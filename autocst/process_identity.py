"""Process identity includes creation time, so a reused PID cannot own a run."""
from __future__ import annotations

import os
import sys


def get_process_identity(pid: int) -> dict:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("PID must be a positive integer")
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return {"pid": pid, "alive": False, "creation_time": None}
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            code = wintypes.DWORD()
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                raise OSError(ctypes.get_last_error(), "Cannot establish process creation time")
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            alive = bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            return {"pid": pid, "alive": alive, "creation_time": str(created)}
        finally:
            kernel.CloseHandle(handle)
    # Linux fallback is used by portable recovery tests, not by the CST adapter.
    try:
        raw = open(f"/proc/{pid}/stat", encoding="utf-8").read()
        created = raw[raw.rfind(")") + 2:].split()[19]
        return {"pid": pid, "alive": True, "creation_time": created}
    except FileNotFoundError:
        return {"pid": pid, "alive": False, "creation_time": None}


def matches_process(binding: dict) -> bool:
    identity = get_process_identity(binding["cst_pid"])
    return identity["alive"] and identity["creation_time"] == str(binding["cst_creation_time"])
