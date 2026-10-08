"""Installation discovery without starting applications or changing settings."""

from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import sys


def find_cst() -> Path | None:
    candidates = []
    for key in ("AUTOCST_CST_ROOT", "CST_STUDIO_SUITE_LINK_INSTALLPATH_2025", "CST_INSTALLPATH_2025"):
        if os.environ.get(key):
            candidates.append(Path(os.environ[key]))
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\WOW6432Node\CST AG\CST DESIGN ENVIRONMENT\2025") as key:
                candidates.append(Path(winreg.QueryValueEx(key, "INSTALLPATH")[0]))
        except OSError:
            pass
    for key, default in (("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                         ("ProgramFiles", r"C:\Program Files")):
        candidates.append(Path(os.environ.get(key, default)) / "CST Studio Suite 2025")
    for path in candidates:
        if (path / "CST DESIGN ENVIRONMENT.exe").is_file():
            return path.resolve()
    return None


def environment() -> dict:
    root = find_cst()
    result = {"platform": platform.platform(), "python": sys.executable,
              "python_version": platform.python_version(), "cst_root": str(root) if root else None,
              "matlab_executable": shutil.which("matlab"), "cst_import_ok": False,
              "license_checked": False, "solver_checked": False}
    if root:
        patch = root / "Patch_Version"
        result["cst_patch"] = patch.read_text(errors="replace").splitlines() if patch.exists() else []
        libraries = root / "AMD64" / "python_cst_libraries"
        result["python_library"] = str(libraries)
        sys.path.insert(0, str(libraries))
        try:
            import cst.interface
            result["cst_import_ok"] = True
            result["interface_path"] = cst.interface.__file__
            result["connectable_cst_pids"] = list(cst.interface.running_design_environments())
        except (ImportError, OSError, RuntimeError) as exc:
            result["import_error"] = str(exc)
        finally:
            sys.path.remove(str(libraries))
    return result
