"""Completion-driven CST execution, independent of Codex/MCP connection lifetime."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback

from .process_identity import get_process_identity, matches_process
from .research_runner import Runner, APIUncertain, read_json, seconds_since
from .service import submission_lock, write_json, utc_now
from .signals import Event, ProcessExit, DirectoryChange, event_name, wait, notify


FINAL = {"completed", "failed", "cancelled", "needs_attention"}


class EventRunner(Runner):
    def __init__(self, root: Path, *, api=None):
        super().__init__(root, api=api)
        self.queue_signal = Event(event_name(self.root))
        self.shutdown = threading.Event()
        self.current_run = None
        self.heartbeat_mutex = threading.Lock()
        from .optimization_batch import OptimizationBatches
        self.batches = OptimizationBatches(self.root)

    def heartbeat(self, run=None, error=None):
        with self.heartbeat_mutex:
            super().heartbeat(run, error)

    def health(self):
        while not self.shutdown.is_set():
            try:
                self.heartbeat(self.current_run)
                # Bookkeeping fallback for a sender crashing between DB commit
                # and SetEvent. It never queries CST or delays normal completion.
                active = self.store.active_runs()
                ready_experiments = {item["experiment_id"] for item in self.store.list_experiments()
                                     if item["state"] == "active"}
                ready_queue = not active and any(run["state"] == "queued" and
                                                run["experiment_id"] in ready_experiments
                                                for run in self.store.runs())
                active_control = any(run["details"].get("cancel_requested") or
                                     run["details"].get("resume_requested") for run in active)
                if ((self.state / "runner_stop.request").exists() or self.store.controls() or
                        active_control or (not self.current_run and (ready_queue or (not active and self.batches.has_ready())))):
                    self.queue_signal.set()
                self.replay_completions()
            except Exception:
                with (self.state / "runner.log").open("a", encoding="utf-8") as out:
                    out.write(traceback.format_exc())
            self.shutdown.wait(10)

    def completion(self, run: dict):
        if run["state"] in FINAL:
            notify(self.root, f"run-{run['run_id']}", manual=True)
            # Persistent completion/fault events are also the model's inbox.
            receipt = Path(run["run_directory"]) / "completion_signal.json"
            if receipt.is_file():
                prior = read_json(receipt)
                if prior.get("state") == run["state"] and prior.get("state_updated_utc") == run["updated_utc"]:
                    return
            write_json(receipt,
                       {"utc": utc_now(), "state": run["state"], "run_id": run["run_id"],
                        "state_updated_utc": run["updated_utc"],
                        "signal": event_name(self.root, f"run-{run['run_id']}"),
                        "delivery": "native_event", "solver_polling": False})

    def replay_completions(self):
        # A crash after committing the final DB state but before SetEvent must
        # not strand an already-waiting client. Replaying never starts CST.
        for run in self.store.runs():
            if run["state"] in FINAL and not (run["state"] == "needs_attention" and run["details"].get("resume_requested")):
                receipt = Path(run["run_directory"]) / "completion_signal.json"
                prior = read_json(receipt) if receipt.is_file() else {}
                if prior.get("state") != run["state"] or prior.get("state_updated_utc") != run["updated_utc"]:
                    self.completion(run)
        self.batches.replay_completions()

    def acknowledge_controls(self):
        super().acknowledge_controls()
        # cancel/stop can finalize queued runs without execute_once visiting them.
        self.replay_completions()

    def launch_waiter(self, run: dict) -> dict:
        directory = Path(run["run_directory"])
        calls = directory / "api_calls"
        calls.mkdir(exist_ok=True)
        stem = f"{time.time_ns()}_solve_wait"
        request, response = calls / f"{stem}.request.json", calls / f"{stem}.response.json"
        write_json(request, {"run_directory": str(directory), "job": run["job"], "action": "solve_wait",
                            "phase_implementation_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                                               for p in (self.root / "autocst").glob("*.py")}})
        run = self.update(run, "starting", "blocking_solver_requested", solver_started_utc=utc_now(),
                          wait_source="worker_process", response_path=str(response))
        write_json(directory / "start_requested.json", {"utc": utc_now(), "boot_id": self.boot_id})
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", PYTHONPATH=str(self.root))
        with (calls / f"{stem}.log").open("ab") as log:
            proc = subprocess.Popen([sys.executable, "-m", "autocst.research_action", str(request), str(response)],
                                    cwd=self.root, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        identity = get_process_identity(proc.pid)
        write_json(directory / "solver_wait_spawn.json", {**identity, "response_path": str(response), "utc": utc_now()})
        run = self.update(run, "solving", "awaiting_cst_completion", worker_identity=identity)
        return self.await_worker(run, identity, response, proc)

    def await_worker(self, run: dict, identity: dict, response: Path, proc=None) -> dict:
        if response.is_file():
            return self.accept_response(run, response)
        self.current_run = run
        try:
            worker_signal = ProcessExit(identity["pid"], identity["creation_time"])
        except ProcessLookupError:
            if response.is_file():
                return self.accept_response(run, response)
            return self.recover_by_directory(run)
        try:
            while True:
                run = self.store.run(run["run_id"])
                self.current_run = run
                if (self.state / "runner_stop.request").exists():
                    raise APIUncertain("Runner stopped monitoring; original worker/CST not restarted or killed")
                if run["details"].get("cancel_requested"):
                    cancelled = self.cancel(run)
                    if proc:
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                    return cancelled
                elapsed = seconds_since(run["details"].get("solver_started_utc"))
                remaining = run["job"]["timeout_seconds"] - elapsed
                if remaining <= 0:
                    run = self.update(run, run["state"], "solver_budget_exceeded",
                                      cancel_requested=True, cancel_reason="single_run_budget")
                    continue
                hit = wait([worker_signal, self.queue_signal], remaining)
                if hit == 0:
                    if proc:
                        proc.wait()
                    if response.is_file():
                        return self.accept_response(run, response)
                    return self.recover_by_directory(run)
                # Queue/control signal or deadline: reread durable state; no CST poll.
                self.acknowledge_controls()
        finally:
            worker_signal.close()

    def accept_response(self, run: dict, response: Path) -> dict:
        payload = read_json(response)
        elapsed = seconds_since(run["details"].get("solver_started_utc"))
        if not payload.get("ok") or payload.get("solver_info", {}).get("state") != "SUCCESS" or not payload.get("fresh_run_confirmed"):
            raise APIUncertain(f"Blocking CST completion was not verified: {payload}")
        run = self.update(run, "exporting", "cst_completion_received", solver_info=payload["solver_info"],
                          solver_elapsed_seconds=elapsed, solver_completion_received_utc=utc_now(),
                          wait_source="worker_process", solver_poll_count=0)
        # tick in exporting/analyzing performs only saving/export/analysis, not start or poll.
        return self.tick()

    def recover_by_directory(self, run: dict) -> dict:
        directory = Path(run["run_directory"])
        binding = read_json(directory / "binding.json")
        if not matches_process(binding):
            raise APIUncertain("CST identity lost and no synchronous completion receipt is available")
        project_folder = Path(binding["project_path"]).with_suffix("")
        if not project_folder.is_dir():
            raise APIUncertain("No CST result directory to register recovery notifications")
        change = DirectoryChange(project_folder)
        try:
            run = self.update(run, run["state"], "awaiting_result_directory_event",
                              wait_source="result_directory_recovery")
            self.current_run = run
            # Register before verification so completion cannot fall into a race gap.
            while True:
                state = self.api(run, "poll")  # Recovery only, triggered by registration/change.
                if not state["running"]:
                    if state.get("solver_info", {}).get("state") != "SUCCESS" or not state.get("fresh_run_confirmed"):
                        raise APIUncertain(f"Recovered solver stopped without valid completion: {state}")
                    run = self.update(run, "exporting", "recovered_completion_verified",
                                      solver_info=state["solver_info"],
                                      solver_elapsed_seconds=seconds_since(run["details"].get("solver_started_utc")))
                    return self.tick()
                remaining = run["job"]["timeout_seconds"] - seconds_since(run["details"].get("solver_started_utc"))
                if remaining <= 0:
                    run = self.update(run, run["state"], "solver_budget_exceeded", cancel_requested=True)
                while True:
                    run = self.store.run(run["run_id"])
                    self.current_run = run
                    if (self.state / "runner_stop.request").exists():
                        raise APIUncertain("Runner stopped monitoring; original CST not restarted or killed")
                    if run["details"].get("cancel_requested"):
                        return self.cancel(run)
                    remaining = run["job"]["timeout_seconds"] - seconds_since(run["details"].get("solver_started_utc"))
                    if remaining <= 0:
                        run = self.update(run, run["state"], "solver_budget_exceeded",
                                          cancel_requested=True, cancel_reason="single_run_budget")
                        continue
                    hit = wait([change, self.queue_signal], remaining)
                    if hit == 0:
                        change.rearm()
                        break
                    if hit is None:
                        run = self.update(run, run["state"], "solver_budget_exceeded", cancel_requested=True)
                    self.acknowledge_controls()
        finally:
            change.close()

    def execute_once(self) -> dict | None:
        active = self.store.active_runs()
        run = active[0] if active else self.store.next_run()
        if run is None and self.batches.advance():
            run = self.store.next_run()
        self.current_run = run
        self.power(run is not None)
        if run is None:
            return None
        original_state = run["state"]
        try:
            if original_state in {"preparing", "prepared", "needs_attention", "exporting", "analyzing"}:
                run = self.tick(prepare_only=True)
                if run["state"] in FINAL:
                    self.completion(run)
                    return run
                if run["state"] == "prepared":
                    run = self.launch_waiter(run)
                elif run["state"] in {"starting", "solving"}:
                    return self.execute_once()
            elif original_state in {"starting", "solving"}:
                directory = Path(run["run_directory"])
                receipt_path = run["details"].get("response_path")
                identity = None
                for name in ("solver_wait_process.json", "solver_wait_spawn.json"):
                    if (directory / name).is_file():
                        identity = read_json(directory / name)
                        receipt_path = identity.get("response_path", receipt_path)
                        break
                if identity and receipt_path:
                    run = self.await_worker(run, identity, Path(receipt_path))
                else:
                    run = self.recover_by_directory(run)
            self.completion(run)
            return run
        except Exception as exc:
            run = self.update(run, "needs_attention", "event_recovery_required", error=str(exc),
                              recovery_state=self.store.run(run["run_id"])["state"], resume_requested=False)
            self.completion(run)
            return run

    def run_forever(self):
        with submission_lock(self.state / "runner.lock"):
            self.replay_completions()
            thread = threading.Thread(target=self.health, name="AutoCST-health", daemon=True)
            thread.start()
            try:
                while not (self.state / "runner_stop.request").exists():
                    run = self.execute_once()
                    self.acknowledge_controls()
                    self.current_run = None
                    if run is None or run["state"] == "needs_attention":
                        wait([self.queue_signal])
                    # Terminal runs drain the next queued job immediately.
            finally:
                self.shutdown.set()
                thread.join(timeout=5)
                self.power(False)
                self.queue_signal.close()
                write_json(self.state / "runner_exit.json", {"utc": utc_now(), "boot_id": self.boot_id,
                           "reason": "graceful_stop", "execution_mode": "event_driven"})
