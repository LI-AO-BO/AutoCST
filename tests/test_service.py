import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autocst.service import Service, normalize_job, write_json, process_alive, submission_lock


class ServiceTests(unittest.TestCase):
    def test_rejects_code_nonfinite_booleans_and_unknown_fields(self):
        for job in [
            {"kind": "waveguide", "parameters": {"a_mm": "1:Shell(foo)"}},
            {"kind": "waveguide", "parameters": {"a_mm": float("nan")}},
            {"kind": "waveguide", "parameters": {"a_mm": True}},
            {"kind": "waveguide", "timeout_seconds": True},
            {"kind": "waveguide", "timeout_seconds": 0},
            {"kind": "waveguide", "solve": "false"},
            {"kind": "waveguide", "vba": "arbitrary code"},
            {"kind": "waveguide", "cst_pid": "36684"},
            {"kind": "waveguide", "cst_pid": True},
            {"kind": "existing"},
        ]:
            with self.subTest(job=job), self.assertRaises(ValueError):
                normalize_job(job)

    def test_single_mode_band_constraint(self):
        for params in ({"fmin_ghz": 4}, {"fmax_ghz": 18}, {"b_mm": 30}):
            with self.assertRaises(ValueError):
                normalize_job({"kind": "waveguide", "parameters": params})
        self.assertEqual(normalize_job({"kind": "waveguide"})["parameters"]["a_mm"], 22.86)
        self.assertEqual(normalize_job({"kind": "waveguide", "cst_pid": 36684})["cst_pid"], 36684)

    def test_rejects_run_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            api = Service(Path(temp))
            for name in ("../sources", "a" * 32 + "/x", "C:\\test"):
                with self.assertRaises(ValueError):
                    api.status(name)

    def test_atomic_submission_and_no_unrelated_changes(self):
        with tempfile.TemporaryDirectory() as temp, patch("autocst.service.subprocess.Popen") as popen:
            popen.return_value.pid = 12345
            api = Service(Path(temp))
            receipt = api.submit({"kind": "waveguide", "solve": False})
            run = api.run_path(receipt["run_id"])
            self.assertEqual(json.loads((run / "job.json").read_text())["solve"], False)
            with patch("autocst.service.process_alive", return_value=True):
                with self.assertRaises(RuntimeError):
                    api.submit({"kind": "waveguide"})
            self.assertEqual(popen.call_count, 1)
            self.assertFalse((Path(temp) / "sources").exists())

    def test_dead_worker_is_interrupted_not_completed(self):
        with tempfile.TemporaryDirectory() as temp:
            api = Service(Path(temp))
            run_id = "a" * 32
            run = api.runs / run_id
            write_json(run / "status.json", {"state": "running", "run_id": run_id})
            write_json(run / "process.json", {"pid": 42})
            with patch("autocst.service.process_alive", return_value=False):
                self.assertEqual(api.status(run_id)["state"], "interrupted")
                self.assertIsNone(api.results(run_id)["result"])

    def test_spawn_failure_has_failure_receipt(self):
        with tempfile.TemporaryDirectory() as temp, patch("autocst.service.subprocess.Popen", side_effect=OSError("spawn failed")):
            api = Service(Path(temp))
            with self.assertRaises(OSError):
                api.submit({"kind": "waveguide"})
            statuses = list(api.runs.glob("*/status.json"))
            self.assertEqual(json.loads(statuses[0].read_text())["state"], "failed")
            with submission_lock(api.state / "submit.lock"):
                pass

    def test_process_liveness(self):
        import os
        self.assertTrue(process_alive(os.getpid()))

    def test_process_lock_is_released_after_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            anchor = Path(temp) / "lock"
            with self.assertRaises(ValueError), submission_lock(anchor):
                raise ValueError("crash simulation")
            with submission_lock(anchor):
                pass

    def test_process_receipt_failure_does_not_mark_live_worker_failed(self):
        with tempfile.TemporaryDirectory() as temp, patch("autocst.service.subprocess.Popen") as popen:
            api = Service(Path(temp))
            popen.return_value.pid = 12345
            original = write_json

            def fail_process_receipt(path, value):
                if path.name == "process.json":
                    raise OSError("disk error after spawn")
                original(path, value)

            with patch("autocst.service.write_json", side_effect=fail_process_receipt), self.assertRaises(OSError):
                api.submit({"kind": "waveguide"})
            statuses = list(api.runs.glob("*/status.json"))
            self.assertEqual(json.loads(statuses[0].read_text())["state"], "queued")
            with self.assertRaises(RuntimeError):
                api.submit({"kind": "waveguide"})


if __name__ == "__main__":
    unittest.main()
