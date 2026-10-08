"""Explicit, one-solve MCP integration check against a user-selected CST PID."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


def payload(response):
    if response.isError:
        raise RuntimeError(str(response.content))
    return response.structuredContent or json.loads(response.content[0].text)


async def verify(pid: int):
    dest = ROOT / ".autocst" / "verification"
    dest.mkdir(parents=True, exist_ok=True)
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "autocst.mcp_server", "--root", str(ROOT)], cwd=str(ROOT),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    record = {"started_utc": datetime.now(timezone.utc).isoformat(), "cst_pid": pid,
              "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (ROOT / "autocst").glob("*.py")}}
    with (dest / "mcp-simulation.log").open("a", encoding="utf-8") as log:
        async with stdio_client(params, errlog=log) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                record["protocol"] = init.protocolVersion
                receipt = payload(await session.call_tool("cst_submit_waveguide", {"cst_pid": pid}))
                run_id = receipt["run_id"]
                record["run_id"] = run_id
                target = dest / f"mcp-simulation-{run_id}.json"
                target.write_text(json.dumps(record, indent=2), encoding="utf-8")
                print(json.dumps({"submitted": run_id}), flush=True)
                async with asyncio.timeout(510):
                    previous = None
                    while True:
                        state = payload(await session.call_tool("cst_run_status", {"run_id": run_id}))
                        stage = (state["state"], state.get("phase"))
                        if stage != previous:
                            print(json.dumps({"run_id": run_id, "state": stage[0], "phase": stage[1]}), flush=True)
                            previous = stage
                        if state["state"] not in {"queued", "running"}:
                            break
                        await asyncio.sleep(2)
                record["results"] = payload(await session.call_tool("cst_run_results", {"run_id": run_id}))
                record["passed"] = state["state"] == "completed" and record["results"]["result"]["numerical_check"]["passed"]
                record["finished_utc"] = datetime.now(timezone.utc).isoformat()
                target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"passed": record["passed"], "run_id": run_id, "record": str(target)}), flush=True)
                if not record["passed"]:
                    raise RuntimeError(str(state))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cst-pid", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(verify(args.cst_pid))
