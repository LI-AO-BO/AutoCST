import concurrent.futures
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from autocst.research_store import ResearchStore, normalize_spec


def specification(**budgets):
    return {"objective": {"frequency_ghz": 10, "target_phase_deg": -90, "tolerance_deg": 2},
            "constraints": {"min_reflection_magnitude": 0.9},
            "parameter_bounds": {"patch_width_mm": [2, 8]},
            "budgets": {"max_runs": 8, "max_total_solver_seconds": 14400,
                        "max_run_solver_seconds": 3600, **budgets}}


class ResearchStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ResearchStore(Path(self.temp.name))
        self.experiment = self.store.create_experiment(specification())
        self.experiment_id = self.experiment["experiment_id"]

    def submit(self, key="one", **job_overrides):
        return self.store.submit(self.experiment_id,
                                 {"kind": "metasurface", "parameters": {"patch_width_mm": 5}, **job_overrides},
                                 {"reason": "baseline"}, key)

    def test_frozen_version_inputs_and_idempotent_retry_after_revision(self):
        run = self.submit()
        before = (Path(run["run_directory"]) / "spec.json").read_bytes()
        revised = specification()
        revised["objective"]["target_phase_deg"] = 30
        self.store.revise_experiment(self.experiment_id, revised)
        duplicate = self.submit()
        self.assertEqual(duplicate["run_id"], run["run_id"])
        self.assertEqual(duplicate["spec_version"], 1)
        self.assertEqual(self.store.experiment(self.experiment_id)["version"], 2)
        self.assertEqual(before, (Path(run["run_directory"]) / "spec.json").read_bytes())
        self.assertEqual(self.submit("two")["spec_version"], 2)
        with self.assertRaisesRegex(ValueError, "different request"):
            self.submit(timeout_seconds=60)

    def test_concurrent_claims_and_unknown_state_never_duplicate_solver(self):
        first, second = self.submit("one"), self.submit("two")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            claimed = list(pool.map(lambda _: self.store.next_run(), range(6)))
        claimed = [item for item in claimed if item is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["run_id"], first["run_id"])
        self.store.update_run(first["run_id"], "needs_attention", "lost_connection", {"binding": {"pid": 42}})
        restarted = ResearchStore(Path(self.temp.name))
        self.assertIsNone(restarted.next_run())
        self.assertEqual(len(restarted.active_runs()), 1)
        self.assertEqual(restarted.run(second["run_id"])["state"], "queued")
        restarted.control(self.experiment_id, "resume")
        self.assertTrue(restarted.run(first["run_id"])["details"]["resume_requested"])
        self.assertIsNone(restarted.next_run())
        restarted.update_run(first["run_id"], "solving", "reattached", {"binding": {"created_utc": "known"}})
        binding = restarted.run(first["run_id"])["details"]["binding"]
        self.assertEqual(binding, {"pid": 42, "created_utc": "known"})
        self.assertTrue(any(event["event"] == "run_reconciled" for event in restarted.events()))

    def test_pause_finishes_current_cancel_releases_queued_budget(self):
        first, second = self.submit("one"), self.submit("two")
        self.store.next_run()
        self.store.control(self.experiment_id, "pause")
        self.assertNotIn("cancel_requested", self.store.run(first["run_id"])["details"])
        self.assertEqual(self.store.experiment(self.experiment_id)["state"], "paused")
        self.store.control(self.experiment_id, "cancel")
        self.assertTrue(self.store.run(first["run_id"])["details"]["cancel_requested"])
        self.assertEqual(self.store.run(second["run_id"])["state"], "cancelled")
        self.assertEqual(self.store.context(self.experiment_id)["budget"]["solver_seconds_reserved"], 3600)
        self.store.update_run(first["run_id"], "cancelled", "abort_confirmed", {"solver_elapsed_seconds": 30})
        self.store.control(self.experiment_id, "resume")
        self.assertEqual(self.store.context(self.experiment_id)["budget"]["remaining_solver_seconds"], 14370)
        self.assertIsNone(self.store.next_run())

    def test_budget_reservations_and_actual_time_accounting(self):
        for index in range(4):
            self.submit(str(index))
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            self.submit("overflow")
        run = self.store.next_run()
        self.store.update_run(run["run_id"], "completed", "finished", {"solver_elapsed_seconds": 10})
        self.submit("released", timeout_seconds=3000)
        with self.assertRaisesRegex(ValueError, "single-run"):
            self.submit("long", timeout_seconds=3601)
        self.assertEqual(self.store.context(self.experiment_id)["budget"]["solver_seconds_used"], 10)

    def test_failed_without_timing_does_not_invent_zero_usage(self):
        run = self.submit()
        self.store.next_run()
        self.store.update_run(run["run_id"], "failed", "unknown_solver_time")
        self.assertEqual(self.store.context(self.experiment_id)["budget"]["solver_seconds_used"], 3600)
        with self.assertRaisesRegex(ValueError, "cannot be restarted"):
            self.store.update_run(run["run_id"], "queued", "retry")

    def test_events_and_controls_survive_restart_and_ack_is_idempotent(self):
        self.submit()
        requested = self.store.control(self.experiment_id, "pause")
        restarted = ResearchStore(Path(self.temp.name))
        events = restarted.events(self.experiment_id)
        self.assertEqual(len(events), 3)
        event_id = events[-1]["event_id"]
        first = restarted.ack_event(event_id)
        self.assertEqual(restarted.ack_event(event_id)["acknowledged_utc"], first["acknowledged_utc"])
        self.assertEqual(restarted.events(after=event_id), [])
        restarted.ack_control(requested["control_id"])
        self.assertEqual(restarted.controls(), [])
        self.assertEqual(restarted.context(self.experiment_id)["event_cursor"], event_id)

    def test_frozen_input_hashes_and_parameter_bounds(self):
        run = self.submit()
        for filename, expected in run["details"]["input_sha256"].items():
            self.assertEqual(hashlib.sha256((Path(run["run_directory"]) / filename).read_bytes()).hexdigest(), expected)
        with self.assertRaisesRegex(ValueError, "outside"):
            self.submit("bad", parameters={"patch_width_mm": 99})
        with self.assertRaisesRegex(ValueError, "must be explicit"):
            self.submit("missing", parameters={})
        for budgets in ({"max_runs": True}, {"max_run_solver_seconds": 0}, {"max_total_solver_seconds": 1}):
            with self.assertRaises(ValueError):
                normalize_spec(specification(**budgets))

    def test_heartbeat_updates_snapshot_without_flooding_events(self):
        run = self.submit()
        self.store.next_run()
        self.store.update_run(run["run_id"], "solving", "solver_running", {"poll": 0})
        before = len(self.store.events())
        for index in range(10):
            self.store.update_run(run["run_id"], "solving", "solver_running", {"poll": index})
        self.assertEqual(len(self.store.events()), before)
        self.assertEqual(self.store.run(run["run_id"])["details"]["poll"], 9)
        with self.assertRaises(ValueError):
            self.store.update_run(run["run_id"], "completed", "finished", {"solver_elapsed_seconds": -1})

    def test_submit_checks_prepared_version_in_the_same_transaction(self):
        job = {"kind": "metasurface", "parameters": {"patch_width_mm": 5}}
        original = self.store.submit(self.experiment_id, job, {}, "first", expected_spec_version=1)
        self.store.revise_experiment(self.experiment_id, specification())
        with self.assertRaisesRegex(ValueError, "revised after preparation"):
            self.store.submit(self.experiment_id, job, {}, "stale", expected_spec_version=1)
        self.assertEqual(self.store.submit(self.experiment_id, job, {}, "first", expected_spec_version=1)["run_id"], original["run_id"])
        barrier = threading.Barrier(2)
        def revise():
            barrier.wait()
            return self.store.revise_experiment(self.experiment_id, specification())
        def submit():
            barrier.wait()
            try:
                return self.store.submit(self.experiment_id, job, {}, "racing", expected_spec_version=2)
            except ValueError as error:
                return str(error)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            revision_future, submission_future = pool.submit(revise), pool.submit(submit)
            self.assertEqual(revision_future.result()["version"], 3)
            submitted = submission_future.result()
        if isinstance(submitted, dict):
            self.assertEqual(submitted["spec_version"], 2)
        else:
            self.assertIn("revised after preparation", submitted)

    def test_fixed_parameters_reject_booleans_expressions_and_nonfinite_values(self):
        for value in (True, "3.2", float("nan"), float("inf")):
            candidate = specification()
            candidate["model"] = {"kind": "metasurface", "fixed_parameters": {"epsilon_r": value}}
            with self.assertRaises(ValueError):
                normalize_spec(candidate)

    def test_active_query_does_not_load_archived_history(self):
        first = self.submit()
        self.store.next_run()
        self.store.update_run(first["run_id"], "completed", "finished", {"solver_elapsed_seconds": 1})
        active = self.submit("active")
        self.store.next_run()
        original = self.store._run
        with patch.object(self.store, "_run", wraps=original) as loader:
            self.assertEqual(self.store.active_runs()[0]["run_id"], active["run_id"])
            self.assertEqual(loader.call_count, 1)
            self.assertEqual(loader.call_args.args[1], active["run_id"])


if __name__ == "__main__":
    unittest.main()
