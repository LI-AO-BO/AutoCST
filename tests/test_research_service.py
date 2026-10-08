import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autocst.research_service import ResearchService


SPEC = {"objective": {"kind": "phase_target", "frequency_ghz": 10, "target_phase_deg": -90},
        "model": {"kind": "metasurface", "fixed_parameters": {"period_mm": 15.0}},
        "parameter_bounds": {"patch_mm": [4, 12]},
        "budgets": {"max_runs": 5, "max_total_solver_seconds": 3000, "max_run_solver_seconds": 600}}


class PreparationTests(unittest.TestCase):
    def fixture(self, temp):
        root = Path(temp)
        (root / "autocst").mkdir()
        (root / "autocst" / "frozen.py").write_text("# reviewed code\n")
        service = ResearchService(root)
        return service, service.create_experiment(SPEC)["experiment_id"]

    @patch("autocst.research_service.get_process_identity", return_value={"pid": 123, "alive": True, "creation_time": "456"})
    def test_prepare_only_and_idempotent_submission(self, identity):
        with tempfile.TemporaryDirectory() as temp:
            service, exp = self.fixture(temp)
            prepared = service.prepare_job(exp, {"kind": "metasurface", "parameters": {"patch_mm": 8}, "cst_pid": 123})
            self.assertEqual(service.store.runs(), [])
            self.assertEqual(prepared["job"]["timeout_seconds"], 600)
            receipt = service.submit_job(prepared["prepared_id"], "one")
            # Receipt recovery must work even after a future code upgrade.
            (service.root / "autocst" / "frozen.py").write_text("# new code\n")
            repeated = service.submit_job(prepared["prepared_id"], "one")
            self.assertEqual(receipt["run_id"], repeated["run_id"])
            self.assertEqual(len(service.store.runs()), 1)

    @patch("autocst.research_service.get_process_identity", return_value={"pid": 123, "alive": True, "creation_time": "456"})
    def test_new_submission_rejects_changed_script_code_or_objective(self, identity):
        with tempfile.TemporaryDirectory() as temp:
            service, exp = self.fixture(temp)
            prepared = service.prepare_job(exp, {"kind": "metasurface", "cst_pid": 123})
            history = Path(prepared["directory"]) / "history.vba"
            history.write_text("changed")
            with self.assertRaises(ValueError):
                service.submit_job(prepared["prepared_id"], "one")
            prepared = service.prepare_job(exp, {"kind": "metasurface", "cst_pid": 123})
            service.revise_experiment(exp, {**SPEC, "objective": {"kind": "phase_target", "frequency_ghz": 10, "target_phase_deg": 0}})
            with self.assertRaises(ValueError):
                service.submit_job(prepared["prepared_id"], "two")

    def test_fixed_topology_and_parameter_range_cannot_be_changed_silently(self):
        with tempfile.TemporaryDirectory() as temp:
            service, exp = self.fixture(temp)
            for job in ({"kind": "metasurface", "parameters": {"patch_mm": 13}},
                        {"kind": "metasurface", "parameters": {"period_mm": 20}},
                        {"kind": "waveguide"}):
                with self.subTest(job=job), self.assertRaises(ValueError):
                    service.prepare_job(exp, job)

    def test_completion_events_omit_background_state_noise(self):
        with tempfile.TemporaryDirectory() as temp:
            service, exp = self.fixture(temp)
            receipt = service.store.submit(exp, {"kind": "metasurface", "parameters": {"patch_mm": 8}, "timeout_seconds": 100}, {}, "one")
            self.assertEqual(service.events(exp)["events"], [])
            service.store.update_run(receipt["run_id"], "completed", "analysis_completed")
            self.assertEqual(len(service.events(exp)["events"]), 1)


if __name__ == "__main__":
    unittest.main()
