"""Resident queue executor. Each native API call has a separate, bounded process."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

from .process_identity import get_process_identity, matches_process
from .service import submission_lock, write_json, utc_now


class APIUncertain(RuntimeError):
    pass


class APITimeout(APIUncertain):
    pass


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def seconds_since(utc: str | None) -> float:
    return max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(utc)).total_seconds()) if utc else 0


class Runner:
    def __init__(self, root: Path, *, api=None):
        from .research_store import ResearchStore
        self.root = Path(root).resolve()
        self.state = self.root / ".autocst"
        self.state.mkdir(exist_ok=True)
        self.store = ResearchStore(self.state)
        self.api = api or self.call_api
        self.boot_id = uuid.uuid4().hex
        self.started_utc = utc_now()
        self.power_lease = False

    def heartbeat(self, run: dict | None = None, error: str | None = None):
        write_json(self.state / "runner_status.json", {
            "pid": os.getpid(), "process": get_process_identity(os.getpid()),
            "boot_id": self.boot_id, "started_utc": self.started_utc,
            "heartbeat_utc": utc_now(), "run_id": run.get("run_id") if run else None,
            "phase": run.get("phase") if run else "idle", "error": error,
            "execution": "current_user_interactive_task", "schema": 2})
        self.sample_soak()

    def sample_soak(self):
        request = self.state / "background_soak.json"
        if not request.is_file():
            return
        report = read_json(request)
        if report.get("state") != "running":
            return
        now = utc_now()
        gap = seconds_since(report.get("last_sample_utc"))
        if report.get("last_sample_utc") and gap < 60:
            return
        if report.get("boot_id") and report["boot_id"] != self.boot_id:
            report["restart_count"] = report.get("restart_count", 0) + 1
        report.update(boot_id=self.boot_id, last_sample_utc=now,
                      samples=report.get("samples", 0) + 1,
                      max_sample_gap_seconds=max(gap, report.get("max_sample_gap_seconds", 0)))
        elapsed = seconds_since(report["started_utc"])
        report["elapsed_seconds"] = elapsed
        if elapsed >= report["required_seconds"]:
            report.update(state="completed", completed_utc=now,
                          passed=report.get("restart_count", 0) == 0 and report["max_sample_gap_seconds"] < 120,
                          scope="Resident runner survival only; not a long CST solver or app-close acceptance")
        write_json(request, report)
        with (self.state / "background_soak_samples.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps({"utc": now, "boot_id": self.boot_id, "elapsed_seconds": elapsed,
                                  "max_gap_seconds": report["max_sample_gap_seconds"]}) + "\n")

    def power(self, active: bool):
        if sys.platform == "win32" and active != self.power_lease:
            import ctypes
            # A temporary system-awake lease; no power-plan/registry changes and
            # no claim to prevent explicit user sleep, shutdown or logoff.
            result = ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if active else 0x80000000)
            if not result:
                raise OSError("Could not change temporary system-awake lease")
        self.power_lease = active

    def call_api(self, run: dict, action: str) -> dict:
        directory = Path(run["run_directory"])
        calls = directory / "api_calls"
        calls.mkdir(exist_ok=True)
        call_id = f"{time.time_ns()}_{action}"
        request_path, response_path = calls / f"{call_id}.request.json", calls / f"{call_id}.response.json"
        versions = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (self.root / "autocst").glob("*.py")}
        write_json(request_path, {"run_directory": str(directory), "job": run["job"], "action": action,
                                  "phase_implementation_sha256": versions})
        # Preparation/export can need more time than a solver status query; none
        # shares the multi-hour solver budget or holds an MCP request open.
        limit = {"prepare": 180, "finish": 300, "start": 90, "cancel": 45, "poll": 30}[action]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PYTHONPATH=str(self.root))
        with (calls / f"{call_id}.log").open("ab") as log:
            proc = subprocess.Popen([sys.executable, "-m", "autocst.research_action", str(request_path), str(response_path)],
                                    cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            try:
                proc.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                proc.kill()  # Only our blocked API process; never the user's CST.
                proc.wait(timeout=10)
                raise APITimeout(f"CST {action} API exceeded {limit}s; solver state must be verified")
        if not response_path.is_file():
            raise APIUncertain(f"CST {action} API exited without response (code {proc.returncode})")
        payload = read_json(response_path)
        if not payload.get("ok"):
            raise APIUncertain(f"CST {action}: {payload.get('error')}")
        return payload

    def update(self, run: dict, state: str, phase: str, **details) -> dict:
        return self.store.update_run(run["run_id"], state, phase, details)

    def tick(self, *, prepare_only: bool = False) -> dict | None:
        active = self.store.active_runs()
        if len(active) > 1:
            raise RuntimeError("More than one claimed CST run: queue integrity requires attention")
        run = active[0] if active else self.store.next_run()
        self.power(run is not None)
        self.heartbeat(run)
        if run is None:
            return None
        directory = Path(run["run_directory"])
        details = run.get("details", {})
        if run["state"] == "needs_attention":
            if not details.get("resume_requested") and not details.get("cancel_requested"):
                return run
            run = self.update(run, details.get("recovery_state", "solving"), "verifying_recovery",
                              resume_requested=False)
            details = run.get("details", {})
        try:
            if details.get("cancel_requested"):
                if (directory / "binding.json").is_file():
                    return self.cancel(run)
                if not (directory / "prepare_requested.json").exists():
                    return self.update(run, "cancelled", "cancelled_before_preparation", solver_elapsed_seconds=0)
                raise APIUncertain("Cannot confirm cancellation: interrupted preparation has no binding")
            # Freeze verification detects tampering before any side effect.
            input_path = directory / "job.json"
            if details.get("job_sha256") and hashlib.sha256(input_path.read_bytes()).hexdigest() != details["job_sha256"]:
                raise APIUncertain("Frozen job.json changed after submission")
            for name, digest in details.get("input_sha256", {}).items():
                path = directory / name
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise APIUncertain(f"Frozen input changed after submission: {name}")
            if run["state"] == "preparing":
                marker = directory / "prepare_requested.json"
                if marker.exists():
                    # The native build might have happened without a final receipt.
                    # Only a persisted project binding can resolve that ambiguity.
                    if not (directory / "binding.json").is_file() or not (directory / "prepare_completed.json").is_file():
                        raise APIUncertain("Preparation interrupted without complete saved-model evidence; inspect CST")
                    run = self.update(run, "prepared", "recovered_preparation")
                else:
                    for name, digest in run.get("decision", {}).get("implementation_sha256", {}).items():
                        if hashlib.sha256((self.root / "autocst" / name).read_bytes()).hexdigest() != digest:
                            raise APIUncertain(f"Implementation changed before execution: {name}; prepare a new run")
                    write_json(marker, {"utc": utc_now(), "boot_id": self.boot_id})
                    result = self.api(run, "prepare")
                    write_json(directory / "binding.json", result["binding"])
                    run = self.update(run, "prepared", "model_saved", binding=result["binding"])
            if run["state"] in {"prepared", "starting", "solving"}:
                binding = read_json(directory / "binding.json")
                offline_built = not run["job"].get("solve", True) and (directory / "solver_saved.json").is_file()
                if not offline_built and not matches_process(binding):
                    raise APIUncertain("Bound CST PID is absent or its creation time changed")
                if run.get("details", {}).get("cancel_requested"):
                    return self.cancel(run)
                if prepare_only and run["state"] == "prepared" and run["job"].get("solve", True):
                    return run
                if run["state"] == "prepared":
                    if not run["job"].get("solve", True):
                        # Building without solving does not use an old result as a new solve.
                        response = self.api(run, "finish")
                        write_json(directory / "result.json", response["result"])
                        return self.update(run, "completed", "built_without_solver", solver_elapsed_seconds=0,
                                           result=response["result"])
                    # Receipt is committed BEFORE the native start. On restart we
                    # verify the last solver state and never repeat start_solver.
                    run = self.update(run, "starting", "start_requested", solver_started_utc=utc_now())
                    write_json(directory / "start_requested.json", {"utc": utc_now(), "boot_id": self.boot_id})
                    self.api(run, "start")
                    run = self.update(run, "solving", "solver_running")
                state = self.api(run, "poll")
                run = self.update(run, run["state"], "solver_running" if state["running"] else "solver_checked",
                                  last_poll=state, safe_reconnect_attempts=0, error=None,
                                  solver_elapsed_seconds=seconds_since(run["details"].get("solver_started_utc")))
                if state["running"]:
                    if run["details"]["solver_elapsed_seconds"] > run["job"]["timeout_seconds"]:
                        run = self.update(run, run["state"], "solver_budget_exceeded", cancel_requested=True,
                                          cancel_reason="single_run_budget")
                        return self.cancel(run)
                    return run
                info = state.get("solver_info") or {}
                if info.get("state") != "SUCCESS":
                    raise APIUncertain(f"Solver is stopped without SUCCESS evidence: {info}")
                if run["state"] == "starting" and not (directory / "start_returned.json").exists():
                    # Fresh generated models contain no preceding solver results.
                    # A backend receipt must establish that SUCCESS belongs to this start.
                    if not state.get("fresh_run_confirmed"):
                        raise APIUncertain("Start acknowledgement missing; cannot bind SUCCESS to this run")
                run = self.update(run, "exporting", "solver_success", solver_info=info,
                                  solver_elapsed_seconds=seconds_since(run["details"].get("solver_started_utc")))
            if run["state"] == "exporting":
                response = self.api(run, "finish")
                write_json(directory / "result.json", response["result"])
                run = self.update(run, "analyzing", "results_saved", result=response["result"])
            if run["state"] == "analyzing":
                from .research_analysis import analyze_run
                analysis = analyze_run(run)
                write_json(directory / "analysis.json", analysis)
                run = self.update(run, "completed", "analysis_completed", analysis=analysis)
                context = self.store.context(run["experiment_id"])
                write_json(self.state / "experiments" / run["experiment_id"] / "context.json", context)
            return run
        except Exception as exc:
            if isinstance(exc, APITimeout) and run["state"] in {"starting", "solving"}:
                retries = run.get("details", {}).get("safe_reconnect_attempts", 0)
                if retries < 2:
                    return self.update(run, run["state"], "reconnecting_without_restart",
                                       safe_reconnect_attempts=retries + 1, error=str(exc))
            if run["state"] == "preparing" and (directory / "prepare_failed_project_closed.json").is_file():
                return self.update(run, "failed", "preparation_failed_project_closed", error=str(exc),
                                   solver_elapsed_seconds=0, ownership_released=True)
            # Native calls may perform their side effect before timing out. Mark
            # uncertainty, retain ownership, and require an explicit recovery request.
            return self.update(run, "needs_attention", "recovery_required", error=str(exc),
                               recovery_state=run["state"], resume_requested=False,
                               solver_elapsed_seconds=seconds_since(run.get("details", {}).get("solver_started_utc")))

    def cancel(self, run: dict) -> dict:
        result = self.api(run, "cancel")
        confirmed = result.get("cancelled")
        if isinstance(confirmed, dict):
            confirmed = confirmed.get("stopped", False)
        if confirmed is not True:
            raise APIUncertain("Cancellation requested but CST stop not confirmed")
        return self.update(run, "cancelled", "solver_stop_confirmed", cancel_requested=False,
                           solver_elapsed_seconds=seconds_since(run.get("details", {}).get("solver_started_utc")))

    def acknowledge_controls(self):
        for control in self.store.controls():
            if control["action"] in {"pause", "resume"}:
                self.store.ack_control(control["control_id"])
            elif not any(run["state"] not in {"completed", "failed", "cancelled"}
                         for run in self.store.runs(control["experiment_id"])):
                self.store.ack_control(control["control_id"])

    def run_forever(self, interval: float = 5):
        with submission_lock(self.state / "runner.lock"):
            try:
                while not (self.state / "runner_stop.request").exists():
                    try:
                        self.tick()
                        self.acknowledge_controls()
                    except Exception as exc:
                        self.heartbeat(error=str(exc))
                        with (self.state / "runner.log").open("a", encoding="utf-8") as out:
                            out.write(f"{utc_now()} {traceback.format_exc()}\n")
                    time.sleep(interval)
            finally:
                self.power(False)
                write_json(self.state / "runner_exit.json", {"utc": utc_now(), "boot_id": self.boot_id,
                           "reason": "runner_stop.request"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    if not 1 <= args.interval <= 60:
        parser.error("interval must be 1..60 seconds")
    try:
        from .event_runner import EventRunner
        EventRunner(args.root).run_forever()
    except BaseException:
        write_json(args.root / ".autocst" / "runner_start_failure.json",
                   {"utc": utc_now(), "traceback": traceback.format_exc()})
        raise


if __name__ == "__main__":
    main()
