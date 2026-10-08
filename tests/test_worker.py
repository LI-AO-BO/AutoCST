import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from autocst.cst_backend import CSTCleanupRequired
from autocst.service import write_json
from autocst.worker import execute, main


class WorkerTests(unittest.TestCase):
    def test_watchdog_preserves_uncertain_cst_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = "c" * 32
            run = root / ".autocst" / "runs" / run_id
            write_json(run / "status.json", {"state": "running", "run_id": run_id})
            write_json(run / "job.json", {"timeout_seconds": 300})
            with patch("sys.argv", ["worker", "--root", str(root), "--run-id", run_id]), \
                 patch("autocst.worker.subprocess.run", side_effect=subprocess.TimeoutExpired("owned worker", 480)):
                self.assertEqual(main(), 124)
            state = json.loads((run / "status.json").read_text())
            self.assertEqual(state["state"], "interrupted")
            self.assertTrue(state["cleanup_required"])

    def test_cleanup_uncertainty_remains_blocking(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = "a" * 32
            run = root / ".autocst" / "runs" / run_id
            write_json(run / "status.json", {"state": "queued", "run_id": run_id})
            write_json(run / "job.json", {"kind": "waveguide", "solve": True})
            with patch("autocst.cst_backend.run_cst", side_effect=CSTCleanupRequired("owned instance still open")), patch("autocst.worker.traceback.print_exc"):
                self.assertEqual(execute(root, run_id), 1)
            self.assertEqual(json.loads((run / "status.json").read_text())["state"], "failed_cleanup")
            self.assertFalse((run / "result.json").exists())

    def test_build_only_never_claims_solver_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = "b" * 32
            run = root / ".autocst" / "runs" / run_id
            write_json(run / "status.json", {"state": "queued", "run_id": run_id})
            write_json(run / "job.json", {"kind": "waveguide", "solve": False})
            with patch("autocst.cst_backend.run_cst", return_value={"solver_completed": False}):
                self.assertEqual(execute(root, run_id), 0)
            self.assertEqual(json.loads((run / "status.json").read_text())["state"], "built")
            with patch("autocst.cst_backend.run_cst") as backend, self.assertRaises(RuntimeError):
                execute(root, run_id)
            backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
