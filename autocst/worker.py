"""One isolated CST operation; durable events and final receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import traceback

from .service import Service, utc_now, write_json


def execute(root: Path, run_id: str) -> int:
    run = Service(root).run_path(run_id)
    # A run ID is a one-shot namespace. Keep this marker after both success and
    # failure so a repeated CLI invocation cannot overwrite its evidence.
    try:
        with (run / "execution.claim").open("x", encoding="utf-8") as claim:
            claim.write(utc_now())
    except FileExistsError:
        raise RuntimeError("This run was already claimed; submit a new job for a new run ID")
    status = json.loads((run / "status.json").read_text(encoding="utf-8"))
    job = json.loads((run / "job.json").read_text(encoding="utf-8"))

    def emit(event: str, data: dict) -> None:
        with (run / "events.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps({"utc": utc_now(), "event": event, "data": data}, ensure_ascii=False, default=str) + "\n")
        status.update(state="running", phase=event, updated_utc=utc_now())
        write_json(run / "status.json", status)

    emit("worker_started", {})
    try:
        from .cst_backend import run_cst
        result = run_cst(job, run, emit)
        write_json(run / "result.json", result)
        # Hash only review artifacts; CST project directories can be large.
        artifacts = {str(p.relative_to(run)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in run.iterdir() if p.is_file() and p.suffix in {".csv", ".bas", ".vba", ".json"}
                     and p.name not in {"status.json", "process.json"}}
        status.update(state="completed" if job["solve"] else "built", artifacts_sha256=artifacts,
                      updated_utc=utc_now(), phase="finished",
                      evidence=("CST solver and export receipt; physical validation is reported separately"
                                if job["solve"] else "Model creation only; no solver was run"))
        write_json(run / "status.json", status)
        return 0
    except Exception as exc:
        traceback.print_exc()
        status.update(state="failed_cleanup" if getattr(exc, "cleanup_required", False) else "failed",
                      error=f"{type(exc).__name__}: {exc}", updated_utc=utc_now())
        write_json(run / "status.json", status)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--execute", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.execute:
        return execute(args.root, args.run_id)
    run = Service(args.root).run_path(args.run_id)
    job = json.loads((run / "job.json").read_text(encoding="utf-8"))
    # Bound even native API calls that never return (startup/license/save dialogs).
    # An expired deadline terminates our Python child only: CST ownership must
    # be inspected afterward, so interrupted stays blocking for subsequent runs.
    deadline = job["timeout_seconds"] + 180
    try:
        completed = subprocess.run([sys.executable, "-m", "autocst.worker", "--root", str(args.root),
                                    "--run-id", args.run_id, "--execute"],
                                   stdin=subprocess.DEVNULL, timeout=deadline)
        return completed.returncode
    except subprocess.TimeoutExpired:
        status = json.loads((run / "status.json").read_text(encoding="utf-8"))
        status.update(state="interrupted", updated_utc=utc_now(),
                      error=f"Task exceeded {deadline}s total deadline. Python worker stopped; CST closure is not confirmed.",
                      cleanup_required=True)
        write_json(run / "status.json", status)
        return 124


if __name__ == "__main__":
    raise SystemExit(main())
