import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autocst.research_runner import APITimeout, Runner
from autocst.service import write_json


SPEC = {"name": "recovery test", "objective": {"kind": "phase_target", "frequency_ghz": 10,
        "target_phase_deg": -90, "tolerance_deg": 2}, "constraints": {"min_s11_db": -1},
        "parameter_bounds": {"patch_mm": {"min": 4, "max": 12}},
        "budgets": {"max_runs": 8, "max_total_solver_seconds": 800, "max_run_solver_seconds": 100}}


class FakeAPI:
    def __init__(self):
        self.actions = []
        self.running = True

    def __call__(self, run, action):
        self.actions.append(action)
        directory = Path(run["run_directory"])
        if action == "prepare":
            return {"binding": {"cst_pid": 123, "cst_creation_time": "456", "project_path": str(directory / "test.cst")}}
        if action == "start":
            write_json(directory / "start_returned.json", {"utc": "test"})
            return {"started": True}
        if action == "poll":
            return {"running": self.running, "solver_info": {"state": "RUNNING" if self.running else "SUCCESS"}}
        if action == "cancel":
            self.running = False
            return {"cancelled": True}
        if action == "finish":
            return {"result": {"solver_success": True, "data_integrity": True}}
        raise AssertionError(action)


class RecoveryTests(unittest.TestCase):
    def create(self, root, fake):
        runner = Runner(root, api=fake)
        experiment = runner.store.create_experiment(SPEC)
        run = runner.store.submit(experiment["experiment_id"],
                                  {"kind": "metasurface", "parameters": {"patch_mm": 8},
                                   "cst_pid": 123, "timeout_seconds": 100}, {}, "unique-one")
        return runner, run

    @patch("autocst.research_runner.matches_process", return_value=True)
    def test_restart_while_solving_never_starts_twice(self, matching):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeAPI()
            runner, run = self.create(Path(temp), fake)
            runner.tick()
            self.assertEqual(fake.actions.count("start"), 1)
            restarted = Runner(Path(temp), api=fake)
            restarted.tick()
            self.assertEqual(fake.actions.count("start"), 1)
            fake.running = False
            with patch("autocst.research_analysis.analyze_run", return_value={"target_achieved": False}):
                done = restarted.tick()
            self.assertEqual(done["state"], "completed")
            self.assertEqual(fake.actions.count("start"), 1)

    @patch("autocst.research_runner.matches_process", return_value=True)
    def test_crash_after_start_intent_requires_check_does_not_retry(self, matching):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeAPI()
            runner, run = self.create(Path(temp), fake)
            original = fake.__call__
            def crash(run, action):
                if action == "start":
                    fake.actions.append(action)
                    raise RuntimeError("process died after native start")
                return original(run, action)
            runner.api = crash
            uncertain = runner.tick()
            self.assertEqual(uncertain["state"], "needs_attention")
            runner.api = fake
            runner.store.control(run["experiment_id"], "resume")
            runner.tick()
            self.assertEqual(fake.actions.count("start"), 1)

    @patch("autocst.research_runner.matches_process", return_value=True)
    def test_export_retry_never_restarts_solver(self, matching):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeAPI()
            fake.running = False
            runner, run = self.create(Path(temp), fake)
            original = fake.__call__
            def bad_export(run, action):
                if action == "finish":
                    raise OSError("export disk error")
                return original(run, action)
            runner.api = bad_export
            uncertain = runner.tick()
            self.assertEqual(uncertain["details"]["recovery_state"], "exporting")
            runner.store.control(run["experiment_id"], "resume")
            runner.api = fake
            with patch("autocst.research_analysis.analyze_run", return_value={"target_achieved": False}):
                done = Runner(Path(temp), api=fake).tick()
            self.assertEqual(done["state"], "completed")
            self.assertEqual(fake.actions.count("start"), 1)

    @patch("autocst.research_runner.matches_process", return_value=False)
    def test_reused_pid_does_not_start(self, matching):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeAPI()
            runner, _ = self.create(Path(temp), fake)
            self.assertEqual(runner.tick()["state"], "needs_attention")
            self.assertNotIn("start", fake.actions)

    @patch("autocst.research_runner.matches_process", return_value=True)
    def test_bounded_reconnect_after_poll_timeout_never_restarts_solver(self, matching):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeAPI()
            runner, _ = self.create(Path(temp), fake)
            runner.tick()
            original = fake.__call__
            def timeout(run, action):
                if action == "poll":
                    raise APITimeout("temporary native status stall")
                return original(run, action)
            runner.api = timeout
            self.assertEqual(runner.tick()["phase"], "reconnecting_without_restart")
            recovered = Runner(Path(temp), api=fake).tick()
            self.assertEqual(recovered["state"], "solving")
            self.assertEqual(fake.actions.count("start"), 1)

    @patch("autocst.research_runner.matches_process", return_value=True)
    def test_pause_finishes_current_and_cancel_confirms_stop(self, matching):
        with tempfile.TemporaryDirectory() as temp:
            fake = FakeAPI()
            runner, run = self.create(Path(temp), fake)
            runner.tick()
            runner.store.control(run["experiment_id"], "pause")
            runner.tick()
            self.assertEqual(fake.actions.count("start"), 1)
            runner.store.control(run["experiment_id"], "cancel")
            self.assertEqual(runner.tick()["state"], "cancelled")
            self.assertIn("cancel", fake.actions)


if __name__ == "__main__":
    unittest.main()
