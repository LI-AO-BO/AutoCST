"""CST 2025 adapter restricted to an owned instance and a bounded model template."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Callable

from .config import find_cst
from .models import waveguide_parameters, waveguide_history, validate_waveguide


class CSTCleanupRequired(RuntimeError):
    """The owned CST instance could not be confirmed closed; preserve the job lock."""
    cleanup_required = True


class CSTLaunchPermissionError(RuntimeError):
    """Windows requires an elevated CST process but this worker is not elevated."""


def _json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def launch_permission_diagnostic(root: Path) -> dict:
    """Read only: detect known Windows compatibility elevation requirements."""
    result = {"administrator_required": False, "worker_elevated": None,
              "blocked": False, "checked": sys.platform == "win32"}
    if sys.platform != "win32":
        return result
    import ctypes
    import winreg
    result["worker_elevated"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    executable = root / "AMD64" / "CST DESIGN ENVIRONMENT_AMD64.exe"
    layers = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
    for hive_name, hive in (("HKCU", winreg.HKEY_CURRENT_USER), ("HKLM", winreg.HKEY_LOCAL_MACHINE)):
        try:
            with winreg.OpenKey(hive, layers) as key:
                value, _ = winreg.QueryValueEx(key, str(executable))
            if "RUNASADMIN" in str(value).upper().split():
                result.update(administrator_required=True, source=f"{hive_name}\\{layers}",
                              executable=str(executable), compatibility_flags=value)
        except FileNotFoundError:
            pass
    result["blocked"] = result["administrator_required"] and not result["worker_elevated"]
    return result


def run_cst(job: dict, run_dir: Path, emit: Callable[[str, dict], None]) -> dict:
    """Build, optionally solve, and verify a new single-mode rectangular waveguide.

    The caller must serialize CST jobs. An explicit user-selected cst_pid may be
    attached, but only the newly-created project is owned and may be closed.
    No arbitrary VBA, existing project modification, or automatic solver retry occurs.
    """
    if job.get("kind") != "waveguide":
        raise ValueError("Only kind='waveguide' is implemented")
    parameters = waveguide_parameters(job.get("parameters"))
    timeout = job.get("timeout_seconds", 600)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        raise ValueError("timeout_seconds must be an integer from 1 to 3600")
    solve = job.get("solve", True)
    if not isinstance(solve, bool):
        raise ValueError("solve must be boolean")
    attach_pid = job.get("cst_pid")
    if attach_pid is not None and (isinstance(attach_pid, bool) or not isinstance(attach_pid, int) or attach_pid <= 0):
        raise ValueError("cst_pid must be an explicitly selected positive process ID")
    owns_application = attach_pid is None
    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    project_path = run_dir / "waveguide.cst"
    if project_path.exists() or (run_dir / "history.vba").exists():
        raise FileExistsError("Run directory already contains CST artifacts; use a fresh run ID")
    root = find_cst()
    if root is None:
        raise RuntimeError("CST Studio Suite 2025 installation was not found")
    launch_check = launch_permission_diagnostic(root)
    launch_check.update(connection_mode="new" if owns_application else "attach_explicit_pid", cst_pid=attach_pid)
    _json(run_dir / "launch_diagnostic.json", launch_check)
    if owns_application and launch_check["blocked"]:
        emit("launch_blocked", launch_check)
        raise CSTLaunchPermissionError(
            "Windows compatibility settings require CST to run as administrator, "
            "but this worker is not elevated. No CST instance was started and no settings were changed. "
            "Use a user-authorized matching execution privilege or review the CST RUNASADMIN setting.")
    libraries = root / "AMD64" / "python_cst_libraries"
    sys.path.insert(0, str(libraries))
    try:
        import cst.interface
        import cst.results
    except (ImportError, OSError) as exc:
        raise RuntimeError("Cannot import the installed CST Python libraries; use the configured compatible Python interpreter") from exc
    finally:
        sys.path.remove(str(libraries))

    history = waveguide_history(parameters)
    (run_dir / "history.vba").write_text(history, encoding="utf-8")
    _json(run_dir / "model.json", {"kind": "waveguide", "parameters": parameters,
          "material": "Vacuum", "walls": "PEC", "propagation_axis": "z",
          "ports": [1, 2], "excitation": "port 1 mode 1", "mesh_lines_per_wavelength": 20,
          "steady_state_limit_db": -40, "mesh_adaptation": False})
    de = project = None
    owned_pid = None
    solver_completed = False
    solver_info = None
    launch_requested = False
    started = time.monotonic()
    try:
        emit("launching", {"cst_root": str(root),
                           "ownership": "new_instance" if owns_application else "user_application_new_project_only",
                           "cst_pid": attach_pid})
        if owns_application:
            launch_requested = True
            de = cst.interface.DesignEnvironment.new()
        else:
            de = cst.interface.DesignEnvironment.connect(attach_pid)
        owned_pid = de.pid()
        if not de.is_connected():
            raise RuntimeError(f"CST Python connection was not established for PID {owned_pid}")
        if attach_pid is not None and owned_pid != attach_pid:
            raise RuntimeError("CST connected PID differs from the explicitly selected PID")
        version_file = root / "Patch_Version"
        version = version_file.read_text(errors="replace").strip() if version_file.exists() else "unknown"
        _json(run_dir / "cst_instance.json", {"pid": owned_pid, "installation_version": version,
              "application_owned": owns_application, "project_owned": True,
              "application_closed": False, "project_closed": False})
        emit("cst_started", {"pid": owned_pid, "installation_version": version})
        # Do not alter the user application's global quiet mode when attaching.
        if owns_application:
            de.set_quiet_mode(True)
        project = de.new_mws()
        project.model3d.add_to_history("AutoCST: uniform rectangular PEC waveguide", history, timeout=None)
        solver_name = project.model3d.get_active_solver_name()
        if solver_name != "HF Time Domain":
            raise RuntimeError(f"Unexpected active solver after explicit model setup: {solver_name}")
        emit("model_built", {"solver": solver_name, "history": str(run_dir / "history.vba")})
        project.save(str(project_path), include_results=True, allow_overwrite=False)
        if solve:
            emit("solver_started", {"solver": solver_name, "job_timeout_seconds": timeout,
                                    "deadline_enforced_by": "worker_supervisor"})
            # Documented synchronous operation completes solver + postprocessing,
            # and raises RuntimeError on error. A false is_running alone is insufficient.
            # The SDK help does not state the units of its optional timeout.
            # The service supervisor enforces an explicit wall-clock job deadline.
            project.model3d.run_solver(timeout=None)
            solver_info = project.model3d.get_solver_run_info(timeout=None)
            _json(run_dir / "solver_run_info.json", solver_info)
            if not isinstance(solver_info, dict) or solver_info.get("state") != "SUCCESS":
                raise RuntimeError(f"CST did not report a successful solver run: {solver_info}")
            solver_completed = True
            emit("solver_finished", {"run_info": solver_info})
            project.save(include_results=True)
        _json(run_dir / "cst_messages.json", project.get_messages())
    except BaseException as original_error:
        if owns_application and launch_requested and de is None:
            # A native launch failure can occur after process creation but before
            # a handle is returned. Do not release the job lock on uncertain ownership.
            try:
                _json(run_dir / "cleanup_required.json", {
                    "error": str(original_error), "pid": None,
                    "reason": "CST launch did not return an ownership handle"})
            except OSError:
                pass
            raise CSTCleanupRequired("CST launch failed before returning an ownership handle; check for a leftover instance") from original_error
        if project is not None:
            try:
                _json(run_dir / "cst_messages.json", project.get_messages())
            except Exception as exc:
                _json(run_dir / "message_capture_error.json", {"error": str(exc)})
            try:
                if project.model3d.is_solver_running(timeout=None):
                    project.model3d.abort_solver(timeout=None)
                    emit("solver_aborted", {"pid": owned_pid, "reason": "job_error_or_timeout"})
            except Exception as exc:
                _json(run_dir / "abort_error.json", {"error": str(exc), "pid": owned_pid})
        raise
    finally:
        if de is not None:
            close_warnings = []
            project_close_error = None
            try:
                # The only project handle eligible for closing is our new_mws result.
                if project is not None:
                    project.close()
            except Exception as exc:
                close_warnings.append(str(exc))
                project_close_error = exc
            try:
                if owns_application:
                    de.close()
                elif project_close_error is not None:
                    raise project_close_error
            except Exception as exc:
                try:
                    _json(run_dir / "cleanup_required.json", {"pid": owned_pid, "error": str(exc),
                          "application_owned": owns_application, "project": str(project_path)})
                except OSError:
                    pass  # Lock retention must not depend on successful evidence writing.
                raise CSTCleanupRequired(f"Could not confirm cleanup of this job's CST project in PID {owned_pid}: {exc}") from exc
            _json(run_dir / "cst_instance.json", {"pid": owned_pid,
                  "application_owned": owns_application, "project_owned": True,
                  "application_closed": owns_application, "project_closed": True,
                  "cleanup_complete": True, "warnings": close_warnings})
            emit("cst_closed", {"pid": owned_pid, "application_closed": owns_application,
                                "project_closed": True})

    result = {"kind": "waveguide", "parameters": parameters, "project": str(project_path),
              "solver_completed": solver_completed, "solver_run_info": solver_info,
              "physical_verification": {"performed": False, "reason": "No physical measurement was performed"},
              "mesh_convergence": {"performed": False}, "numerical_check": None,
              "elapsed_seconds": time.monotonic() - started}
    if not solve:
        result["status"] = "model_built_without_solver"
        return result

    # Offline read avoids sharing unsaved GUI state. A fresh project has no old runs.
    results = cst.results.ProjectFile(str(project_path), allow_interactive=False).get_3d()
    tree = results.get_tree_items()
    _json(run_dir / "result_tree.json", tree)
    curves = {}
    metadata = {}
    for label in ("S1,1", "S2,1"):
        treepath = "1D Results\\S-Parameters\\" + label
        if treepath not in tree:
            raise RuntimeError(f"Expected result missing from this run: {treepath}")
        run_ids = results.get_run_ids(treepath)
        if 0 not in run_ids:
            raise RuntimeError(f"Fresh project result has no run_id=0: {treepath}; got {run_ids}")
        item = results.get_result_item(treepath, run_id=0)
        frequency = [float(v) for v in item.get_xdata()]
        values = [complex(v) for v in item.get_ydata()]
        curves[label] = (frequency, values)
        metadata[label] = {"treepath": treepath, "run_id": item.run_id, "available_run_ids": run_ids,
                           "xlabel": item.xlabel, "ylabel": item.ylabel, "title": item.title}
    x, s11 = curves["S1,1"]
    x21, s21 = curves["S2,1"]
    if x != x21:
        raise RuntimeError("S11 and S21 frequency grids differ")
    csv_path = run_dir / "sparameters.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["frequency_ghz", "s11_real", "s11_imag", "s21_real", "s21_imag"])
        for f, r, t in zip(x, s11, s21):
            writer.writerow([f, r.real, r.imag, t.real, t.imag])
    _json(run_dir / "result_metadata.json", metadata)
    emit("exported", {"csv": str(csv_path), "samples": len(x)})
    check = validate_waveguide(x, s11, s21, parameters)
    _json(run_dir / "numerical_check.json", check)
    emit("validated", {"passed": check["passed"], "scope": check["scope"]})
    with project_path.open("rb") as stream:
        project_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    result.update(status="solved_and_checked" if check["passed"] else "numerical_check_failed",
                  numerical_check=check, result_csv=str(csv_path),
                  project_sha256=project_hash,
                  elapsed_seconds=time.monotonic() - started)
    if not check["passed"]:
        raise RuntimeError("Solver finished but the independent waveguide numerical check failed; inspect numerical_check.json")
    return result
