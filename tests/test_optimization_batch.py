"""Durable batch contracts; every CST operation is replaced by a fake service."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from autocst.optimization_batch import OptimizationBatches
from autocst.research_store import ResearchStore


SPEC = {
    "objective": {"kind": "phase_target", "frequency_ghz": 10,
                  "target_phase_deg": -90, "tolerance_deg": 5},
    "model": {"kind": "metasurface", "fixed_parameters": {"period_mm": 15}},
    "parameter_bounds": {"patch_mm": [4, 14]},
    "budgets": {"max_runs": 12, "max_total_solver_seconds": 1200,
                "max_run_solver_seconds": 60},
}
TEMPLATE = {
    "kind": "metasurface", "cst_pid": 321, "solve": True,
    "timeout_seconds": 60,
    "parameters": {"patch_mm": 9, "period_mm": 15,
                   "air_height_mm": 10, "mesh_steps_per_wavelength": 10},
}
IDENTITY = {"pid": 321, "alive": True, "creation_time": "fixture-cst-instance"}


class SimulatedProcessExit(BaseException):
    """A process death is deliberately outside the controller's error handler."""


class FakeResearchService:
    def __init__(self, root):
        self.root = root
        self.store = ResearchStore(root / ".autocst")
        self.preparations = []
        self.submissions = []
        self.before_prepare = None
        self.fail_prepare = False
        self.exit_after_commit = False
        self.error_after_commit = False
        self.prepared_directory = root / ".autocst" / "fake_prepared"
        self.prepared_directory.mkdir(parents=True)

    def prepare_job(self, experiment_id, job, decision):
        if self.before_prepare:
            self.before_prepare()
        if self.fail_prepare:
            raise ValueError("Preparation failed before any submission")
        exp = self.store.experiment(experiment_id)
        frozen_job = deepcopy(job)
        frozen_job.setdefault("cst_creation_time", IDENTITY["creation_time"])
        frozen_job.setdefault("timeout_seconds", exp["spec"]["budgets"]["max_run_solver_seconds"])
        frozen_job.setdefault("solve", True)
        prepared = {
            "prepared_id": uuid.uuid4().hex, "experiment_id": experiment_id,
            "experiment_version": exp["version"], "job": frozen_job,
            "decision": deepcopy(decision),
            "code_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in (self.root / "autocst").glob("*.py")},
        }
        path = self.prepared_directory / f"{prepared['prepared_id']}.json"
        path.write_text(json.dumps(prepared), encoding="utf-8")
        self.preparations.append(prepared)
        return deepcopy(prepared)

    def submit_job(self, prepared_id, key):
        prepared = json.loads((self.prepared_directory / f"{prepared_id}.json").read_text(encoding="utf-8"))
        self.submissions.append((prepared_id, key))
        receipt = self.store.submit(prepared["experiment_id"], prepared["job"],
                                    prepared["decision"], key,
                                    expected_spec_version=prepared["experiment_version"])
        if self.exit_after_commit:
            self.exit_after_commit = False
            raise SimulatedProcessExit("DB commit succeeded; process died before batch receipt")
        if self.error_after_commit:
            self.error_after_commit = False
            raise RuntimeError("DB commit succeeded; reply delivery failed")
        return receipt


def fake_proposal(spec, runs, config):
    candidate = next((run for run in runs if run["details"].get("analysis", {}).get("target_met")), None)
    if candidate:
        return {"action": "stop", "stop_code": "target_requires_validation",
                "best_run_id": candidate["run_id"],
                "reason": "A candidate hit requires independent validation"}
    return {"action": "submit", "parameters": {"patch_mm": 8 + len(runs) * 0.1},
            "reason": "Fixture proposal based on the available completed runs"}


class OptimizationBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "autocst").mkdir()
        (self.root / "autocst" / "reviewed.py").write_text("# frozen implementation\n", encoding="utf-8")
        self.service = FakeResearchService(self.root)
        self.experiment_id = self.service.store.create_experiment(SPEC)["experiment_id"]
        self.batches = OptimizationBatches(self.root, service=self.service)
        self.proposal = patch("autocst.research_optimizer.propose_bayesian", side_effect=fake_proposal).start()
        self.addCleanup(patch.stopall)
        self.identity = patch("autocst.optimization_batch.get_process_identity", return_value=deepcopy(IDENTITY)).start()
        self.notification = patch("autocst.optimization_batch.notify").start()

    def start(self, *, key="batch-one", max_new_runs=3, template=None):
        return self.batches.start(self.experiment_id, deepcopy(template or TEMPLATE),
                                  max_new_runs=max_new_runs, config={"seed": 7},
                                  idempotency_key=key)

    def complete(self, run_id, *, target_met=False, elapsed=1, phase=-90.0, magnitude=1.0):
        return self.service.store.update_run(run_id, "completed", "analysis_completed", {
            "solver_elapsed_seconds": elapsed,
            "analysis": {"target_met": target_met, "constraints_met": True, "usable_for_optimization": True,
                         "target_status": "provisional_requires_validation" if target_met else "not_met",
                         "metrics": {"phase_error_deg": 1 if target_met else 20,
                                     "phase_deg": phase, "reflection_magnitude": magnitude}},
        })

    def test_start_freezes_template_identity_policy_and_budget_without_submitting(self):
        template = deepcopy(TEMPLATE)
        before = self.service.store.context(self.experiment_id)["budget"]
        batch = self.start(template=template, max_new_runs=2)
        template["parameters"]["period_mm"] = 30
        request = json.loads((self.batches.path(batch["batch_id"]) / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(request["job_template"]["parameters"], TEMPLATE["parameters"])
        self.assertEqual(request["job_template"]["cst_creation_time"], IDENTITY["creation_time"])
        self.assertEqual(request["spec"]["budgets"], SPEC["budgets"])
        self.assertEqual(request["max_new_runs"], 2)
        self.assertEqual(request["config"], {"seed": 7})
        self.assertEqual(self.service.store.context(self.experiment_id)["budget"], before)
        self.assertEqual(self.service.submissions, [])

    def test_same_key_start_is_idempotent_and_changed_inputs_are_rejected(self):
        first = self.start()
        repeated = self.start()
        self.assertEqual(first["batch_id"], repeated["batch_id"])
        self.assertEqual(len(self.service.preparations), 1)
        with self.assertRaises(ValueError):
            self.start(max_new_runs=4)
        self.assertEqual(len(self.batches.states()), 1)

    def test_existing_receipt_replay_does_not_contend_with_advancing_runner(self):
        from autocst.service import submission_lock
        first = self.start()
        with submission_lock(self.batches.lock):
            repeated = self.start()
        self.assertEqual(first["batch_id"], repeated["batch_id"])
        self.assertEqual(len(self.service.preparations), 1)

    def test_concurrent_starts_leave_one_unfinished_batch_for_the_experiment(self):
        entered, release = threading.Event(), threading.Event()

        def blocked_prepare():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Concurrent fixture was not released")

        self.service.before_prepare = blocked_prepare
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.start, key="winner")
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaises((ValueError, RuntimeError)):
                    self.start(key="rival")
            finally:
                release.set()
            first.result(timeout=5)
        self.service.before_prepare = None
        with self.assertRaises(ValueError):
            self.start(key="later-rival")
        self.assertEqual(len(self.batches.states()), 1)

    def test_queued_or_solving_current_run_blocks_a_second_submission(self):
        batch = self.start()
        self.assertTrue(self.batches.advance())
        current = self.batches.status(batch["batch_id"])["run_ids"][0]
        self.assertFalse(self.batches.advance())
        self.service.store.update_run(current, "solving", "awaiting_cst_completion")
        self.assertFalse(self.batches.advance())
        self.assertEqual(len(self.service.store.runs()), 1)
        self.assertEqual(len(self.service.submissions), 1)

    def test_max_new_runs_stops_after_completed_runs_without_new_submission(self):
        batch = self.start(max_new_runs=2)
        for index in range(2):
            self.assertTrue(self.batches.advance())
            status = self.batches.status(batch["batch_id"])
            self.assertEqual(len(status["run_ids"]), index + 1)
            self.complete(status["run_ids"][-1])
        self.assertFalse(self.batches.advance())
        final = self.batches.status(batch["batch_id"])
        self.assertEqual((final["state"], final["stop_reason"]), ("completed", "batch_run_limit"))
        self.assertEqual(len(self.service.store.runs()), 2)

    def test_target_hit_stops_candidate_search_without_claiming_validation(self):
        batch = self.start()
        self.batches.advance()
        self.complete(self.batches.status(batch["batch_id"])["run_ids"][-1], target_met=True)
        self.assertFalse(self.batches.advance())
        final = self.batches.status(batch["batch_id"])
        self.assertEqual((final["state"], final["stop_reason"]), ("completed", "target_requires_validation"))
        self.assertIn("independent mesh", final["boundary"])
        self.assertEqual(len(self.service.store.runs()), 1)

    def test_pause_batch_and_pause_experiment_each_block_advance(self):
        batch = self.start()
        self.batches.control(batch["batch_id"], "pause")
        self.assertFalse(self.batches.advance())
        self.assertFalse(self.batches.has_ready())
        self.batches.control(batch["batch_id"], "resume")
        self.service.store.control(self.experiment_id, "pause")
        self.assertFalse(self.batches.advance())
        self.assertFalse(self.batches.has_ready())
        with self.assertRaises(ValueError):
            self.batches.control(batch["batch_id"], "resume")
        self.service.store.control(self.experiment_id, "resume")
        self.assertTrue(self.batches.advance())

    def test_stop_suppresses_future_proposals_and_does_not_cancel_inflight(self):
        batch = self.start()
        self.batches.advance()
        run_id = self.batches.status(batch["batch_id"])["run_ids"][-1]
        self.service.store.update_run(run_id, "solving", "awaiting_cst_completion")
        self.batches.control(batch["batch_id"], "stop")
        self.assertFalse(self.batches.advance())
        self.assertEqual(self.service.store.run(run_id)["state"], "solving")
        self.assertFalse(self.service.store.run(run_id)["details"].get("cancel_requested", False))
        with self.assertRaises(ValueError):
            self.batches.control(batch["batch_id"], "resume")

    def test_changed_spec_stops_at_attention_without_submitting(self):
        batch = self.start()
        self.service.store.revise_experiment(self.experiment_id, deepcopy(SPEC))
        self.assertFalse(self.batches.advance())
        self.assertEqual(self.batches.status(batch["batch_id"])["state"], "needs_attention")
        self.assertEqual(self.service.submissions, [])

    def test_revision_during_template_preparation_cannot_mix_spec_and_version(self):
        def revise_during_prepare():
            self.service.before_prepare = None
            revised = deepcopy(SPEC)
            revised["objective"]["target_phase_deg"] = 0
            self.service.store.revise_experiment(self.experiment_id, revised)

        self.service.before_prepare = revise_during_prepare
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(self.batches.states(), [])
        self.assertEqual(self.service.submissions, [])

    def test_changed_implementation_stops_at_attention_without_submitting(self):
        batch = self.start()
        (self.root / "autocst" / "reviewed.py").write_text("# changed implementation\n", encoding="utf-8")
        self.assertFalse(self.batches.advance())
        self.assertEqual(self.batches.status(batch["batch_id"])["state"], "needs_attention")
        self.assertEqual(self.service.submissions, [])

    def test_replaced_or_dead_cst_identity_cannot_auto_attach_or_submit(self):
        batch = self.start()
        self.identity.return_value = {**IDENTITY, "creation_time": "different-instance"}
        self.assertFalse(self.batches.advance())
        self.assertEqual(self.batches.status(batch["batch_id"])["state"], "needs_attention")
        with self.assertRaises(ValueError):
            self.batches.control(batch["batch_id"], "resume")
        self.assertEqual(self.service.submissions, [])

    def test_tampered_frozen_request_stops_at_attention(self):
        batch = self.start()
        path = self.batches.path(batch["batch_id"]) / "request.json"
        request = json.loads(path.read_text(encoding="utf-8"))
        request["max_new_runs"] = 100
        path.write_text(json.dumps(request), encoding="utf-8")
        self.assertFalse(self.batches.advance())
        self.assertEqual(self.batches.status(batch["batch_id"])["state"], "needs_attention")
        self.assertEqual(self.service.submissions, [])

    def test_failed_cancelled_and_attention_runs_are_never_automatically_retried(self):
        for run_state, batch_state in (("failed", "stopped"), ("cancelled", "stopped"),
                                       ("needs_attention", "needs_attention")):
            with self.subTest(state=run_state):
                experiment_id = self.service.store.create_experiment(deepcopy(SPEC))["experiment_id"]
                batch = self.batches.start(experiment_id, deepcopy(TEMPLATE), max_new_runs=3,
                                           idempotency_key=f"fault-{run_state}")
                self.assertTrue(self.batches.advance())
                run_id = self.batches.status(batch["batch_id"])["run_ids"][-1]
                self.service.store.update_run(run_id, run_state, "injected_failure", {"solver_elapsed_seconds": 1})
                before = len(self.service.submissions)
                self.assertFalse(self.batches.advance())
                self.assertFalse(self.batches.advance())
                self.assertEqual(self.batches.status(batch["batch_id"])["state"], batch_state)
                self.assertEqual(len(self.service.submissions), before)

    def test_preparation_error_requires_attention_and_does_not_retry_on_health_wake(self):
        batch = self.start()
        self.service.fail_prepare = True
        self.assertFalse(self.batches.advance())
        self.service.fail_prepare = False
        self.assertFalse(self.batches.advance())
        self.assertEqual(self.batches.status(batch["batch_id"])["state"], "needs_attention")
        self.assertEqual(self.service.store.runs(), [])

    def test_commit_then_process_exit_reuses_checkpoint_prepared_id_and_run_key(self):
        batch = self.start()
        self.service.exit_after_commit = True
        with self.assertRaises(SimulatedProcessExit):
            self.batches.advance()
        self.assertEqual(len(self.service.store.runs()), 1)
        self.assertEqual(self.batches.status(batch["batch_id"])["run_ids"], [])
        checkpoint = self.batches.path(batch["batch_id"]) / "iteration-0001.json"
        frozen = checkpoint.read_bytes()
        restarted = OptimizationBatches(self.root, service=self.service)
        self.assertTrue(restarted.advance())
        self.assertEqual(checkpoint.read_bytes(), frozen)
        self.assertEqual(self.service.submissions[0], self.service.submissions[1])
        self.assertEqual(len(self.service.preparations), 2)  # template plus one iteration
        self.assertEqual(len(self.service.store.runs()), 1)
        self.assertEqual(len(restarted.status(batch["batch_id"])["run_ids"]), 1)

    def test_commit_then_receipt_error_waits_for_explicit_resume_and_reuses_same_key(self):
        batch = self.start()
        self.service.error_after_commit = True
        self.assertFalse(self.batches.advance())
        self.assertEqual(self.batches.status(batch["batch_id"])["state"], "needs_attention")
        self.assertFalse(self.batches.advance())
        self.assertEqual(len(self.service.submissions), 1)
        self.batches.control(batch["batch_id"], "resume")
        self.assertTrue(self.batches.advance())
        self.assertEqual(self.service.submissions[0], self.service.submissions[1])
        self.assertEqual(len(self.service.store.runs()), 1)

    def test_last_budget_commit_is_reconciled_before_budget_blocks_new_submissions(self):
        spec = deepcopy(SPEC)
        spec["budgets"].update(max_runs=1, max_total_solver_seconds=60)
        experiment_id = self.service.store.create_experiment(spec)["experiment_id"]
        batch = self.batches.start(experiment_id, deepcopy(TEMPLATE), max_new_runs=3,
                                   idempotency_key="last-budget-slot")
        self.service.exit_after_commit = True
        with self.assertRaises(SimulatedProcessExit):
            self.batches.advance()
        run = self.service.store.runs(experiment_id)[0]
        restarted = OptimizationBatches(self.root, service=self.service)
        self.assertTrue(restarted.advance())
        recovered = restarted.status(batch["batch_id"])
        self.assertEqual(recovered["run_ids"], [run["run_id"]])
        self.assertEqual(recovered["state"], "active")
        self.complete(run["run_id"])
        self.assertFalse(restarted.advance())
        self.assertEqual(restarted.status(batch["batch_id"])["stop_reason"], "experiment_budget")
        self.assertEqual(len(self.service.store.runs(experiment_id)), 1)

    def test_inactive_experiment_or_build_only_template_cannot_start_batch(self):
        self.service.store.control(self.experiment_id, "pause")
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(self.service.preparations, [])
        self.service.store.control(self.experiment_id, "resume")
        with self.assertRaises(ValueError):
            self.start(template={**deepcopy(TEMPLATE), "solve": False})
        self.assertEqual(self.batches.states(), [])

    def test_invalid_policy_is_rejected_before_preparation_or_submission(self):
        self.proposal.side_effect = ValueError("Invalid Bayesian configuration")
        with self.assertRaises(ValueError):
            self.start()
        self.assertEqual(self.service.preparations, [])
        self.assertEqual(self.batches.states(), [])

    def test_declared_mesh_validation_uses_two_new_runs_and_preserves_candidate(self):
        for expected_pass, last_phase in ((True, -89.5), (False, -84.0)):
            with self.subTest(passed=expected_pass):
                spec = deepcopy(SPEC)
                spec["validation"] = {"mesh_steps_per_wavelength": [10, 15, 20],
                                      "max_phase_change_deg": 3, "max_magnitude_change": 0.01}
                experiment_id = self.service.store.create_experiment(spec)["experiment_id"]
                candidate_job = deepcopy(TEMPLATE)
                candidate_job["parameters"]["patch_mm"] = 9.223
                candidate = self.service.store.submit(experiment_id, candidate_job, {}, "original-candidate")
                self.complete(candidate["run_id"], target_met=True)
                (Path(candidate["run_directory"]) / "solver.log").write_text("Number of mesh cells : 1100\n", encoding="utf-8")
                original_before = self.service.store.run(candidate["run_id"])
                input_before = (Path(candidate["run_directory"]) / "job.json").read_bytes()
                batch = self.batches.start(experiment_id, deepcopy(TEMPLATE), max_new_runs=2,
                                           idempotency_key=f"mesh-validation-{expected_pass}")
                for mesh, phase in ((15, -90.5), (20, last_phase)):
                    self.assertTrue(self.batches.advance())
                    run_id = self.batches.status(batch["batch_id"])["run_ids"][-1]
                    run = self.service.store.run(run_id)
                    self.assertEqual(run["job"]["parameters"]["mesh_cells_per_box"], mesh)
                    physical = {k: v for k, v in run["job"]["parameters"].items() if k not in {"mesh_steps_per_wavelength", "mesh_cells_per_box"}}
                    self.assertEqual(physical, {k: v for k, v in candidate_job["parameters"].items()
                                                if k not in {"mesh_steps_per_wavelength", "mesh_cells_per_box"}})
                    self.assertEqual(run["decision"]["stage"], "mesh_validation")
                    self.complete(run_id, target_met=expected_pass or mesh == 15, phase=phase)
                    (Path(run["run_directory"]) / "solver.log").write_text(f"Number of mesh cells : {1000 + mesh * 10}\n", encoding="utf-8")
                self.assertFalse(self.batches.advance())
                final = self.batches.status(batch["batch_id"])
                self.assertEqual(final["state"], "completed")
                self.assertEqual(final["stop_reason"], "declared_mesh_checks_passed" if expected_pass
                                 else "declared_mesh_checks_failed")
                self.assertEqual(len(final["run_ids"]), 2)
                self.assertEqual(len(self.service.store.runs(experiment_id)), 3)
                validation = json.loads((self.batches.path(batch["batch_id"]) / "validation.json").read_text(encoding="utf-8"))
                self.assertEqual(validation["passed"], expected_pass)
                self.assertTrue(validation["mesh_setting_effective"])
                self.assertEqual([row["initial_mesh_steps"] for row in validation["rows"]], [10, 15, 20])
                self.assertEqual(validation["physical_validation"], "not_performed")
                self.assertEqual(self.service.store.run(candidate["run_id"]), original_before)
                self.assertEqual((Path(candidate["run_directory"]) / "job.json").read_bytes(), input_before)

    def test_dead_cst_identity_requires_attention_before_any_submission(self):
        batch = self.start()
        self.identity.return_value = {**IDENTITY, "alive": False}
        self.assertFalse(self.batches.advance())
        self.assertEqual(self.batches.status(batch["batch_id"])["state"], "needs_attention")
        self.assertEqual(self.service.submissions, [])

    def test_same_or_unknown_actual_initial_grids_cannot_pass_validation(self):
        for cells in (None, 1561):
            with self.subTest(initial_cells=cells):
                spec = deepcopy(SPEC)
                spec["validation"] = {"mesh_steps_per_wavelength": [10, 15, 20],
                                      "max_phase_change_deg": 3, "max_magnitude_change": 0.01}
                experiment_id = self.service.store.create_experiment(spec)["experiment_id"]
                candidates = []
                for mesh in (10, 15, 20):
                    job = deepcopy(TEMPLATE)
                    job["parameters"].update(patch_mm=9.223, mesh_cells_per_box=mesh)
                    run = self.service.store.submit(experiment_id, job, {}, f"candidate-{mesh}")
                    self.complete(run["run_id"], target_met=True)
                    if cells is not None:
                        (Path(run["run_directory"]) / "solver.log").write_text(f"Number of mesh cells : {cells}\n", encoding="utf-8")
                    candidates.append(run)
                batch = self.batches.start(experiment_id, deepcopy(TEMPLATE), max_new_runs=2,
                                           idempotency_key=f"grid-effect-{cells}")
                self.assertFalse(self.batches.advance())
                self.assertEqual(self.batches.status(batch["batch_id"])["stop_reason"], "mesh_settings_ineffective")
                result = json.loads((self.batches.path(batch["batch_id"]) / "validation.json").read_text())
                self.assertFalse(result["passed"])
                self.assertFalse(result["mesh_setting_effective"])
                self.assertEqual(len(self.service.store.runs(experiment_id)), 3)

    def test_tampered_iteration_checkpoint_requires_attention_without_new_submit(self):
        batch = self.start()
        self.service.exit_after_commit = True
        with self.assertRaises(SimulatedProcessExit):
            self.batches.advance()
        checkpoint = self.batches.path(batch["batch_id"]) / "iteration-0001.json"
        iteration = json.loads(checkpoint.read_text(encoding="utf-8"))
        iteration["prepared_id"] = uuid.uuid4().hex
        checkpoint.write_text(json.dumps(iteration), encoding="utf-8")
        self.assertFalse(self.batches.advance())
        self.assertEqual(self.batches.status(batch["batch_id"])["state"], "needs_attention")
        self.assertEqual(len(self.service.submissions), 1)
        self.assertEqual(len(self.service.store.runs()), 1)


if __name__ == "__main__":
    unittest.main()
