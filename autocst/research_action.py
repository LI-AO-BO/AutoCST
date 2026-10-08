"""One bounded CST API operation; the resident runner owns the wall clock limit."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import traceback

from .service import write_json, utc_now


def execute(request: dict) -> dict:
    from .research_backend import ResearchSession
    directory = Path(request["run_directory"])
    job = request["job"]
    action = request["action"]
    session = ResearchSession(job, directory)
    if action == "prepare":
        return {"binding": session.prepare()}
    binding = json.loads((directory / "binding.json").read_text(encoding="utf-8"))
    # finish may resume offline export after its project has already been saved/closed.
    session.recover(binding)
    if action == "start":
        session.start()
        write_json(directory / "start_returned.json", {"utc": utc_now()})
        return {"started": True}
    if action == "solve_wait":
        return session.solve_wait()
    if action == "poll":
        return session.poll()
    if action == "finish":
        return {"result": session.finish()}
    if action == "cancel":
        return {"cancelled": session.cancel()}
    raise ValueError(f"Unknown bounded action: {action}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    parser.add_argument("response")
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    if request["action"] == "solve_wait":
        import os
        from .process_identity import get_process_identity
        identity = get_process_identity(os.getpid())
        write_json(Path(request["run_directory"]) / "solver_wait_process.json",
                   {**identity, "request_path": args.request, "response_path": args.response,
                    "started_utc": utc_now()})
    try:
        payload = {"ok": True, **execute(request)}
    except Exception as exc:
        payload = {"ok": False, "error": str(exc), "error_type": type(exc).__name__,
                   "traceback": traceback.format_exc()}
    write_json(Path(args.response), payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
