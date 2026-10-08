"""Shared API for CLI and MCP. Each real solve owns an independent process."""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid

from .config import environment
from .manual import search_manual, read_pages
from .models import waveguide_parameters


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def submission_lock(path: Path):
    """OS lock releases on crash; the persistent file is only a lock anchor."""
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another submission currently holds the process lock") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".write-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def normalize_job(job: dict) -> dict:
    if not isinstance(job, dict) or job.get("kind") != "waveguide":
        raise ValueError("v0.1 supports kind='waveguide' only")
    unknown = set(job) - {"kind", "parameters", "timeout_seconds", "solve", "cst_pid"}
    if unknown:
        raise ValueError(f"Unsupported fields: {sorted(unknown)}")
    params = job.get("parameters", {})
    if not isinstance(params, dict):
        raise ValueError("parameters must be an object")
    defaults = waveguide_parameters(params)
    timeout = job.get("timeout_seconds", 300)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 10 <= timeout <= 1800:
        raise ValueError("timeout_seconds must be an integer from 10 to 1800")
    solve = job.get("solve", True)
    if not isinstance(solve, bool):
        raise ValueError("solve must be boolean")
    cst_pid = job.get("cst_pid")
    if cst_pid is not None and (isinstance(cst_pid, bool) or not isinstance(cst_pid, int) or cst_pid <= 0):
        raise ValueError("cst_pid must be a positive integer identifying the explicitly selected instance")
    return {"kind": "waveguide", "parameters": defaults, "timeout_seconds": timeout,
            "solve": solve, "cst_pid": cst_pid}


def process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Service:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.state = self.root / ".autocst"
        self.runs = self.state / "runs"
        self.pdf = self.root / "sources" / "CSTStudioSuite_All_In_One.pdf"

    def doctor(self) -> dict:
        from . import __version__
        return {**environment(), "workspace": str(self.root), "manual_present": self.pdf.is_file(),
                "supported_jobs": ["waveguide", "metasurface", "history", "existing_project"],
                "legacy_submit_jobs": ["waveguide"], "version": __version__,
                "research_entry": "create_experiment -> prepare_simulation -> submit_prepared",
                "resident_runner_status_file": str(self.state / "runner_status.json")}

    def search(self, query: str, limit: int = 5) -> dict:
        return search_manual(query, self.pdf, self.state / "manual", limit=limit)

    def pages(self, start: int, end: int) -> dict:
        return read_pages(self.pdf, self.state / "manual", start, end)

    def run_path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid run_id")
        path = self.runs / run_id
        if not path.is_dir():
            raise FileNotFoundError(f"Unknown run_id: {run_id}")
        return path

    def submit(self, job: dict) -> dict:
        normalized = normalize_job(job)
        self.runs.mkdir(parents=True, exist_ok=True)
        # Process-owned locking recovers automatically after a crashed submitter.
        with submission_lock(self.state / "submit.lock"):
            research_db = self.state / "research.sqlite3"
            if research_db.is_file():
                with sqlite3.connect(research_db) as db:
                    row = db.execute("SELECT run_id,state FROM runs WHERE state NOT IN ('completed','failed','cancelled') LIMIT 1").fetchone()
                if row:
                    raise RuntimeError(f"Research queue owns CST: {row[0]} ({row[1]}); resolve or cancel before legacy submission")
            for prior in self.runs.iterdir():
                if prior.is_dir() and (prior / "status.json").is_file():
                    state = self.status(prior.name)
                    if state["state"] in {"queued", "running", "interrupted", "failed_cleanup"}:
                        raise RuntimeError(f"Unresolved run {prior.name}: {state['state']}; inspect its files before another solve")
            run_id = uuid.uuid4().hex
            run = self.runs / run_id
            run.mkdir()
            write_json(run / "job.json", normalized)
            status = {"run_id": run_id, "state": "queued", "created_utc": utc_now(),
                      "updated_utc": utc_now(), "run_directory": str(run),
                      "job_sha256": hashlib.sha256((run / "job.json").read_bytes()).hexdigest()}
            write_json(run / "status.json", status)
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            code_root = Path(__file__).resolve().parent.parent
            env["PYTHONPATH"] = str(code_root) + os.pathsep + env.get("PYTHONPATH", "")
            try:
                with (run / "worker.log").open("ab") as log:
                    proc = subprocess.Popen([sys.executable, "-m", "autocst.worker", "--root", str(self.root), "--run-id", run_id],
                                            cwd=code_root, env=env, stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=subprocess.STDOUT,
                                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            except Exception as exc:
                status.update(state="failed", error=str(exc), updated_utc=utc_now())
                write_json(run / "status.json", status)
                raise
            # If this receipt fails, do not label a running worker as failed or allow
            # a replacement solve. Its queued/running state remains unresolved.
            write_json(run / "process.json", {"pid": proc.pid, "started_utc": utc_now()})
            return {**status, "worker_pid": proc.pid}

    def status(self, run_id: str) -> dict:
        run = self.run_path(run_id)
        result = json.loads((run / "status.json").read_text(encoding="utf-8"))
        process_file = run / "process.json"
        if process_file.exists():
            pid = json.loads(process_file.read_text(encoding="utf-8"))["pid"]
            result["worker_pid"] = pid
            if result["state"] in {"queued", "running"} and not process_alive(pid):
                result.update(state="interrupted", error="Worker exited without a final receipt. CST state requires inspection.")
        return result

    def results(self, run_id: str) -> dict:
        state = self.status(run_id)
        run = self.run_path(run_id)
        report = run / "result.json"
        return {"status": state, "result": json.loads(report.read_text(encoding="utf-8")) if report.exists() else None}
