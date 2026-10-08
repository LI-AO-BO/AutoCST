"""Short, reviewable research operations shared by Codex, CLI and MATLAB."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

from .config import environment
from .process_identity import get_process_identity
from .service import Service, submission_lock, utc_now, write_json


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class ResearchService:
    def __init__(self, root: Path):
        from .research_store import ResearchStore
        self.root = Path(root).expanduser().resolve()
        self.state = self.root / ".autocst"
        self.state.mkdir(exist_ok=True)
        self.store = ResearchStore(self.state)

    def environment(self) -> dict:
        return {**environment(), "version": "0.2.0", "workspace": str(self.root),
                "supported_research_jobs": ["metasurface", "waveguide", "history", "existing_project"],
                "runner": self.runner_status(), "solver_policy": "explicit_user_selected_cst_pid",
                "long_run_boundary": "Submitted solver/export/metrics continue independently; Codex reasoning resumes when app is available"}

    def create_experiment(self, spec: dict) -> dict:
        if not isinstance(spec, dict):
            raise ValueError("Experiment specification must be an object")
        # The store validates budgets and bounds; a named scientific objective is
        # required before a generic script can consume machine time.
        if not spec.get("objective"):
            raise ValueError("A scientific objective must be specified")
        return self.store.create_experiment(spec)

    def start_optimization(self, experiment_id: str, job_template: dict, *, max_new_runs: int = 4,
                           config: dict | None = None, idempotency_key: str) -> dict:
        from .optimization_batch import OptimizationBatches
        return OptimizationBatches(self.root, service=self).start(
            experiment_id, job_template, max_new_runs=max_new_runs, config=config, idempotency_key=idempotency_key)

    def optimization_status(self, batch_id: str) -> dict:
        from .optimization_batch import OptimizationBatches
        return OptimizationBatches(self.root, service=self).status(batch_id)

    def control_optimization(self, batch_id: str, action: str) -> dict:
        from .optimization_batch import OptimizationBatches
        return OptimizationBatches(self.root, service=self).control(batch_id, action)

    def revise_experiment(self, experiment_id: str, spec: dict) -> dict:
        return self.store.revise_experiment(experiment_id, spec)

    @staticmethod
    def validate_contract(experiment: dict, job: dict):
        spec = experiment["spec"]
        model = spec.get("model", {})
        if model.get("kind") and job["kind"] != model["kind"]:
            raise ValueError("Model kind differs from the experiment; revise the experiment explicitly")
        parameters = job.get("parameters", {})
        for name, value in model.get("fixed_parameters", {}).items():
            if parameters.get(name) != value:
                raise ValueError(f"Fixed model parameter changed: {name}; revise the experiment")
        for name, bounds in spec.get("parameter_bounds", {}).items():
            low, high = (bounds["min"], bounds["max"]) if isinstance(bounds, dict) else bounds
            if name not in parameters or not low <= parameters[name] <= high:
                raise ValueError(f"Parameter {name} must lie in the agreed experiment bounds")
        if job["timeout_seconds"] > spec["budgets"]["max_run_solver_seconds"]:
            raise ValueError("Job solver limit exceeds the agreed single-run budget")

    def prepare_job(self, experiment_id: str, job: dict, decision: dict | None = None) -> dict:
        from .research_models import normalize_research_job, render_history
        experiment = self.store.experiment(experiment_id)
        if not isinstance(job, dict):
            raise ValueError("Simulation job must be an object")
        supplied = dict(job)
        supplied.setdefault("timeout_seconds", experiment["spec"]["budgets"]["max_run_solver_seconds"])
        normalized = normalize_research_job(supplied)
        self.validate_contract(experiment, normalized)
        if not normalized.get("cst_pid"):
            raise ValueError("Select the manually started CST instance with cst_pid")
        identity = get_process_identity(normalized["cst_pid"])
        if not identity["alive"]:
            raise ValueError("Selected CST process is not running")
        normalized["cst_creation_time"] = identity["creation_time"]
        # Freeze a source file and history text, not a mutable path supplied by a chat.
        if normalized.get("history_path"):
            path = Path(normalized.pop("history_path")).expanduser().resolve()
            normalized["history_text"] = path.read_text(encoding="utf-8-sig")
            normalized["history_source"] = {"path": str(path), "sha256": sha256(path)}
        source = normalized.get("source_project")
        if source:
            path = Path(source).expanduser().resolve()
            if path.suffix.lower() != ".cst" or not path.is_file():
                raise ValueError("source_project must be an existing .cst file")
            normalized["source_project"] = str(path)
            normalized["source_project_sha256"] = sha256(path)
        prepared_id = uuid.uuid4().hex
        directory = self.state / "prepared" / prepared_id
        directory.mkdir(parents=True)
        history = render_history(normalized)
        (directory / "history.vba").write_bytes((history or "").encode("utf-8"))
        normalized["history_text"] = history or normalized.get("history_text", "")
        code_hashes = {p.name: sha256(p) for p in (self.root / "autocst").glob("*.py")}
        payload = {"prepared_id": prepared_id, "experiment_id": experiment_id,
                   "experiment_version": experiment.get("version", experiment.get("spec_version", 1)),
                   "created_utc": utc_now(), "job": normalized, "decision": decision or {},
                   "code_hashes": code_hashes, "history_sha256": sha256(directory / "history.vba"),
                   "directory": str(directory), "review": {
                       "model_kind": normalized["kind"], "parameters": normalized.get("parameters", {}),
                       "cst_identity": identity, "solver_budget_seconds": normalized.get("timeout_seconds"),
                       "history_file": str(directory / "history.vba"),
                       "scientific_scope": "Idealized numerical simulation; mesh convergence and measurement are separate"}}
        write_json(directory / "prepared.json", payload)
        (directory / "prepared.sha256").write_text(sha256(directory / "prepared.json"), encoding="ascii")
        return payload

    def submit_job(self, prepared_id: str, idempotency_key: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", prepared_id):
            raise ValueError("Invalid prepared_id")
        prepared_file = self.state / "prepared" / prepared_id / "prepared.json"
        if sha256(prepared_file) != prepared_file.with_suffix(".sha256").read_text(encoding="ascii").strip():
            raise ValueError("Prepared input bundle changed; prepare a new revision")
        payload = json.loads(prepared_file.read_text(encoding="utf-8"))
        decision = {**payload["decision"], "prepared_id": prepared_id,
                    "implementation_sha256": payload["code_hashes"]}
        # Receipt recovery remains idempotent even after a code/spec upgrade.
        # Store.submit compares the frozen request before considering new budgets.
        if any(run["idempotency_key"] == idempotency_key for run in self.store.runs(payload["experiment_id"])):
            prior = self.store.submit(payload["experiment_id"], payload["job"], decision, idempotency_key)
            return {**prior, "runner": self.runner_status()}
        experiment = self.store.experiment(payload["experiment_id"])
        if experiment["version"] != payload["experiment_version"]:
            raise ValueError("Experiment was revised after preparation; prepare against the new version")
        directory = Path(payload["directory"])
        if sha256(directory / "history.vba") != payload["history_sha256"]:
            raise ValueError("Prepared history changed; prepare a new revision")
        if hashlib.sha256(payload["job"]["history_text"].encode("utf-8")).hexdigest() != payload["history_sha256"]:
            raise ValueError("Frozen execution history differs from the reviewable script")
        for name, digest in payload["code_hashes"].items():
            if sha256(self.root / "autocst" / name) != digest:
                raise ValueError(f"Implementation changed after preparation: {name}; prepare again")
        job = payload["job"]
        self.validate_contract(experiment, job)
        if job.get("source_project") and sha256(Path(job["source_project"])) != job["source_project_sha256"]:
            raise ValueError("Source project changed after preparation")
        with submission_lock(self.state / "submit.lock"):
            legacy = Service(self.root)
            if legacy.runs.exists():
                for directory in legacy.runs.iterdir():
                    if directory.is_dir() and (directory / "status.json").is_file():
                        prior = legacy.status(directory.name)
                        if prior["state"] in {"queued", "running", "interrupted", "failed_cleanup"}:
                            raise ValueError(f"Unresolved legacy run owns CST: {directory.name} ({prior['state']})")
            run = self.store.submit(payload["experiment_id"], job, decision, idempotency_key,
                                    expected_spec_version=payload["experiment_version"])
        if run["state"] == "queued":
            # Idempotent repeat does not replace an already consumed input bundle.
            review_path = Path(run["run_directory"]) / "preparation.json"
            if not review_path.exists():
                write_json(review_path, payload)
        from .signals import notify
        notify(self.root)
        return {**run, "runner": self.runner_status()}

    def run_status(self, run_id: str) -> dict:
        return {**self.store.run(run_id), "runner": self.runner_status()}

    def run_results(self, run_id: str) -> dict:
        run = self.store.run(run_id)
        directory = Path(run["run_directory"])
        def optional(name):
            file = directory / name
            return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else None
        return {"status": run, "result": optional("result.json"), "analysis": optional("analysis.json")}

    def experiment_context(self, experiment_id: str) -> dict:
        context = self.store.context(experiment_id)
        validation = self.state / "experiments" / experiment_id / "validation.json"
        context["independent_validation"] = json.loads(validation.read_text(encoding="utf-8")) if validation.is_file() else None
        from .optimization_batch import OptimizationBatches
        context["optimization_batches"] = [batch for batch in OptimizationBatches(self.root, service=self).states()
                                            if batch["experiment_id"] == experiment_id]
        write_json(self.state / "experiments" / experiment_id / "context.json", context)
        return {**context, "runner": self.runner_status()}

    def events(self, experiment_id: str | None = None, after: int = 0) -> dict:
        events = self.store.events(experiment_id, after)
        meaningful = [event for event in events if event["event"] in {"control_requested", "experiment_revised"}
                      or event.get("data", {}).get("state") in {"completed", "failed", "cancelled", "needs_attention"}
                      or event["event"] == "run_cancelled"]
        return {"events": meaningful, "next_cursor": max((event["event_id"] for event in events), default=after),
                "runner": self.runner_status()}

    def acknowledge(self, event_id: int) -> dict:
        return self.store.ack_event(event_id)

    def control(self, experiment_id: str, action: str) -> dict:
        result = self.store.control(experiment_id, action)
        from .signals import notify
        notify(self.root)
        return result

    def runner_status(self) -> dict:
        path = self.state / "runner_status.json"
        if not path.is_file():
            return {"installed": (self.state / "runner_task.json").is_file(), "alive": False,
                    "instruction": "Install/start the current-user resident runner"}
        status = json.loads(path.read_text(encoding="utf-8"))
        identity = get_process_identity(status["pid"])
        status["alive"] = identity["alive"] and identity["creation_time"] == status["process"]["creation_time"]
        from .research_runner import seconds_since
        status["heartbeat_age_seconds"] = seconds_since(status["heartbeat_utc"])
        status["responsive"] = status["alive"] and status["heartbeat_age_seconds"] < 20
        return status

    def install_runner(self) -> dict:
        return self._runner_task("install")

    def start_runner(self) -> dict:
        stop = self.state / "runner_stop.request"
        if stop.exists():
            stop.unlink()
        return self._runner_task("start")

    def _runner_task(self, action: str) -> dict:
        if sys.platform != "win32":
            raise RuntimeError("Resident runner installation currently supports Windows")
        task_name = "AutoCST-" + hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            raise RuntimeError("pythonw.exe is required for a hidden interactive runner")
        script = self.root / "integration" / "manage-runner.ps1"
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                               "-Action", action, "-Root", str(self.root), "-Python", str(pythonw),
                               "-TaskName", task_name], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        if proc.returncode:
            raise RuntimeError(f"Current-user task {action} failed: {proc.stderr.strip()} {proc.stdout.strip()}")
        receipt = {"task_name": task_name, "action": action, "utc": utc_now(), "output": proc.stdout.strip(),
                   "identity": "current_logged_in_user", "interactive": True, "elevated": False}
        write_json(self.state / "runner_task.json", receipt)
        return receipt
