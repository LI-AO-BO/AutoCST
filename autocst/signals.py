"""Windows kernel notifications: wakes are immediate hints; SQLite remains durable."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
from pathlib import Path
import sys
import time


def _kernel():
    if sys.platform != "win32":
        raise RuntimeError("Native event runner currently requires Windows")
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    lib.CreateEventW.restype = wintypes.HANDLE
    lib.SetEvent.argtypes = [wintypes.HANDLE]
    lib.ResetEvent.argtypes = [wintypes.HANDLE]
    lib.CloseHandle.argtypes = [wintypes.HANDLE]
    lib.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD]
    lib.WaitForMultipleObjects.restype = wintypes.DWORD
    lib.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    lib.OpenProcess.restype = wintypes.HANDLE
    lib.FindFirstChangeNotificationW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD]
    lib.FindFirstChangeNotificationW.restype = wintypes.HANDLE
    lib.FindNextChangeNotification.argtypes = [wintypes.HANDLE]
    lib.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
    return lib


def event_name(root: Path, suffix: str = "queue") -> str:
    key = hashlib.sha256(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:12]
    return f"Local\\AutoCST-{key}-{suffix}"


class Event:
    def __init__(self, name: str, *, manual: bool = False):
        self.lib = _kernel()
        self.handle = self.lib.CreateEventW(None, manual, False, name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def set(self):
        if not self.lib.SetEvent(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def reset(self):
        if not self.lib.ResetEvent(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.lib.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def notify(root: Path, suffix: str = "queue", *, manual: bool = False):
    if sys.platform == "win32":
        with Event(event_name(root, suffix), manual=manual) as event:
            event.set()


class ProcessExit:
    def __init__(self, pid: int, creation_time: str):
        from .process_identity import get_process_identity
        self.lib = _kernel()
        self.handle = self.lib.OpenProcess(0x100000 | 0x1000, False, pid)
        if not self.handle:
            raise ProcessLookupError(pid)
        identity = get_process_identity(pid)
        if identity["creation_time"] != creation_time:
            self.close()
            raise RuntimeError("Worker PID identity changed")

    def close(self):
        if self.handle:
            self.lib.CloseHandle(self.handle)
            self.handle = None


class DirectoryChange:
    def __init__(self, path: Path):
        self.lib = _kernel()
        # Only the CST project folder, not our own receipt/status files.
        self.handle = self.lib.FindFirstChangeNotificationW(str(path), True, 0x1 | 0x8 | 0x10)
        if self.handle in (None, ctypes.c_void_p(-1).value):
            self.handle = None
            raise ctypes.WinError(ctypes.get_last_error())

    def rearm(self):
        if not self.lib.FindNextChangeNotification(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.lib.FindCloseChangeNotification(self.handle)
            self.handle = None


def wait(handles: list, timeout_seconds: float | None = None) -> int | None:
    """Return the signalled index; timeout is a deadline, never a polling interval."""
    raw = (wintypes.HANDLE * len(handles))(*(item.handle for item in handles))
    timeout = 0xFFFFFFFF if timeout_seconds is None else max(0, min(int(timeout_seconds * 1000), 0xFFFFFFFE))
    result = _kernel().WaitForMultipleObjects(len(handles), raw, False, timeout)
    if result == 258:
        return None
    if result == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    if not 0 <= result < len(handles):
        raise RuntimeError(f"Unexpected wait result: {result}")
    return int(result)


def wait_for_run(root: Path, run_id: str, timeout_seconds: float | None = None) -> dict:
    """Wait for a final state; resumed attention signals cannot end a fresh wait."""
    from .research_store import ResearchStore
    store = ResearchStore(Path(root) / ".autocst")
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    with Event(event_name(root, f"run-{run_id}"), manual=True) as event:
        while True:
            # Reset before reading durable state: a completion between reset and
            # the DB read is observed there, or leaves the event signalled.
            event.reset()
            run = store.run(run_id)
            if run["state"] in {"completed", "failed", "cancelled"} or (
                    run["state"] == "needs_attention" and not run["details"].get("resume_requested")):
                return run
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if wait([event], remaining) is None:
                raise TimeoutError("Completion event deadline exceeded; no solver was restarted")


def wait_for_batch(root: Path, batch_id: str, timeout_seconds: float | None = None) -> dict:
    """A short-lived frontend may block natively while the resident runner works."""
    from .optimization_batch import OptimizationBatches
    batches = OptimizationBatches(root)
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    with Event(event_name(root, f"batch-{batch_id}"), manual=True) as event:
        while True:
            event.reset()
            batch = batches.status(batch_id)
            if batch["state"] in {"completed", "stopped", "needs_attention"}:
                return batch
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if wait([event], remaining) is None:
                raise TimeoutError("Optimization batch observation deadline exceeded; no simulation was restarted")
