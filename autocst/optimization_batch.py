"""Durable, bounded Bayesian batches driven by the existing CST event runner."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re

from .process_identity import get_process_identity
from .service import submission_lock, utc_now, write_json
from .signals import notify


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                   allow_nan=False).encode("utf-8")).hexdigest()


class OptimizationBatches:
    def __init__(self, root: Path, *, service=None):
        from .research_service import ResearchService
        self.root = Path(root).resolve()
        self.service = service or ResearchService(self.root)
        self.directory = self.root / ".autocst" / "optimization_batches"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = self.directory / "controller.lock"

    def path(self, batch_id: str) -> Path:
        if not isinstance(batch_id, str) or not re.fullmatch(r"[a-f0-9]{32}", batch_id):
            raise ValueError("Invalid optimization batch ID")
        return self.directory / batch_id

    def states(self) -> list[dict]:
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(self.directory.glob("*/batch.json"))]

    def _save(self, batch: dict, **changes) -> dict:
        batch = {**batch, **changes, "updated_utc": utc_now()}
        write_json(self.path(batch["batch_id"]) / "batch.json", batch)
        if batch["state"] in {"completed", "stopped", "needs_attention"}:
            self._completion(batch)
        return batch

    def _completion(self, batch: dict):
        notify(self.root, f"batch-{batch['batch_id']}", manual=True)
        write_json(self.path(batch["batch_id"]) / "completion_signal.json", {
            "utc": utc_now(), "state_updated_utc": batch["updated_utc"], "state": batch["state"],
            "delivery": "native_event", "batch_id": batch["batch_id"]})

    def replay_completions(self):
        for batch in self.states():
            if batch["state"] in {"completed", "stopped", "needs_attention"}:
                path = self.path(batch["batch_id"]) / "completion_signal.json"
                prior = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
                if prior.get("state_updated_utc") != batch["updated_utc"]:
                    self._completion(batch)

    def start(self, experiment_id: str, job_template: dict, *, max_new_runs: int = 4,
              config: dict | None = None, idempotency_key: str) -> dict:
        if isinstance(max_new_runs, bool) or not isinstance(max_new_runs, int) or not 1 <= max_new_runs <= 1000:
            raise ValueError("max_new_runs must be an integer from 1 to 1000")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 256:
            raise ValueError("An optimization idempotency key of 1 to 256 characters is required")
        request_input = {"experiment_id": experiment_id, "job_template": job_template,
                         "max_new_runs": max_new_runs, "config": config or {}, "idempotency_key": idempotency_key}
        batch_id = hashlib.sha256((experiment_id + "\0" + idempotency_key).encode()).hexdigest()[:32]
        path = self.path(batch_id)
        # Completed creation has immutable request bytes and an atomic state
        # file. Receipt replay must not contend with the runner advancing it.
        if (path / "batch.json").is_file():
            prior = json.loads((path / "request.json").read_text(encoding="utf-8"))
            if prior["request_sha256"] != digest(request_input):
                raise ValueError("Optimization key already used with different inputs")
            return self.status(batch_id)
        with submission_lock(self.lock):
            if (path / "batch.json").is_file():
                prior = json.loads((path / "request.json").read_text(encoding="utf-8"))
                if prior["request_sha256"] != digest(request_input):
                    raise ValueError("Optimization key already used with different inputs")
                return self.status(batch_id)
            from .research_optimizer import propose_bayesian
            context = self.service.store.context(experiment_id)
            if context["experiment"]["state"] != "active":
                raise ValueError("Resume the experiment before starting an optimization batch")
            if any(item["experiment_id"] == experiment_id and item["state"] in
                   {"active", "paused", "needs_attention"} for item in self.states()):
                raise ValueError("This experiment already has an unfinished optimization batch")
            # Validates the algorithm configuration before any background work.
            propose_bayesian(context["experiment"]["spec"], [], config or {})
            prepared = self.service.prepare_job(experiment_id, job_template, {
                "stage": "batch_template_review", "reason": "Freeze the permitted model and bounded Bayesian policy"})
            current_exp = self.service.store.experiment(experiment_id)
            if (current_exp["version"] != context["experiment"]["version"] or
                    prepared["experiment_version"] != context["experiment"]["version"]):
                raise ValueError("Experiment changed during template preparation; review the new version before starting")
            if prepared["job"].get("solve") is not True:
                raise ValueError("Optimization requires actual solver jobs")
            path.mkdir(exist_ok=True)
            request = {**request_input, "request_sha256": digest(request_input),
                       "job_template": prepared["job"], "spec_version": prepared["experiment_version"],
                       "spec": context["experiment"]["spec"], "code_hashes": prepared["code_hashes"],
                       "template_prepared_id": prepared["prepared_id"], "created_utc": utc_now()}
            write_json(path / "request.json", request)
            (path / "request.sha256").write_text(digest(request), encoding="ascii")
            self._save({"batch_id": batch_id, "experiment_id": experiment_id,
                        "state": "active", "created_utc": utc_now(), "run_ids": [],
                        "max_new_runs": max_new_runs, "method": "Gaussian process / expected improvement",
                        "stop_reason": None})
        notify(self.root)
        return self.status(batch_id)

    def status(self, batch_id: str) -> dict:
        path = self.path(batch_id)
        batch = json.loads((path / "batch.json").read_text(encoding="utf-8"))
        rows = []
        for run_id in batch["run_ids"]:
            run = self.service.store.run(run_id)
            analysis = run["details"].get("analysis", {})
            rows.append({"run_id": run_id, "state": run["state"], "parameters": run["job"]["parameters"],
                         "metrics": analysis.get("metrics"), "target_status": analysis.get("target_status"),
                         "run_directory": run["run_directory"]})
        return {**batch, "runs": rows, "directory": str(path),
                "boundary": "A target hit is a candidate; independent mesh and physical validation are separate"}

    def control(self, batch_id: str, action: str) -> dict:
        if action not in {"pause", "resume", "stop"}:
            raise ValueError("Batch control must be pause, resume or stop")
        with submission_lock(self.lock):
            batch = self.status(batch_id)
            if batch["state"] in {"completed", "stopped"}:
                raise ValueError("A finished batch cannot restart; use a new batch key")
            if action == "resume":
                self._verify(batch)
                if self.service.store.experiment(batch["experiment_id"])["state"] != "active":
                    raise ValueError("Resume the experiment before resuming its optimization batch")
            # The current run belongs to the experiment: pause/stop suppresses
            # future proposals, without cancelling an in-flight CST simulation.
            batch = {key: value for key, value in batch.items() if key not in {"runs", "directory", "boundary"}}
            self._save(batch, state={"resume": "active", "pause": "paused", "stop": "stopped"}[action],
                       stop_reason="user_stop" if action == "stop" else None)
            control_path = self.path(batch_id) / f"control-{utc_now().replace(':', '').replace('+', '_')}.json"
            write_json(control_path, {"action": action, "utc": utc_now()})
        notify(self.root)
        return self.status(batch_id)

    def _verify(self, batch: dict) -> dict:
        from .research_service import sha256
        path = self.path(batch["batch_id"])
        request = json.loads((path / "request.json").read_text(encoding="utf-8"))
        if digest(request) != (path / "request.sha256").read_text(encoding="ascii"):
            raise ValueError("Frozen optimization request changed")
        exp = self.service.store.experiment(batch["experiment_id"])
        if exp["version"] != request["spec_version"]:
            raise ValueError("Experiment changed; create a new explicitly reviewed optimization batch")
        current_hashes = {p.name: sha256(p) for p in (self.root / "autocst").glob("*.py")}
        if current_hashes != request["code_hashes"]:
            raise ValueError("Optimizer implementation changed; create a new reviewed batch")
        source = request["job_template"].get("source_project")
        if source and sha256(Path(source)) != request["job_template"].get("source_project_sha256"):
            raise ValueError("Frozen source project changed; create a new reviewed batch")
        identity = get_process_identity(request["job_template"]["cst_pid"])
        if not identity["alive"] or identity["creation_time"] != request["job_template"]["cst_creation_time"]:
            raise ValueError("Selected CST instance was closed or replaced; do not attach a different instance automatically")
        return request

    def _validation_proposal(self, request: dict, context: dict, batch: dict, target: dict) -> dict:
        """Only declared metasurface mesh checks; never infer checks for custom models."""
        settings = request["spec"].get("validation", {})
        meshes = settings.get("mesh_steps_per_wavelength", [])
        if request["job_template"]["kind"] != "metasurface" or not meshes:
            return target
        if (not isinstance(meshes, list) or len(set(meshes)) != len(meshes) or
                any(isinstance(value, bool) or not isinstance(value, int) or not 8 <= value <= 40 for value in meshes)):
            raise ValueError("Declared independent mesh settings must be distinct integers in [8,40]")
        limits = [settings.get("max_phase_change_deg"), settings.get("max_magnitude_change")]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in limits):
            raise ValueError("Explicit positive finite mesh comparison limits are required")
        candidate_id = batch.get("validation_candidate_run_id", target["best_run_id"])
        candidate = next(run for run in context["runs"] if run["run_id"] == candidate_id)
        batch["validation_candidate_run_id"] = candidate_id
        self._save(batch)
        mesh_keys = {"mesh_steps_per_wavelength", "mesh_cells_per_box"}
        physical = {key: value for key, value in candidate["job"]["parameters"].items() if key not in mesh_keys}
        matches = {}
        for run in context["runs"]:
            parameters = run["job"].get("parameters", {})
            if (run["state"] == "completed" and run["spec_version"] == request["spec_version"] and
                    run["job"].get("lumped_elements", []) == candidate["job"].get("lumped_elements", []) and
                    {key: value for key, value in parameters.items() if key not in mesh_keys} == physical and
                    run["details"].get("analysis", {}).get("usable_for_optimization") is True):
                mesh = parameters.get("mesh_cells_per_box")
                # Preserve the original reference grid; legacy requests for
                # other densities were ineffective and cannot fulfil new checks.
                if mesh is not None:
                    matches[mesh] = run
                elif parameters.get("mesh_steps_per_wavelength") == meshes[0] and meshes[0] not in matches:
                    matches[meshes[0]] = run
        for mesh in meshes:
            if mesh not in matches:
                return {"action": "submit", "method": "declared_independent_initial_mesh_validation",
                        "reason": f"目标候选已命中，保持全部物理参数不变，独立验证初始网格 {mesh}。",
                        "parameters": {**{name: physical[name] for name in request["spec"]["parameter_bounds"]},
                                       "mesh_cells_per_box": mesh},
                        "candidate_run_id": candidate_id, "candidate_only": True,
                        "training_run_ids": target.get("training_run_ids", []), "stage": "mesh_validation"}
        rows = [{"run_id": matches[mesh]["run_id"], "initial_mesh_steps": mesh,
                 **matches[mesh]["details"]["analysis"]["metrics"],
                 "target_met": matches[mesh]["details"]["analysis"].get("target_met"),
                 "constraints_met": matches[mesh]["details"]["analysis"].get("constraints_met")}
                for mesh in meshes]
        for row in rows:
            log_path = Path(matches[row["initial_mesh_steps"]]["run_directory"]) / "solver.log"
            cells = ([int(value) for value in re.findall(r"Number of mesh cells\s*:\s*(\d+)",
                                                       log_path.read_text(encoding="utf-8", errors="replace"))]
                     if log_path.is_file() else [])
            row["initial_mesh_cells"] = cells[0] if cells else None
        distinct_initial_grids = len({row["initial_mesh_cells"] for row in rows if row["initial_mesh_cells"] is not None})
        mesh_effective = distinct_initial_grids == len(meshes)
        phase_change = max(abs((left["phase_deg"] - right["phase_deg"] + 180) % 360 - 180)
                           for left in rows for right in rows)
        magnitude_change = max(row["reflection_magnitude"] for row in rows) - min(row["reflection_magnitude"] for row in rows)
        passed = (mesh_effective and phase_change <= limits[0] and magnitude_change <= limits[1] and
                  all(row["target_met"] is True and row["constraints_met"] is True for row in rows))
        validation = {"utc": utc_now(), "candidate_run_id": candidate_id, "batch_id": batch["batch_id"],
                      "passed": passed, "rows": rows, "max_pairwise_phase_change_deg": phase_change,
                      "mesh_setting_effective": mesh_effective, "distinct_initial_grids": distinct_initial_grids,
                      "max_magnitude_change": magnitude_change, "limits": settings,
                      "scope": "Three declared independent initial grids with internal adaptive refinement; finite numerical sensitivity evidence",
                      "physical_validation": "not_performed"}
        path = self.path(batch["batch_id"]) / "validation.json"
        if not path.is_file():
            write_json(path, validation)
        # This is a separately labelled aggregate view, not a change to any run.
        write_json(self.root / ".autocst" / "experiments" / batch["experiment_id"] / "validation.json", validation)
        return {"action": "stop", "stop_code": ("mesh_settings_ineffective" if not mesh_effective else
                                                 "declared_mesh_checks_passed" if passed else "declared_mesh_checks_failed"),
                "reason": validation["scope"], "validation_file": str(path), "validation_passed": passed}

    def advance(self) -> bool:
        """Queue at most one next job; all crash windows recover the same run key."""
        from .research_optimizer import propose_bayesian
        with submission_lock(self.lock):
            for batch in sorted(self.states(), key=lambda item: item["created_utc"]):
                if batch["state"] != "active":
                    continue
                try:
                    request = self._verify(batch)
                    context = self.service.store.context(batch["experiment_id"])
                    if context["experiment"]["state"] != "active":
                        continue
                    if batch["run_ids"]:
                        last = self.service.store.run(batch["run_ids"][-1])
                        if last["state"] == "needs_attention":
                            self._save(batch, state="needs_attention", stop_reason="run_requires_attention")
                            continue
                        if last["state"] in {"failed", "cancelled"}:
                            self._save(batch, state="stopped", stop_reason=f"run_{last['state']}")
                            continue
                        if last["state"] != "completed":
                            continue
                    sequence = len(batch["run_ids"]) + 1
                    key = f"bayesian-{batch['batch_id']}-{sequence}"
                    checkpoint = self.path(batch["batch_id"]) / f"iteration-{sequence:04d}.json"
                    if checkpoint.is_file():
                        iteration = json.loads(checkpoint.read_text(encoding="utf-8"))
                        if digest(iteration) != checkpoint.with_suffix(".sha256").read_text(encoding="ascii"):
                            raise ValueError("Frozen iteration checkpoint changed")
                    else:
                        proposal = propose_bayesian(request["spec"], context["runs"], request["config"])
                        if proposal["action"] == "wait":
                            continue
                        if proposal["action"] == "stop":
                            if proposal["stop_code"] == "target_requires_validation":
                                proposal = self._validation_proposal(request, context, batch, proposal)
                            if proposal["action"] == "stop":
                                self._save(batch, state="completed", stop_reason=proposal["stop_code"], last_proposal=proposal)
                                continue
                        if len(batch["run_ids"]) >= request["max_new_runs"]:
                            self._save(batch, state="completed", stop_reason="batch_run_limit")
                            continue
                        # Restore any committed iteration first. New requests
                        # alone consume fresh experiment budget reservations.
                        budget = context["budget"]
                        if budget["remaining_runs"] <= 0 or budget["remaining_solver_seconds"] < request["job_template"]["timeout_seconds"]:
                            self._save(batch, state="completed", stop_reason="experiment_budget")
                            continue
                        job = {**request["job_template"], "parameters": {
                            **request["job_template"]["parameters"], **proposal["parameters"]}}
                        previous_parameters = (self.service.store.run(batch["run_ids"][-1])["job"]["parameters"]
                                               if batch["run_ids"] else request["job_template"]["parameters"])
                        decision = {"stage": proposal.get("stage", "bayesian_optimization"), "batch_id": batch["batch_id"],
                                    "sequence": sequence, "hypothesis": request["spec"].get("hypothesis"),
                                    "evidence": proposal, "reason": proposal["reason"],
                                    "parent_run_id": batch["run_ids"][-1] if batch["run_ids"] else None,
                                    "expected_observation": "Reduce the recorded constrained phase-error score; verify the actual solver result",
                                    "parameter_change": {name: {"from": previous_parameters.get(name), "to": value}
                                                         for name, value in proposal["parameters"].items()},
                                    "validation_scope": "Candidate search; target hit still requires independent numerical validation"}
                        prepared = self.service.prepare_job(batch["experiment_id"], job, decision)
                        iteration = {"utc": utc_now(), "sequence": sequence, "idempotency_key": key,
                                     "prepared_id": prepared["prepared_id"], "proposal": proposal}
                        write_json(checkpoint, iteration)
                        checkpoint.with_suffix(".sha256").write_text(digest(iteration), encoding="ascii")
                    # After a commit/receipt crash submit_job recovers the same
                    # frozen prepared bundle and key, never a second CST solve.
                    run = self.service.submit_job(iteration["prepared_id"], key)
                    self._save(batch, run_ids=[*batch["run_ids"], run["run_id"]], last_proposal=iteration["proposal"])
                    return True
                except Exception as exc:
                    self._save(batch, state="needs_attention", stop_reason="controller_error", error=str(exc))
        return False

    def has_ready(self) -> bool:
        active_experiments = {item["experiment_id"] for item in self.service.store.list_experiments()
                              if item["state"] == "active"}
        return any(batch["state"] == "active" and batch["experiment_id"] in active_experiments
                   for batch in self.states())
