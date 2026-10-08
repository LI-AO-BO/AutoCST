"""Explicit metasurface submission and reconnect verification via official MCP.

submit creates one prepared job and replays the same submission key twice.
It closes that MCP connection before returning the run ID. --wait opens a new
connection, waits on the Windows completion event without status polling, then
reads the result. resume only reads an existing run. Neither
mode creates a CST application, retries a solver, or controls experiment state.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


TERMINAL_OR_ATTENTION = {"completed", "failed", "cancelled", "needs_attention"}
REQUIRED_TOOLS = {
    "cst_experiment_context", "cst_prepare_simulation", "cst_submit_prepared",
    "cst_research_run_status", "cst_research_run_results", "cst_runner_status",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def payload(response):
    if response.isError:
        raise RuntimeError(str(response.content))
    return response.structuredContent if response.structuredContent is not None else json.loads(response.content[0].text)


class Receipts:
    """Each JSON snapshot is immutable; progress is appended to a separate log."""

    def __init__(self, root):
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        self.directory = root / ".autocst" / "verification" / f"research-simulation-{name}"
        self.directory.mkdir(parents=True, exist_ok=False)
        self.files = []

    def save(self, name, value):
        path = self.directory / name
        with path.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        self.files.append({"name": name, "sha256": file_hash(path)})
        return str(path)

    def event(self, value):
        with (self.directory / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"utc": utc_now(), **value}, ensure_ascii=False, allow_nan=False) + "\n")


async def call(session, name, arguments, timeout=60):
    async with asyncio.timeout(timeout):
        return payload(await session.call_tool(name, arguments))


@asynccontextmanager
async def connection(root, receipts, label):
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "autocst.mcp_server", "--root", str(root)],
        cwd=str(root), env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    with (receipts.directory / f"{label}-mcp.log").open("x", encoding="utf-8") as log:
        async with stdio_client(parameters, errlog=log) as (read, write):
            async with ClientSession(read, write) as session:
                async with asyncio.timeout(60):
                    initialized = await session.initialize()
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                if not REQUIRED_TOOLS.issubset(tools):
                    raise RuntimeError(f"Required MCP tools are absent: {REQUIRED_TOOLS-set(tools)}")
                if tools["cst_submit_prepared"].annotations.idempotentHint is not True:
                    raise RuntimeError("Prepared-submission tool does not advertise retry-safe semantics")
                receipts.save(f"{label}-connection.json", {
                    "opened_utc": utc_now(), "protocol_version": initialized.protocolVersion,
                    "server_info": initialized.serverInfo.model_dump(mode="json"),
                    "tool_names": sorted(tools),
                    "submit_schema": tools["cst_submit_prepared"].model_dump(mode="json", exclude_none=True),
                })
                try:
                    yield session
                finally:
                    receipts.event({"event": "mcp_session_exiting", "connection": label})
    receipts.save(f"{label}-closed.json", {"closed_utc": utc_now(), "official_stdio_client_closed": True})


def runner_receipt(status):
    return {key: status.get(key) for key in (
        "pid", "process", "boot_id", "heartbeat_utc", "heartbeat_age_seconds", "alive", "responsive", "phase", "run_id",
    )}


def check_run(receipt, experiment_id):
    if receipt.get("experiment_id") != experiment_id:
        raise ValueError("Returned run belongs to a different experiment")
    if not receipt.get("run_id"):
        raise ValueError("Submission did not return a run_id")


async def submit(args, receipts, known):
    decision = json.loads(args.decision.read_text(encoding="utf-8-sig"))
    if not isinstance(decision, dict):
        raise ValueError("--decision must contain a JSON object")
    receipts.save("01-decision.json", {"source": str(args.decision), "sha256": file_hash(args.decision), "decision": decision})
    async with connection(args.root, receipts, "submit") as session:
        context = await call(session, "cst_experiment_context", {"experiment_id": args.experiment_id})
        receipts.save("02-context-before.json", context)
        experiment = context["experiment"]
        if experiment["experiment_id"] != args.experiment_id:
            raise ValueError("Experiment identity differs from the requested experiment")
        if experiment["state"] != "active":
            raise ValueError("Experiment must already be active; this script does not resume it")
        specification = experiment["spec"]
        if specification.get("model", {}).get("kind") not in (None, "metasurface"):
            raise ValueError("This explicit acceptance script submits metasurface jobs only")
        runner = await call(session, "cst_runner_status", {})
        receipts.save("03-runner-before.json", runner_receipt(runner))
        if not runner.get("alive") or not runner.get("responsive"):
            raise RuntimeError("A responsive existing runner is required; this script does not start it")
        parameters = dict(specification.get("model", {}).get("fixed_parameters", {}))
        parameters.update(patch_mm=args.patch_mm, mesh_steps_per_wavelength=args.mesh_steps)
        job = {"kind": "metasurface", "parameters": parameters, "cst_pid": args.cst_pid,
               "timeout_seconds": specification["budgets"]["max_run_solver_seconds"], "solve": True}
        receipts.save("04-proposed-job.json", job)
        prepared = await call(session, "cst_prepare_simulation", {
            "experiment_id": args.experiment_id, "job": job, "decision": decision,
        })
        known["prepared_id"] = prepared["prepared_id"]
        receipts.save("05-prepared.json", prepared)
        frozen = prepared["job"]
        if prepared["experiment_id"] != args.experiment_id or frozen["kind"] != "metasurface":
            raise ValueError("Prepared experiment or model identity does not match")
        if frozen["cst_pid"] != args.cst_pid or frozen["parameters"]["patch_mm"] != args.patch_mm:
            raise ValueError("Prepared CST identity or patch length does not match")
        if frozen["parameters"]["mesh_steps_per_wavelength"] != args.mesh_steps:
            raise ValueError("Prepared mesh setting does not match")
        if not frozen["solve"] or not frozen.get("cst_creation_time"):
            raise ValueError("Prepared job must bind a specific existing CST process and request a solve")
        history = Path(prepared["review"]["history_file"])
        if file_hash(history) != prepared["history_sha256"]:
            raise ValueError("Prepared history file differs from its review hash")
        receipts.save("06-review-check.json", {
            "passed": True, "cst_pid": args.cst_pid, "cst_creation_time": frozen["cst_creation_time"],
            "parameters": frozen["parameters"], "solver_timeout_seconds": frozen["timeout_seconds"],
            "history_file": str(history), "history_sha256": prepared["history_sha256"],
            "review_scope": "Explicit experiment, selected instance, proposed parameters, frozen history hash; numerical validity requires results",
        })
        submission = {"prepared_id": prepared["prepared_id"], "idempotency_key": args.idempotency_key}
        run_ids = []
        for attempt in range(3):
            # These are protocol receipt replays, never a solver retry or a new key.
            result = await call(session, "cst_submit_prepared", submission)
            receipts.save(f"07-submission-{attempt}.json", result)
            check_run(result, args.experiment_id)
            known["run_id"] = result["run_id"]
            run_ids.append(result["run_id"])
            if result["idempotency_key"] != args.idempotency_key:
                raise ValueError("Returned idempotency key differs from the submitted key")
            if len(set(run_ids)) != 1:
                raise RuntimeError("Same-key replay returned a different run ID; no further submissions will be attempted")
        known["solver_timeout_seconds"] = frozen["timeout_seconds"]
        receipts.save("08-idempotency.json", {
            "passed": True, "initial_submissions": 1, "same_key_replays": 2,
            "prepared_id": prepared["prepared_id"], "idempotency_key": args.idempotency_key, "run_ids": run_ids,
        })
    # This is emitted only after the SDK closes the first MCP child and its pipes.
    receipts.save("09-submitted-and-disconnected.json", {"utc": utc_now(), **known, "submission_connection_closed": True})
    print(json.dumps({"status": "submitted_and_disconnected", **known, "receipt_directory": str(receipts.directory)}, ensure_ascii=False), flush=True)


def boolean_check(value):
    return value.get("passed") if isinstance(value, dict) else value if isinstance(value, bool) else None


async def observe(args, receipts, known):
    async with connection(args.root, receipts, "observe") as session:
        first = await call(session, "cst_research_run_status", {"run_id": known["run_id"]})
        check_run(first, args.experiment_id)
        receipts.save("10-status-after-reconnect.json", first)
        solver_limit = first["job"]["timeout_seconds"]
        maximum_wait = solver_limit + 600
        wait_limit = args.wait_seconds if args.wait_seconds is not None else maximum_wait
        if wait_limit > maximum_wait:
            raise ValueError(f"--wait-seconds must not exceed this run's solver limit + 600 ({maximum_wait}s)")
        state = first
        deadline = time.monotonic() + wait_limit
        if args.wait and state["state"] not in TERMINAL_OR_ATTENTION:
            # The durable run is re-read after registering the manual-reset event,
            # so completion between the MCP status read and registration is safe.
            sys.path.insert(0, str(args.root))
            from autocst.signals import event_name, wait_for_run
            receipts.event({"event": "waiting_for_completion_signal", "run_id": known["run_id"],
                            "event_name": event_name(args.root, f"run-{known['run_id']}"),
                            "wait_limit_seconds": wait_limit, "status_polling": False})
            print(json.dumps({"run_id": known["run_id"], "status": "waiting_for_completion_signal"}), flush=True)
            try:
                state = await asyncio.to_thread(wait_for_run, args.root, known["run_id"], max(0, deadline - time.monotonic()))
            except TimeoutError:
                receipts.save("11-observation-timeout.json", {
                    "utc": utc_now(), "wait_limit_seconds": wait_limit, "last_status": state,
                    "waiting_mechanism": "Windows named event", "status_polling": False,
                    "solver_cancelled": False, "solver_retried": False,
                })
                return {"status": "observation_timeout", "run_id": known["run_id"], "exit_code": 2}
            check_run(state, args.experiment_id)
            if state["run_id"] != known["run_id"] or state["state"] not in TERMINAL_OR_ATTENTION:
                raise RuntimeError("Completion signal did not correspond to this run's durable terminal/attention state")
            receipts.save("11-completion-signal.json", {
                "received_utc": utc_now(), "waiting_mechanism": "Windows named event",
                "status_polling": False, "durable_run": state,
            })

        async def read_result(name, arguments):
            remaining = deadline - time.monotonic() if args.wait else 60
            if remaining <= 0:
                raise TimeoutError("Observation deadline exceeded; the solver was not cancelled or retried")
            return await call(session, name, arguments, min(60, remaining))

        results = await read_result("cst_research_run_results", {"run_id": known["run_id"]})
        context = await read_result("cst_experiment_context", {"experiment_id": args.experiment_id})
        runner = await read_result("cst_runner_status", {})
        receipts.save("12-results.json", results)
        receipts.save("13-context-after.json", context)
        receipts.save("14-runner-after.json", runner_receipt(runner))
        run_ids = {run["run_id"] for run in context["runs"]}
        if known["run_id"] not in run_ids:
            raise ValueError("Run is missing from its recovered experiment context")
        final = results["status"]
        check_run(final, args.experiment_id)
        if final["run_id"] != known["run_id"]:
            raise ValueError("Result receipt belongs to a different run")
        evidence, analysis = results.get("result") or {}, results.get("analysis") or {}
        numerical = boolean_check(evidence.get("numerical_validity"))
        integrity = boolean_check(evidence.get("data_integrity"))
        engineering_passed = final["state"] == "completed" and evidence.get("solver_success") is True and integrity is True and numerical is not False
        outcome = {
            "status": final["state"], "run_id": known["run_id"],
            "solver_success": evidence.get("solver_success"), "data_integrity_passed": integrity,
            "numerical_check_passed": numerical, "single_mesh_target_met": analysis.get("target_met"),
            "target_status": analysis.get("target_status"),
            "execution_and_data_checks_passed": engineering_passed,
            "scientific_boundary": "Target achievement, mesh convergence and physical validation are separate conclusions",
            "exit_code": 0 if engineering_passed or (not args.wait and final["state"] not in TERMINAL_OR_ATTENTION) else 1,
        }
        receipts.save("15-observation-outcome.json", outcome)
        return outcome


async def verify(args):
    receipts = Receipts(args.root)
    known = {"experiment_id": args.experiment_id}
    if args.run_id:
        known["run_id"] = args.run_id
    receipts.save("00-invocation.json", {
        "started_utc": utc_now(), "mode": args.mode, "workspace": str(args.root),
        "experiment_id": args.experiment_id, "cst_pid": args.cst_pid, "patch_mm": args.patch_mm,
        "mesh_steps": args.mesh_steps, "idempotency_key": args.idempotency_key, "wait_requested": args.wait,
        "mcp_sdk_version": importlib.metadata.version("mcp"),
        "source_sha256": {str(path.relative_to(args.root)): file_hash(path) for path in (args.root / "autocst").glob("*.py")},
        "verifier_sha256": file_hash(__file__), "solver_retry_permitted": False,
    })
    try:
        if args.mode == "submit":
            await submit(args, receipts, known)
        if args.mode == "resume" or args.wait:
            outcome = await observe(args, receipts, known)
        else:
            outcome = {"status": "submitted", "run_id": known["run_id"], "exit_code": 0,
                       "completion_verified": False, "same_key_replay_verified": True}
    except Exception as exc:
        outcome = {"status": "verification_error", "error": f"{type(exc).__name__}: {exc}", **known, "exit_code": 1,
                   "boundary": "A recorded submission may still be running; inspect receipts/context before any further submission"}
        receipts.save("98-error.json", outcome)
    receipts.save("99-receipt-manifest.json", {"finished_utc": utc_now(), "outcome": outcome, "receipts": receipts.files})
    print(json.dumps({**outcome, "receipt_directory": str(receipts.directory)}, ensure_ascii=False), flush=True)
    return outcome["exit_code"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("submit", "resume"), nargs="?", default="submit")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--cst-pid", type=int)
    parser.add_argument("--patch-mm", type=float)
    parser.add_argument("--mesh-steps", type=int, default=10)
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--idempotency-key")
    parser.add_argument("--run-id", help="Required for resume; this mode never submits")
    parser.add_argument("--wait", action="store_true", help="Wait on the Windows completion event, then read results through a new MCP connection; no status polling")
    parser.add_argument("--wait-seconds", type=float, help="Optional observation deadline, at most solver timeout + 600")
    args = parser.parse_args()
    args.root = args.root.expanduser().resolve()
    if args.mode == "submit":
        if any(value is None for value in (args.cst_pid, args.patch_mm, args.decision, args.idempotency_key)):
            parser.error("submit requires --cst-pid, --patch-mm, --decision and --idempotency-key")
        if args.cst_pid <= 0 or not math.isfinite(args.patch_mm) or args.patch_mm <= 0:
            parser.error("--cst-pid and --patch-mm must be positive, finite values")
        if not 8 <= args.mesh_steps <= 40:
            parser.error("--mesh-steps must be between 8 and 40")
        if not args.idempotency_key.strip() or len(args.idempotency_key) > 256:
            parser.error("--idempotency-key must contain 1 to 256 characters")
        if args.run_id:
            parser.error("--run-id is for resume; submission creates a new run or recovers its same-key receipt")
        args.decision = args.decision.expanduser().resolve()
        if not args.decision.is_file():
            parser.error("--decision file does not exist")
    elif not args.run_id:
        parser.error("resume requires --run-id")
    if args.wait_seconds is not None and (not math.isfinite(args.wait_seconds) or args.wait_seconds <= 0):
        parser.error("--wait-seconds must be positive and finite")
    return asyncio.run(verify(args))


if __name__ == "__main__":
    raise SystemExit(main())
