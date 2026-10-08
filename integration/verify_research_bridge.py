"""Read-only MCP reconnect verification; never install/start/control/submit a job.

The verifier starts only its own stdio MCP children and closes each through the
official SDK. The resident runner must already exist for independence evidence.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


LEGACY_TOOLS = {
    "cst_environment", "cst_search_manual", "cst_read_manual_pages",
    "cst_submit_waveguide", "cst_run_status", "cst_run_results",
}
RESEARCH_READS = {
    "cst_experiment_context", "cst_research_run_status", "cst_research_run_results",
    "cst_completion_events", "cst_runner_status", "cst_optimization_status",
}
RESEARCH_WRITES = {
    "cst_create_experiment", "cst_revise_experiment", "cst_prepare_simulation",
    "cst_submit_prepared", "cst_acknowledge_event", "cst_control_experiment", "cst_start_runner",
    "cst_start_optimization", "cst_control_optimization",
}
EXPECTED_TOOLS = LEGACY_TOOLS | RESEARCH_READS | RESEARCH_WRITES


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def payload(response):
    if response.isError:
        raise RuntimeError(str(response.content))
    return response.structuredContent if response.structuredContent is not None else json.loads(response.content[0].text)


def validate_registry(tools):
    found = {tool.name: tool for tool in tools}
    if set(found) != EXPECTED_TOOLS:
        raise AssertionError(f"Unexpected tool registry: missing={EXPECTED_TOOLS-set(found)}, extra={set(found)-EXPECTED_TOOLS}")
    for name in RESEARCH_READS:
        assert found[name].annotations.readOnlyHint is True, name
    for name in RESEARCH_WRITES:
        assert found[name].annotations.readOnlyHint is False, name
    assert found["cst_submit_prepared"].annotations.idempotentHint is True
    assert found["cst_submit_waveguide"].annotations.idempotentHint is False
    assert set(found["cst_submit_prepared"].inputSchema["required"]) == {"prepared_id", "idempotency_key"}
    assert found["cst_acknowledge_event"].inputSchema["properties"]["event_id"]["type"] == "integer"
    assert found["cst_control_experiment"].inputSchema["properties"]["action"]["enum"] == ["pause", "resume", "cancel"]
    assert all(tool.annotations.openWorldHint is False for tool in tools)
    schema = {name: tool.model_dump(mode="json", exclude_none=True) for name, tool in found.items()}
    return {"passed": True, "count": len(found), "names": sorted(found), "schema_sha256": digest(schema), "schema": schema}


def context_receipt(context):
    experiment = context["experiment"]
    immutable_runs = {}
    states = {}
    for run in context["runs"]:
        immutable_runs[run["run_id"]] = digest({key: run.get(key) for key in (
            "experiment_id", "spec_version", "spec", "job", "decision", "request_sha256", "idempotency_key",
        )})
        states[run["run_id"]] = {key: run.get(key) for key in ("state", "phase", "updated_utc")}
    return {"experiment_id": experiment["experiment_id"], "spec_version": experiment["version"],
            "spec_sha256": digest(experiment["spec"]), "immutable_runs": immutable_runs,
            "run_states": states, "event_cursor": context["event_cursor"], "budget": context["budget"]}


def event_receipt(events):
    return {str(event["event_id"]): digest({key: value for key, value in event.items()
                                         if key != "acknowledged_utc"}) for event in events}


def runner_identity(status):
    return {"pid": status.get("pid"), "boot_id": status.get("boot_id"),
            "creation_time": status.get("process", {}).get("creation_time")}


async def snapshot(args, log):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "autocst.mcp_server", "--root", str(args.root)],
        cwd=str(args.root), env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    result = {"opened_utc": utc_now(), "called_tools": []}
    async with stdio_client(params, errlog=log) as (read, write):
        async with ClientSession(read, write) as session:
            async with asyncio.timeout(60):
                init = await session.initialize()
                result["protocol_version"] = init.protocolVersion
                result["registry"] = validate_registry((await session.list_tools()).tools)

                async def call(name, arguments):
                    result["called_tools"].append(name)
                    return payload(await session.call_tool(name, arguments))

                result["environment"] = await call("cst_environment", {})
                # Avoid implicitly initializing an unconfigured research database.
                if (args.root / ".autocst" / "research.sqlite3").is_file():
                    result["runner"] = await call("cst_runner_status", {})
                    events = await call("cst_completion_events", {"experiment_id": args.experiment_id, "after": 0})
                    result["events"] = event_receipt(events["events"])
                    if args.experiment_id:
                        context = await call("cst_experiment_context", {"experiment_id": args.experiment_id})
                        result["context"] = context_receipt(context)
                else:
                    if args.experiment_id:
                        raise FileNotFoundError("Research database is absent; cannot read the requested experiment")
                    result["runner"] = {"alive": False, "reason": "Research database absent; read-only verification did not create it"}
    result["closed_utc"] = utc_now()
    return result


async def observe_while_mcp_closed(args, first):
    baseline = first["runner"]
    if not baseline.get("alive") or not baseline.get("responsive"):
        return {"status": "not_verified", "reason": "No responsive existing runner; verifier does not start it"}
    deadline = time.monotonic() + args.closed_seconds
    latest = None
    advanced = False
    transient_read_errors = []
    closed_at = datetime.fromisoformat(first["closed_utc"])
    sys.path.insert(0, str(args.root))
    from autocst.signals import DirectoryChange, wait
    change = DirectoryChange(args.root / ".autocst")
    try:
        while time.monotonic() < deadline:
            path = args.root / ".autocst" / "runner_status.json"
            if path.is_file():
                try:
                    latest = json.loads(path.read_text(encoding="utf-8"))
                except (PermissionError, FileNotFoundError) as exc:
                    # A directory notification can arrive while Windows is still
                    # completing the runner's atomic replacement. Wait for the
                    # next filesystem event instead of adding a timed retry.
                    transient_read_errors.append({"utc": utc_now(), "error": type(exc).__name__})
                else:
                    if runner_identity(latest) != runner_identity(baseline):
                        raise AssertionError("Runner identity changed while MCP was closed")
                    advanced = datetime.fromisoformat(latest["heartbeat_utc"]) > closed_at
                    if advanced:
                        break
            if await asyncio.to_thread(wait, [change], max(0, deadline - time.monotonic())) is None:
                break
            # Rearm before the durable file read, avoiding a lost second write.
            change.rearm()
    finally:
        change.close()
    return {"status": "passed" if advanced else "failed", "mcp_closed_utc": first["closed_utc"],
            "runner_identity": runner_identity(baseline), "heartbeat_before": baseline.get("heartbeat_utc"),
            "heartbeat_after_close": latest.get("heartbeat_utc") if latest else None,
            "transient_read_errors": transient_read_errors,
            "observation_method": "Windows directory-change notification then runner_status.json read; no live verifier MCP connection or interval polling"}


def compare_snapshots(first, second, require_context):
    assert first["registry"]["schema_sha256"] == second["registry"]["schema_sha256"], "Tool schema changed across reconnect"
    for event_id, receipt in first.get("events", {}).items():
        assert second.get("events", {}).get(event_id) == receipt, f"Event {event_id} missing or changed"
    result = {"schema_restored": True, "events_preserved": "events" in first}
    if require_context:
        old, new = first["context"], second["context"]
        for name in ("experiment_id", "spec_version", "spec_sha256"):
            assert old[name] == new[name], f"Experiment {name} changed across reconnect"
        for run_id, receipt in old["immutable_runs"].items():
            assert new["immutable_runs"].get(run_id) == receipt, f"Run inputs {run_id} missing or changed"
        assert new["event_cursor"] >= old["event_cursor"], "Event cursor regressed"
        result["experiment_context_preserved"] = True
        result["preserved_run_count"] = len(old["immutable_runs"])
        result["preserved_run_ids"] = sorted(old["immutable_runs"])
        result["new_run_ids"] = sorted(set(new["immutable_runs"]) - set(old["immutable_runs"]))
        result["normal_state_changes"] = {
            run_id: {"before": old["run_states"][run_id], "after": new["run_states"].get(run_id)}
            for run_id in old["immutable_runs"]
            if old["run_states"][run_id] != new["run_states"].get(run_id)
        }
        result["comparison_scope"] = "Frozen experiment specification and prior run IDs/specifications/jobs/decisions; state and budget may progress normally"
    else:
        result["experiment_context_preserved"] = "not_requested"
    return result


async def verify(args):
    record = {"started_utc": utc_now(), "mode": "read_only", "workspace": str(args.root),
              "experiment_id": args.experiment_id, "mcp_sdk_version": importlib.metadata.version("mcp"),
              "actions_not_performed": ["create", "revise", "prepare", "submit", "acknowledge", "control", "install_runner", "start_runner"],
              "idempotency": {"schema_hint_checked": True, "same_key_behavior_verified": False,
                              "reason": "Replaying submissions is excluded from this read-only verification"},
              "source_sha256": {name: hashlib.sha256((args.root / name).read_bytes()).hexdigest()
                                for name in ("autocst/mcp_server.py", "autocst/research_service.py")}}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.report.with_suffix(".log").open("w", encoding="utf-8") as log:
            record["first_session"] = await snapshot(args, log)
            record["independence"] = await observe_while_mcp_closed(args, record["first_session"])
            record["second_session"] = await snapshot(args, log)
        record["recovery"] = compare_snapshots(record["first_session"], record["second_session"], bool(args.experiment_id))
        if record["independence"]["status"] == "passed":
            before, after = record["first_session"]["runner"], record["second_session"]["runner"]
            assert after.get("alive") and after.get("responsive"), "Runner not responsive after reconnect"
            assert runner_identity(before) == runner_identity(after), "Runner restarted across MCP reconnect"
        elif args.require_runner or record["independence"]["status"] == "failed":
            raise AssertionError("Runner independence was not verified")
        record["status"] = "passed" if record["independence"]["status"] == "passed" else "partial"
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["finished_utc"] = utc_now()
    args.report.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": record["status"], "report": str(args.report)}, ensure_ascii=False))
    return 1 if record["status"] == "failed" else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--experiment-id", help="Read an existing experiment across two MCP connections")
    parser.add_argument("--closed-seconds", type=float, default=12, help="Maximum wait for heartbeat advancement after MCP closes")
    parser.add_argument("--require-runner", action="store_true", help="Fail if an existing responsive runner cannot be verified")
    parser.add_argument("--report", type=Path, help="New JSON report path; existing reports are not overwritten")
    args = parser.parse_args()
    args.root = args.root.expanduser().resolve()
    if not 1 <= args.closed_seconds <= 60:
        parser.error("--closed-seconds must be between 1 and 60")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    args.report = (args.report or args.root / ".autocst" / "verification" / f"research-bridge-{timestamp}.json").resolve()
    if args.report.exists():
        parser.error("--report already exists; choose a new report path")
    return asyncio.run(verify(args))


if __name__ == "__main__":
    raise SystemExit(main())
