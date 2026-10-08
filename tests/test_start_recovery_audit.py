"""Persisted starting-state recovery audit using fake API calls only.

Every test uses a new temporary database, reconstructs the Runner, and records
the actions observed. No CST process or resident runner is accessed or controlled.
"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autocst.research_runner import Runner
from autocst.service import write_json


class StartingRecoveryAuditTests(unittest.TestCase):
    # A dedicated report command may collect these observations after this suite.
    # Running ordinary unittest discovery writes only to TemporaryDirectory.
    observations = {}

    def exercise(self, name, *, running, solver_state, fresh, ack, expected, expected_actions):
        with tempfile.TemporaryDirectory(prefix="autocst-start-recovery-") as temp:
            root = Path(temp)
            actions = []

            def fake_api(run, action):
                actions.append(action)
                if action == "poll":
                    return {"running": running, "solver_info": {"state": solver_state},
                            "fresh_run_confirmed": fresh}
                if action == "finish":
                    return {"result": {"solver_success": True, "data_integrity": True}}
                raise AssertionError(f"Unexpected side-effect request during recovery: {action}")

            before = Runner(root, api=fake_api)
            experiment = before.store.create_experiment({"objective": "Fake API starting-state audit"})
            run = before.store.submit(
                experiment["experiment_id"],
                {"kind": "waveguide", "parameters": {}, "timeout_seconds": 300},
                {"scope": "test only; never sent to CST"}, "starting-audit",
            )
            before.store.update_run(run["run_id"], "starting", "start_requested")
            directory = Path(run["run_directory"])
            write_json(directory / "binding.json", {"cst_pid": 123, "cst_creation_time": "fake-only"})
            write_json(directory / "start_requested.json", {"test_only": True})
            if ack:
                write_json(directory / "start_returned.json", {"test_only": True})

            restored = Runner(root, api=fake_api)
            self.assertNotEqual(before.boot_id, restored.boot_id)
            with patch("autocst.research_runner.matches_process", return_value=True):
                with patch.object(restored, "power"):
                    with patch("autocst.research_analysis.analyze_run", return_value={"target_achieved": False}):
                        result = restored.tick()

            self.assertEqual(result["state"], expected)
            self.assertEqual(actions, expected_actions)
            self.assertNotIn("start", actions)
            persisted = before.store.run(run["run_id"])
            self.assertEqual(persisted["state"], expected)
            self.assertEqual(result["run_id"], run["run_id"])
            self.observations[name] = {
                "native_solver_running": running, "native_solver_state": solver_state,
                "fresh_result_evidence": fresh, "start_returned_receipt": ack,
                "restored_state": result["state"], "persisted_state": persisted["state"],
                "api_actions": list(actions), "start_resent": False, "passed": True,
            }

    def test_running_without_ack_only_polls(self):
        self.exercise("running_without_ack", running=True, solver_state="RUNNING", fresh=False,
                      ack=False, expected="starting", expected_actions=["poll"])

    def test_success_without_ack_or_fresh_evidence_requires_attention(self):
        self.exercise("stopped_without_fresh_evidence", running=False, solver_state="SUCCESS", fresh=False,
                      ack=False, expected="needs_attention", expected_actions=["poll"])

    def test_fresh_success_without_ack_can_finish_without_restarting(self):
        self.exercise("success_with_fresh_evidence", running=False, solver_state="SUCCESS", fresh=True,
                      ack=False, expected="completed", expected_actions=["poll", "finish"])

    def test_stopped_without_success_requires_attention(self):
        self.exercise("stopped_without_success", running=False, solver_state="IDLE", fresh=False,
                      ack=False, expected="needs_attention", expected_actions=["poll"])

    def test_success_with_ack_finishes_without_restarting(self):
        self.exercise("success_with_ack", running=False, solver_state="SUCCESS", fresh=True,
                      ack=True, expected="completed", expected_actions=["poll", "finish"])


if __name__ == "__main__":
    unittest.main()
