"""Independent event-loop audit: temporary evidence only, never a real CST process."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from autocst.event_runner import EventRunner
from autocst.research_runner import APIUncertain
from autocst.service import utc_now, write_json
from autocst.signals import wait_for_run


class EventRunnerAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.queue = Mock(handle=11)
        event_patch = patch("autocst.event_runner.Event", return_value=self.queue)
        event_patch.start()
        self.addCleanup(event_patch.stop)
        notification_patch = patch("autocst.event_runner.notify")
        self.notify = notification_patch.start()
        self.addCleanup(notification_patch.stop)
        self.api = Mock()
        self.runner = EventRunner(self.root, api=self.api)
        self.runner.power = Mock()
        self.runner.heartbeat = Mock()
        experiment = self.runner.store.create_experiment({
            "objective": {"frequency_ghz": 10, "target_phase_deg": -90, "tolerance_deg": 2},
            "constraints": {"min_reflection_magnitude": 0.9},
            "parameter_bounds": {"patch_width_mm": [2, 8]},
            "budgets": {"max_runs": 8, "max_run_solver_seconds": 600, "max_total_solver_seconds": 3600}})
        self.experiment_id = experiment["experiment_id"]
        self.run = self.runner.store.submit(self.experiment_id,
            {"kind": "metasurface", "parameters": {"patch_width_mm": 5}, "timeout_seconds": 600},
            {"reason": "Synthetic event-loop audit; no CST instance is used"}, "audit")
        self.directory = Path(self.run["run_directory"])
        self.response = self.directory / "completion.response.json"
        self.identity = {"pid": 4321, "creation_time": "synthetic-worker-identity", "alive": True}
        self.result = {"solver_success": True, "solver_info": {"state": "SUCCESS"},
                       "numerical_validity": {"passed": True}}
        self.binding = {"cst_pid": 1234, "cst_creation_time": "synthetic-cst-identity",
                        "project_path": str(self.directory / "project.cst")}
        write_json(self.directory / "binding.json", self.binding)
        (self.directory / "project" / "Result").mkdir(parents=True)
        (self.directory / "reflection.csv").write_text(
            "frequency_ghz,s11_real,s11_imag,cross_real,cross_imag,total_reflected_power\n"
            "10,0,-1,0,0,1\n", encoding="utf-8")

    def activate(self, *, cancel=False):
        self.runner.store.next_run()
        self.run = self.runner.store.update_run(self.run["run_id"], "solving", "awaiting_cst_completion",
            {"solver_started_utc": utc_now(), "response_path": str(self.response), "cancel_requested": cancel})
        return self.run

    def write_completion(self):
        write_json(self.response, {"ok": True, "solver_info": {"state": "SUCCESS"}, "fresh_run_confirmed": True})

    def test_worker_completion_signal_exports_without_start_or_poll(self):
        self.activate()
        self.api.return_value = {"result": self.result}
        worker = Mock(handle=22)
        def completed(handles, timeout):
            self.assertEqual(handles, [worker, self.queue])
            self.write_completion()
            return 0
        with patch("autocst.event_runner.ProcessExit", return_value=worker) as attach, \
             patch("autocst.event_runner.wait", side_effect=completed) as waiting:
            result = self.runner.await_worker(self.run, self.identity, self.response)
        self.assertEqual(result["state"], "completed")
        self.assertEqual([call.args[1] for call in self.api.call_args_list], ["finish"])
        self.assertEqual(result["details"]["solver_poll_count"], 0)
        self.assertEqual(waiting.call_count, 1)
        attach.assert_called_once_with(4321, "synthetic-worker-identity")
        worker.close.assert_called_once()

    def test_cancel_uses_only_native_cancel_and_requires_confirmed_stop(self):
        self.activate(cancel=True)
        self.api.return_value = {"cancelled": {"stopped": True}}
        worker = Mock(handle=22)
        child = Mock()
        with patch("autocst.event_runner.ProcessExit", return_value=worker), \
             patch("autocst.event_runner.wait") as waiting:
            result = self.runner.await_worker(self.run, self.identity, self.response, child)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["phase"], "solver_stop_confirmed")
        self.assertEqual([call.args[1] for call in self.api.call_args_list], ["cancel"])
        waiting.assert_not_called()
        child.wait.assert_called_once_with(timeout=10)
        child.kill.assert_not_called()

    def test_unconfirmed_cancel_preserves_active_ownership(self):
        self.activate(cancel=True)
        self.api.return_value = {"cancelled": {"stopped": False}}
        with patch("autocst.event_runner.ProcessExit", return_value=Mock(handle=22)):
            with self.assertRaisesRegex(APIUncertain, "not confirmed"):
                self.runner.await_worker(self.run, self.identity, self.response)
        self.assertEqual(self.runner.store.active_runs()[0]["run_id"], self.run["run_id"])
        self.assertFalse(self.response.exists())

    def test_restart_rebinds_original_live_worker_identity_without_launching(self):
        self.activate()
        write_json(self.directory / "solver_wait_process.json", {**self.identity, "response_path": str(self.response)})
        with patch.object(self.runner, "await_worker", return_value=self.run) as waiting, \
             patch.object(self.runner, "launch_waiter") as launch:
            result = self.runner.execute_once()
        self.assertEqual(result["state"], "solving")
        attached_run, identity, response = waiting.call_args.args
        self.assertEqual(attached_run["run_id"], self.run["run_id"])
        self.assertEqual(identity["pid"], 4321)
        self.assertEqual(identity["creation_time"], "synthetic-worker-identity")
        self.assertEqual(response, self.response)
        launch.assert_not_called()
        self.api.assert_not_called()

    def test_dead_worker_uses_directory_event_then_verifies_without_second_solve(self):
        self.activate()
        sequence = []
        def api(run, action):
            sequence.append(action)
            if action == "poll":
                if sequence.count("poll") == 1:
                    return {"running": True}
                return {"running": False, "solver_info": {"state": "SUCCESS"}, "fresh_run_confirmed": True}
            if action == "finish":
                return {"result": self.result}
            self.fail(f"Recovery must never execute {action}")
        self.api.side_effect = api
        change = Mock(handle=33)
        def directory_created(path):
            sequence.append("watch_registered")
            return change
        with patch("autocst.event_runner.ProcessExit", side_effect=ProcessLookupError), \
             patch("autocst.event_runner.matches_process", return_value=True), \
             patch("autocst.event_runner.DirectoryChange", side_effect=directory_created), \
             patch("autocst.event_runner.wait", return_value=0) as waiting:
            result = self.runner.await_worker(self.run, self.identity, self.response)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(sequence, ["watch_registered", "poll", "poll", "finish"])
        self.assertEqual(waiting.call_count, 1)
        change.rearm.assert_called_once()
        change.close.assert_called_once()

    def test_health_recovers_lost_queue_notification_without_cst_query(self):
        self.runner.shutdown = Mock()
        self.runner.shutdown.is_set.side_effect = [False, True]
        self.runner.health()
        self.queue.set.assert_called_once()
        self.runner.heartbeat.assert_called_once_with(None)
        self.api.assert_not_called()

    def test_health_recovers_lost_active_cancel_notification_without_cst_query(self):
        self.activate()
        self.runner.current_run = self.run
        self.runner.store.control(self.experiment_id, "cancel")
        self.runner.shutdown = Mock()
        self.runner.shutdown.is_set.side_effect = [False, True]
        self.runner.health()
        self.queue.set.assert_called_once()
        self.api.assert_not_called()

    def test_health_recovers_lost_resume_notification_without_cst_query(self):
        self.activate()
        self.runner.store.update_run(self.run["run_id"], "needs_attention", "synthetic_interruption",
                                     {"recovery_state": "solving"})
        self.runner.store.control(self.experiment_id, "resume")
        self.runner.shutdown = Mock()
        self.runner.shutdown.is_set.side_effect = [False, True]
        self.runner.health()
        self.queue.set.assert_called_once()
        self.api.assert_not_called()

    def test_health_does_not_wake_an_acknowledged_paused_queue(self):
        self.runner.store.control(self.experiment_id, "pause")
        self.runner.acknowledge_controls()
        self.runner.shutdown = Mock()
        self.runner.shutdown.is_set.side_effect = [False, True]
        self.runner.health()
        self.queue.set.assert_not_called()
        self.api.assert_not_called()

    def test_health_replays_completion_committed_before_native_notification(self):
        self.activate()
        self.runner.store.update_run(self.run["run_id"], "completed", "analysis_completed")
        self.assertFalse((self.directory / "completion_signal.json").exists())
        self.runner.shutdown = Mock()
        self.runner.shutdown.is_set.side_effect = [False, True]
        self.runner.health()
        self.notify.assert_called_once_with(self.runner.root, f"run-{self.run['run_id']}", manual=True)
        receipt = json.loads((self.directory / "completion_signal.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(self.runner.store.run(self.run["run_id"])["state"], "completed")
        self.api.assert_not_called()

    def test_health_replays_final_completion_after_an_older_attention_receipt(self):
        self.activate()
        write_json(self.directory / "completion_signal.json", {"state": "needs_attention", "utc": "old"})
        self.runner.store.update_run(self.run["run_id"], "completed", "analysis_completed")
        self.runner.shutdown = Mock()
        self.runner.shutdown.is_set.side_effect = [False, True]
        self.runner.health()
        self.notify.assert_called_once_with(self.runner.root, f"run-{self.run['run_id']}", manual=True)
        receipt = json.loads((self.directory / "completion_signal.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["state"], "completed")
        self.assertNotEqual(receipt["utc"], "old")
        self.api.assert_not_called()

    def test_health_replays_a_new_attention_generation_after_an_older_attention_receipt(self):
        self.activate()
        write_json(self.directory / "completion_signal.json",
                   {"state": "needs_attention", "utc": "old", "state_updated_utc": "old"})
        self.runner.store.update_run(self.run["run_id"], "needs_attention", "second_recovery_failure",
                                     {"resume_requested": False})
        self.runner.shutdown = Mock()
        self.runner.shutdown.is_set.side_effect = [False, True]
        self.runner.health()
        self.notify.assert_called_once_with(self.runner.root, f"run-{self.run['run_id']}", manual=True)
        receipt = json.loads((self.directory / "completion_signal.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["state"], "needs_attention")
        self.assertNotEqual(receipt["utc"], "old")
        self.api.assert_not_called()

    def test_cancelled_queued_run_notifies_its_existing_waiter_without_solver(self):
        self.runner.store.control(self.experiment_id, "cancel")
        self.runner.acknowledge_controls()
        self.notify.assert_called_once_with(self.runner.root, f"run-{self.run['run_id']}", manual=True)
        self.assertEqual(self.runner.store.run(self.run["run_id"])["state"], "cancelled")
        self.api.assert_not_called()

    def test_recovery_control_wakes_do_not_extend_solver_deadline(self):
        self.activate()
        self.api.side_effect = [{"running": True}, {"cancelled": {"stopped": True}}]
        waits = []
        def wake(handles, timeout):
            waits.append(timeout)
            return 1 if len(waits) == 1 else None
        def elapsed(value):
            return 590 if waits else 0
        with patch("autocst.event_runner.matches_process", return_value=True), \
             patch("autocst.event_runner.DirectoryChange", return_value=Mock(handle=33)), \
             patch("autocst.event_runner.seconds_since", side_effect=elapsed), \
             patch("autocst.event_runner.wait", side_effect=wake):
            result = self.runner.recover_by_directory(self.run)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(waits, [600, 10])
        self.assertEqual([call.args[1] for call in self.api.call_args_list], ["poll", "cancel"])

    def test_recovery_stop_signal_stops_monitoring_without_stopping_cst(self):
        self.activate()
        self.api.return_value = {"running": True}
        calls = []
        def stop_signal(handles, timeout):
            calls.append(1)
            if len(calls) > 1:
                raise AssertionError("Recovery waited again after its graceful-stop signal")
            (self.runner.state / "runner_stop.request").write_text("stop", encoding="utf-8")
            return 1
        with patch("autocst.event_runner.matches_process", return_value=True), \
             patch("autocst.event_runner.DirectoryChange", return_value=Mock(handle=33)), \
             patch("autocst.event_runner.wait", side_effect=stop_signal):
            with self.assertRaisesRegex(APIUncertain, "stop|Stop"):
                self.runner.recover_by_directory(self.run)
        self.assertEqual([call.args[1] for call in self.api.call_args_list], ["poll"])


class CompletionWaitAuditTests(unittest.TestCase):
    def setUp(self):
        self.event = MagicMock()
        self.event.__enter__.return_value = self.event
        self.store = Mock()
        self.root = Path(tempfile.gettempdir()) / "autocst-no-real-run-audit"

    def test_resumed_attention_signal_cannot_return_a_running_state_or_extend_deadline(self):
        self.store.run.side_effect = [{"state": "solving"}, {"state": "solving"}, {"state": "completed"}]
        with patch("autocst.research_store.ResearchStore", return_value=self.store), \
             patch("autocst.signals.Event", return_value=self.event), \
             patch("autocst.signals.time.monotonic", side_effect=[100, 100, 690]), \
             patch("autocst.signals.wait", side_effect=[0, 0]) as waiting:
            result = wait_for_run(self.root, "synthetic", 600)
        self.assertEqual(result["state"], "completed")
        self.assertEqual([call.args[1] for call in waiting.call_args_list], [600, 10])
        self.assertEqual(self.event.reset.call_count, 3)
        self.assertEqual(self.store.run.call_count, 3)

    def test_pending_resume_cannot_return_the_old_attention_before_runner_rebinds(self):
        self.store.run.side_effect = [
            {"state": "needs_attention", "details": {"resume_requested": True}},
            {"state": "completed", "details": {}}]
        with patch("autocst.research_store.ResearchStore", return_value=self.store), \
             patch("autocst.signals.Event", return_value=self.event), \
             patch("autocst.signals.time.monotonic", side_effect=[100, 100]), \
             patch("autocst.signals.wait", return_value=0) as waiting:
            result = wait_for_run(self.root, "synthetic", 600)
        self.assertEqual(result["state"], "completed")
        waiting.assert_called_once_with([self.event], 600)
        self.assertEqual(self.store.run.call_count, 2)

    def test_nonterminal_signal_does_not_restart_the_completion_deadline(self):
        self.store.run.return_value = {"state": "solving"}
        with patch("autocst.research_store.ResearchStore", return_value=self.store), \
             patch("autocst.signals.Event", return_value=self.event), \
             patch("autocst.signals.time.monotonic", side_effect=[100, 100, 690]), \
             patch("autocst.signals.wait", side_effect=[0, None]) as waiting:
            with self.assertRaisesRegex(TimeoutError, "deadline"):
                wait_for_run(self.root, "synthetic", 600)
        self.assertEqual([call.args[1] for call in waiting.call_args_list], [600, 10])
        self.assertEqual(self.store.run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
