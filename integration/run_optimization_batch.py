"""Start or reconnect a durable batch through MCP; wait on its native completion event."""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import json
import math
import os
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autocst.signals import wait_for_batch
from autocst.service import write_json, utc_now
from verify_research_simulation import Receipts, file_hash


def payload(response):
    if response.isError:
        raise RuntimeError(str(response.content))
    return response.structuredContent if response.structuredContent is not None else json.loads(response.content[0].text)


async def request(session, name, arguments, args, receipts, label):
    started = time.monotonic()
    receipts.event({"event": "mcp_request_started", "tool": name, "label": label,
                    "timeout_seconds": args.mcp_request_timeout})
    try:
        async with asyncio.timeout(args.mcp_request_timeout):
            result = payload(await session.call_tool(name, arguments))
    except BaseException as exc:
        # Keep the original request failure before stdio cleanup can report a
        # secondary BrokenResourceError and obscure its diagnostic cause.
        receipts.save(f"{label}-request-fault.json", {"utc": utc_now(), "tool": name,
                      "error_type": type(exc).__name__, "error": repr(exc),
                      "elapsed_seconds": time.monotonic() - started})
        raise
    receipts.event({"event": "mcp_request_completed", "tool": name, "label": label,
                    "elapsed_seconds": time.monotonic() - started})
    return result


async def call_session(args, receipts, *, start):
    label = "start" if start else "read"
    stderr = receipts.directory / f"{label}-mcp.stderr.log"
    # Diagnostics stay on stderr; stdout remains the MCP protocol. Each child
    # has a short request lifetime, separate from the multi-hour native wait.
    bootstrap = ("import faulthandler,sys; faulthandler.enable(); "
                 "faulthandler.dump_traceback_later(float(sys.argv.pop(1)),repeat=True); "
                 "from autocst.mcp_server import main; main()")
    params = StdioServerParameters(command=sys.executable,
        args=["-c", bootstrap, str(args.mcp_stack_after_seconds), "--root", str(args.root)],
        cwd=str(args.root), env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    try:
        with stderr.open("x", encoding="utf-8") as log:
            async with stdio_client(params, errlog=log) as (reader, writer):
                async with ClientSession(reader, writer,
                                         read_timeout_seconds=timedelta(seconds=args.mcp_request_timeout)) as session:
                    try:
                        async with asyncio.timeout(args.mcp_request_timeout):
                            await session.initialize()
                    except BaseException as exc:
                        receipts.save(f"{label}-initialize-fault.json", {"utc": utc_now(),
                                      "error_type": type(exc).__name__, "error": repr(exc)})
                        raise
                    if start:
                        job = json.loads(args.job.read_text(encoding="utf-8-sig"))
                        proposal = {"experiment_id": args.experiment_id, "job_template": job,
                                    "max_new_runs": args.max_new_runs, "config": {"seed": args.seed},
                                    "idempotency_key": args.idempotency_key}
                        receipts.save("01-request.json", proposal)
                        first = await request(session, "cst_start_optimization", proposal, args, receipts, "02-start")
                        receipts.save("02-start.json", first)
                        if args.verify_retries:
                            for number in (3, 4):
                                replay = await request(session, "cst_start_optimization", proposal, args, receipts,
                                                       f"0{number}-same-key-replay")
                                receipts.save(f"0{number}-same-key-replay.json", replay)
                                if replay["batch_id"] != first["batch_id"]:
                                    raise AssertionError("Same-key retry created a different optimization batch")
                        return first
                    batch = await request(session, "cst_optimization_status", {"batch_id": args.batch_id},
                                          args, receipts, "10-batch-result")
                    receipts.save("10-batch-result.json", batch)
                    context = await request(session, "cst_experiment_context", {"experiment_id": batch["experiment_id"]},
                                            args, receipts, "11-experiment-context")
                    receipts.save("11-experiment-context.json", context)
                    return batch
    except BaseException as exc:
        receipts.save(f"{label}-session-fault.json", {"utc": utc_now(), "error_type": type(exc).__name__,
                      "error": repr(exc), "stderr": str(stderr)})
        raise
    finally:
        if stderr.is_file():
            receipts.files.append({"name": stderr.name, "sha256": file_hash(stderr)})


async def run(args):
    receipts = Receipts(args.root)
    receipts.save("00-invocation.json", {"utc": utc_now(), "batch_id": args.batch_id,
                  "mode": "resume" if args.batch_id else "start", "completion_wait": "native_event",
                  "mcp_request_timeout_seconds": args.mcp_request_timeout,
                  "mcp_stack_after_seconds": args.mcp_stack_after_seconds,
                  "implementation_sha256": {p.name: file_hash(p) for p in (args.root / "autocst").glob("*.py")}})
    if not args.batch_id:
        started = await call_session(args, receipts, start=True)
        args.batch_id = started["batch_id"]
        print(json.dumps({"batch_id": args.batch_id, "state": started["state"],
                          "receipt_directory": str(receipts.directory)}, ensure_ascii=False), flush=True)
    if args.wait:
        # No MCP or CST status polling while solving. The independent runner
        # drives all prepared jobs, exporting and feedback before signalling.
        await asyncio.to_thread(wait_for_batch, args.root, args.batch_id, args.wait_seconds)
    batch = await call_session(args, receipts, start=False)
    receipts.save("99-completed.json", {"utc": utc_now(), "batch_id": args.batch_id,
                  "state": batch["state"], "stop_reason": batch["stop_reason"], "new_run_count": len(batch["run_ids"]),
                  "receipts": receipts.files})
    print(json.dumps({"batch_id": args.batch_id, "state": batch["state"], "stop_reason": batch["stop_reason"],
                      "new_run_count": len(batch["run_ids"]), "runs": batch["runs"],
                      "receipt_directory": str(receipts.directory)}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--experiment-id")
    parser.add_argument("--job", type=Path)
    parser.add_argument("--idempotency-key")
    parser.add_argument("--batch-id", help="Reconnect only; never start a new batch")
    parser.add_argument("--max-new-runs", type=int, default=4)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--verify-retries", action="store_true")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--wait-seconds", type=float, default=86400)
    parser.add_argument("--mcp-request-timeout", type=float, default=60,
                        help="Deadline for each short MCP request; independent of the native completion wait")
    parser.add_argument("--mcp-stack-after-seconds", type=float, default=5,
                        help="Write child Python stacks to its diagnostic stderr log after this delay")
    args = parser.parse_args()
    if not args.batch_id and any(value is None for value in (args.experiment_id, args.job, args.idempotency_key)):
        parser.error("Starting requires --experiment-id, --job and --idempotency-key")
    for name in ("mcp_request_timeout", "mcp_stack_after_seconds"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be finite and positive")
    args.root = args.root.resolve()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
