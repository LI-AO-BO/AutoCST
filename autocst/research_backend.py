"""Recoverable, one-operation-at-a-time CST research sessions.

The outer runner serializes these calls and imposes wall-clock deadlines in short
worker processes. This module never restarts a solver during recovery.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys
import uuid

from .config import find_cst
from .cst_backend import CSTCleanupRequired, CSTLaunchPermissionError, launch_permission_diagnostic
from .models import validate_waveguide
from .process_identity import get_process_identity, matches_process
from .research_models import metasurface_metadata, normalize_research_job, render_history


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, payload: dict | list) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _load_cst():
    root = find_cst()
    if root is None:
        raise RuntimeError("CST 2025 installation not found")
    sys.path.insert(0, str(root / "AMD64" / "python_cst_libraries"))
    try:
        import cst.interface
        import cst.results
        return root, cst.interface, cst.results
    finally:
        sys.path.pop(0)


def _parse_solver_log(text: str) -> dict:
    """Read observed CST 2025 English log records, retaining the raw log separately."""
    sources = re.findall(r"Stimulation port\s*:\s*(\S+)\s+Mode number\s*:\s*(\d+)", text)
    passes = [int(value) for value in re.findall(r"Adaptive mesh refinement pass\s+(\d+)", text)]
    cells = [int(value) for value in re.findall(r"Number of mesh cells\s*:\s*(\d+)", text)]
    deltas = [float(value) for value in re.findall(r"All S-Parameters\s*:\s*([\d.eE+-]+)", text)]
    return {"source_records": len(sources),
            "observed_sources": [{"port": port, "mode": int(mode)} for port, mode in sorted(set(sources))],
            "only_zmax_mode_1": bool(sources) and set(sources) == {("Zmax", "1")},
            "adaptation_passes": max(passes) if passes else None,
            "initial_mesh_cells": cells[0] if cells else None,
            "adaptation_mesh_cells": cells[:max(passes)] if passes else [],
            "final_mesh_cells": cells[-1] if cells else None,
            "last_adaptation_delta_s": deltas[-1] if deltas else None,
            "adaptive_accuracy_limit_reached": ("Mesh adaptation terminated because the desired accuracy limit is reached." in text) if passes else None,
            "calculation_finished_successfully": "Calculation finished successfully." in text}


class ResearchSession:
    def __init__(self, job: dict, run_dir: Path):
        self.job = normalize_research_job(job)
        self.run_dir = Path(run_dir).resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.project_path = self.run_dir / "project.cst"
        self.de = None
        self.project = None
        self.binding = None
        self._job_hash = hashlib.sha256(json.dumps(self.job, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _read(self, name: str):
        path = self.run_dir / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _record(self, name: str, payload: dict) -> None:
        _write(self.run_dir / name, {"utc": _utc(), **payload})

    def _connect(self, pid: int):
        _, interface, _ = _load_cst()
        self.de = interface.DesignEnvironment.connect(pid)
        if not self.de.is_connected() or self.de.pid() != pid:
            raise RuntimeError("Explicit CST PID connection failed")

    def prepare(self) -> dict:
        if (self.run_dir / "binding.json").exists() or self.project_path.exists():
            raise FileExistsError("This research run already has a binding or project; recover instead of preparing again")
        script = self.job.get("history_text") or render_history(self.job)
        (self.run_dir / "history.vba").write_text(script, encoding="utf-8")
        _write(self.run_dir / "research_job.json", self.job)
        root, interface, _ = _load_cst()
        pid = self.job.get("cst_pid")
        owns_application = pid is None
        if owns_application:
            diagnostic = launch_permission_diagnostic(root)
            _write(self.run_dir / "launch_diagnostic.json", diagnostic)
            if diagnostic["blocked"]:
                raise CSTLaunchPermissionError("CST requires elevation; connect an explicitly selected user-started instance")
            self.de = interface.DesignEnvironment.new()
            pid = self.de.pid()
        else:
            identity_before = get_process_identity(pid)
            if not identity_before["alive"]:
                raise RuntimeError(f"Selected CST PID {pid} no longer exists")
            expected_creation = self.job.get("cst_creation_time")
            if expected_creation and str(expected_creation) != identity_before["creation_time"]:
                raise RuntimeError("Selected CST PID was reused; do not attach to the replacement process")
            self._connect(pid)
        identity = get_process_identity(pid)
        if not identity["alive"] or not identity["creation_time"]:
            raise RuntimeError("Cannot establish the CST process creation identity")
        original_projects = list(self.de.list_open_projects())
        for original_path in original_projects:
            original = self.de.get_open_project(original_path)
            try:
                running = original.model3d.is_solver_running(timeout=None)
            except Exception as exc:
                raise RuntimeError(f"Cannot establish whether the existing user project is idle: {original_path}") from exc
            if running:
                raise RuntimeError(f"An existing user project is solving; it was left untouched: {original_path}")
        self.binding = {
            "schema_version": 1, "binding_id": str(uuid.uuid4()), "cst_pid": pid,
            "cst_creation_time": identity["creation_time"], "project_path": str(self.project_path),
            "run_directory": str(self.run_dir), "job_sha256": self._job_hash,
            "history_sha256": hashlib.sha256(script.encode()).hexdigest(),
            "application_owned": owns_application, "original_open_projects": original_projects,
            "kind": self.job["kind"], "created_utc": _utc(),
        }
        # Bind before potentially slow model creation/save so the supervisor has
        # the process identity even if this action is interrupted.
        _write(self.run_dir / "binding.json", self.binding)
        quiet_before = bool(self.de.in_quiet_mode())
        self._record("quiet_mode.json", {"original": quiet_before, "restored": False})
        self.de.set_quiet_mode(True)
        try:
            if self.job["kind"] == "existing_project":
                source = Path(self.job["source_project"]).resolve()
                if not source.is_file() or source == self.project_path:
                    raise ValueError("source_project must be a different existing .cst file")
                source_hash = _hash(source)
                expected_hash = self.job.get("source_project_sha256")
                if expected_hash and expected_hash != source_hash:
                    raise ValueError("source_project changed after review; prepare a fresh reviewed job")
                shutil.copy2(source, self.project_path)
                copied_hash = _hash(self.project_path)
                if copied_hash != source_hash or _hash(source) != source_hash:
                    raise RuntimeError("Source changed while copying; the copy will not be opened")
                self._record("source_copy.json", {"source": str(source), "source_sha256": source_hash,
                             "copied_sha256": copied_hash, "original_modified": False})
                self.project = self.de.open_project(str(self.project_path))
                # The official DeleteResults command targets only this copied project.
                # It is not applied to the source or to any pre-existing user project.
                self.project.model3d.DeleteResults()
                if self.job["parameters"]:
                    for key, value in self.job["parameters"].items():
                        self.project.model3d.StoreParameter(key, value)
                    self.project.model3d.Rebuild()
                if self.job["history_text"].strip():
                    self.project.model3d.add_to_history("AutoCST explicit research modification", self.job["history_text"], timeout=None)
            else:
                self.project = self.de.new_mws()
                self.project.save(str(self.project_path), include_results=False, allow_overwrite=False)
                self.project.model3d.add_to_history("AutoCST research model", script, timeout=None)
            solver = self.project.model3d.get_active_solver_name()
            expected_solver = {"metasurface": "HF Frequency Domain", "waveguide": "HF Time Domain"}.get(self.job["kind"])
            if expected_solver and solver != expected_solver:
                raise RuntimeError(f"Unexpected active solver {solver}, expected {expected_solver}")
            self.project.save(include_results=False)
            self._record("results_reset.json", {"fresh_project": self.job["kind"] != "existing_project",
                         "copied_results_deleted": self.job["kind"] == "existing_project",
                         "project_sha256_before_run": _hash(self.project_path)})
            model = (metasurface_metadata(self.job["parameters"]) if self.job["kind"] == "metasurface"
                     else {"kind": self.job["kind"], "parameters": self.job["parameters"]})
            _write(self.run_dir / "model.json", model)
            complete = {"binding_id": self.binding["binding_id"], "solver": solver,
                        "project_path": str(self.project_path), "project_sha256": _hash(self.project_path)}
            self._record("prepared.json", complete)
            self._record("prepare_completed.json", complete)
            return self.binding
        except Exception:
            self._capture_messages()
            # Keep the binding if close fails; the outer runner owns recovery decisions.
            if self.project is not None:
                try:
                    self.project.close()
                    self._record("prepare_failed_project_closed.json", {"project_path": str(self.project_path)})
                except Exception as close_error:
                    raise CSTCleanupRequired(f"Research preparation failed and its project could not be closed: {close_error}") from close_error
            raise
        finally:
            self.de.set_quiet_mode(quiet_before)
            if bool(self.de.in_quiet_mode()) != quiet_before:
                raise CSTCleanupRequired("Could not confirm restoration of the user's CST quiet mode")
            self._record("quiet_mode.json", {"original": quiet_before, "restored": True})

    def recover(self, binding: dict):
        saved_binding = self._read("binding.json")
        if not saved_binding or saved_binding.get("binding_id") != binding.get("binding_id"):
            raise ValueError("Recovery binding does not match this run's saved binding")
        if saved_binding.get("job_sha256") != self._job_hash:
            raise ValueError("Recovery job differs from the frozen job")
        self.binding = saved_binding
        path = Path(saved_binding["project_path"]).resolve()
        if path.parent != self.run_dir or path != self.project_path:
            raise ValueError("Bound project escaped its run directory")
        if self._read("project_closed.json"):
            return self  # Saved/closed results can be exported without a live CST instance.
        if not matches_process(saved_binding):
            if self._read("solver_saved.json"):
                self._record("project_closed.json", {"reason": "original_process_no_longer_exists",
                             "project_path": str(path), "application_closed_by_tool": False})
                return self
            raise RuntimeError("Original CST process is absent or its PID was reused; no solver was restarted")
        self._connect(saved_binding["cst_pid"])
        quiet = self._read("quiet_mode.json")
        if quiet and not quiet.get("restored"):
            self.de.set_quiet_mode(bool(quiet["original"]))
            if bool(self.de.in_quiet_mode()) != bool(quiet["original"]):
                raise CSTCleanupRequired("Could not confirm restoration of the user's CST quiet mode during recovery")
            self._record("quiet_mode.json", {"original": bool(quiet["original"]), "restored": True,
                         "restored_during_recovery": True})
        try:
            self.project = self.de.get_open_project(str(path))
        except Exception:
            if self._read("solver_saved.json"):
                self._record("project_closed.json", {"reason": "saved_project_is_no_longer_open",
                             "project_path": str(path), "application_closed_by_tool": False})
                return self
            raise RuntimeError("Original research project is not open; recovery never reopens or restarts it")
        if Path(self.project.filename()).resolve() != path:
            raise RuntimeError("Connected CST project path differs from the binding")
        return self

    def start(self):
        if self.project is None or not self._read("prepare_completed.json"):
            raise RuntimeError("Start requires the prepared, bound project")
        if not self.job["solve"]:
            self._record("start_returned.json", {"skipped": True})
            return
        # The exclusive intent marker makes start at-most-once even if the API call
        # starts a solver but the action process crashes before returning an ack.
        with (self.run_dir / "start_intent.json").open("x", encoding="utf-8") as stream:
            json.dump({"utc": _utc(), "binding_id": self.binding["binding_id"]}, stream)
        if self.project.model3d.is_solver_running(timeout=None):
            raise RuntimeError("The bound project already has a running solver; start was not sent")
        self._record("solver_before_start.json", {"solver_info": self.project.model3d.get_solver_run_info(timeout=None)})
        self.project.model3d.start_solver(timeout=None)
        self._record("start_returned.json", {"binding_id": self.binding["binding_id"], "started": True})

    def solve_wait(self) -> dict:
        """Wait in CST's native blocking API; the outer process owns the deadline.

        The exclusive intent is shared with start(), so switching between the
        synchronous and asynchronous entry points can never submit a second run.
        """
        if self.project is None or not self._read("prepare_completed.json") or not self.binding:
            raise RuntimeError("Solve requires the prepared, bound project")
        if not matches_process(self.binding):
            raise RuntimeError("Original CST process is absent or its PID was reused; no solver was started")
        if Path(self.project.filename()).resolve() != self.project_path:
            raise RuntimeError("Connected CST project path differs from the binding")
        if not self.job["solve"]:
            self._record("start_returned.json", {"skipped": True})
            return self.poll()
        with (self.run_dir / "start_intent.json").open("x", encoding="utf-8") as stream:
            json.dump({"utc": _utc(), "binding_id": self.binding["binding_id"],
                       "operation": "solve_wait"}, stream)
        if self.project.model3d.is_solver_running(timeout=None):
            raise RuntimeError("The bound project already has a running solver; no start was sent")
        self._record("solver_before_start.json", {"solver_info": self.project.model3d.get_solver_run_info(timeout=None)})
        self._record("solver_waiting.json", {"binding_id": self.binding["binding_id"],
                     "native_call": "Model3D.run_solver(timeout=None)",
                     "completion_notification": "native blocking call return"})
        try:
            self.project.model3d.run_solver(timeout=None)
        except Exception as exc:
            self._record("solver_wait_error.json", {"error": str(exc), "automatic_retry": False})
            raise
        self._record("start_returned.json", {"binding_id": self.binding["binding_id"],
                     "started": True, "completed_native_wait": True})
        status = self.poll()
        self._record("solver_completed.json", {"binding_id": self.binding["binding_id"], **status})
        if status["running"] or not status.get("fresh_run_confirmed"):
            raise RuntimeError(f"Native solver return did not establish a fresh successful run: {status}")
        return status

    def poll(self) -> dict:
        if not self.job["solve"]:
            return {"running": False, "solver_info": {"state": "SKIPPED"}, "fresh_run_confirmed": False}
        if self.project is None:
            saved = self._read("solver_saved.json")
            if saved:
                return {"running": False, "solver_info": saved["solver_info"], "fresh_run_confirmed": True}
            raise RuntimeError("No bound open project to poll")
        running = bool(self.project.model3d.is_solver_running(timeout=None))
        info = self.project.model3d.get_solver_run_info(timeout=None)
        fresh = bool(self._read("results_reset.json") and self._read("start_intent.json"))
        # SUCCESS without an ack is accepted only when a fresh result tree exists.
        if fresh and not running and isinstance(info, dict) and info.get("state") == "SUCCESS":
            tree = self.project.model3d.get_tree_items()
            fresh = any(str(item).startswith("1D Results\\") for item in tree)
        else:
            fresh = False
        result = {"running": running, "solver_info": info, "fresh_run_confirmed": fresh}
        self._record("last_poll.json", result)
        return result

    def _capture_messages(self):
        if self.project is not None:
            try:
                _write(self.run_dir / "cst_messages.json", self.project.get_messages())
            except Exception as exc:
                self._record("message_capture_error.json", {"error": str(exc)})

    def _close_owned_project(self):
        if self.project is not None:
            try:
                self.project.close()
            except Exception as exc:
                raise CSTCleanupRequired(f"Could not close this research job's project: {exc}") from exc
            self.project = None
        self._record("project_closed.json", {"project_path": str(self.project_path),
                     "application_closed_by_tool": False})
        # Even a new application is retained for user inspection in research mode.

    def cancel(self) -> dict:
        if self.project is None:
            if self._read("project_closed.json"):
                return {"stopped": True, "confirmed_stopped": True, "already_closed": True}
            raise RuntimeError("Cannot confirm cancellation without the bound project")
        running = self.project.model3d.is_solver_running(timeout=None)
        if running:
            self.project.model3d.abort_solver(timeout=None)
        still_running = bool(self.project.model3d.is_solver_running(timeout=None))
        info = self.project.model3d.get_solver_run_info(timeout=None)
        result = {"stopped": not still_running, "confirmed_stopped": not still_running,
                  "running": still_running, "solver_info": info}
        self._record("cancel_requested.json", result)
        self._capture_messages()
        if not still_running:
            self.project.save(include_results=True)
            self._close_owned_project()
            self._record("cancel_confirmed.json", result)
        return result

    def finish(self) -> dict:
        cached = self._read("research_result.json")
        if cached:
            return cached
        saved = self._read("solver_saved.json")
        if saved is None:
            if self.project is None:
                raise RuntimeError("No saved results or bound project are available")
            status = self.poll()
            if status["running"]:
                raise RuntimeError("Solver is still running; finish does not abort it")
            success = isinstance(status["solver_info"], dict) and status["solver_info"].get("state") == "SUCCESS"
            if self.job["solve"] and (not success or not status.get("fresh_run_confirmed")):
                raise RuntimeError(f"Cannot export a successful research run without fresh SUCCESS: {status}")
            self._capture_messages()
            self.project.save(include_results=True)
            saved = {"solver_info": status["solver_info"], "solver_success": success,
                     "project_sha256": _hash(self.project_path), "project_path": str(self.project_path),
                     "fresh_run_confirmed": status.get("fresh_run_confirmed", False)}
            self._record("solver_saved.json", saved)
        if self.project is not None:
            self._close_owned_project()
        if _hash(self.project_path) != saved["project_sha256"]:
            raise RuntimeError("Saved CST project hash changed before offline export")
        result = {"kind": self.job["kind"], "project_path": str(self.project_path),
                  "solver_success": saved["solver_success"], "solver_info": saved["solver_info"],
                  "data_integrity": {"passed": False, "checks": []},
                  "numerical_validity": {"passed": None, "scope": "No problem-specific validator"},
                  "target_achieved": None, "physical_measurements": False,
                  "project_sha256": saved["project_sha256"]}
        if not self.job["solve"]:
            result["status"] = "model_only"
            _write(self.run_dir / "research_result.json", result)
            return result
        log_source = self.project_path.with_suffix("") / "Result" / "Model.log"
        log_copy = self.run_dir / "solver.log"
        if log_source.is_file():
            shutil.copy2(log_source, log_copy)
        evidence = {"available": log_copy.is_file()}
        if log_copy.is_file():
            evidence.update(_parse_solver_log(log_copy.read_text(encoding="utf-8", errors="replace")))
            evidence.update(path=str(log_copy), sha256=_hash(log_copy),
                            excitation_list_item_getter_available=False)
        self._record("solver_evidence.json", evidence)
        result["solver_evidence"] = evidence
        if self.job["kind"] == "metasurface" and not evidence.get("only_zmax_mode_1"):
            raise RuntimeError("Cannot verify the requested Zmax mode 1 excitation from the saved solver log")
        curves = self._export_curves()
        result["data_integrity"] = {"passed": bool(curves), "curve_count": len(curves),
                                    "checks": ["saved_project_sha256", "exact_tree_path_and_run_id", "finite_aligned_arrays"]}
        if self.job["kind"] == "metasurface":
            result["numerical_validity"] = self._metasurface_result(curves)
            numerical = result["numerical_validity"]
            numerical["power_balance_passed"] = numerical["passed"]
            numerical["adaptive_solver_criterion_met"] = evidence.get("adaptive_accuracy_limit_reached")
            numerical["passed"] = numerical["power_balance_passed"] and numerical["adaptive_solver_criterion_met"] is True
            numerical["status"] = ("checks_passed" if numerical["passed"] else
                                   "adaptation_evidence_unknown" if numerical["adaptive_solver_criterion_met"] is None else
                                   "adaptation_not_converged" if not numerical["adaptive_solver_criterion_met"] else "power_balance_failed")
            _write(self.run_dir / "numerical_check.json", numerical)
            result["data_integrity"]["checks"].append("solver_log_only_zmax_mode_1")
            result["result_csv"] = str(self.run_dir / "reflection.csv")
        elif self.job["kind"] == "waveguide":
            x, r = curves["1D Results\\S-Parameters\\S1,1"]
            xt, t = curves["1D Results\\S-Parameters\\S2,1"]
            if x != xt:
                raise RuntimeError("Waveguide result grids are not aligned")
            result["numerical_validity"] = validate_waveguide(x, r, t, self.job["parameters"])
            with (self.run_dir / "sparameters.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["frequency_ghz", "s11_real", "s11_imag", "s21_real", "s21_imag"])
                writer.writerows((f, a.real, a.imag, b.real, b.imag) for f, a, b in zip(x, r, t))
            result["result_csv"] = str(self.run_dir / "sparameters.csv")
        result["status"] = "exported"
        _write(self.run_dir / "research_result.json", result)
        return result

    def _export_curves(self) -> dict:
        _, _, results_library = _load_cst()
        project_results = results_library.ProjectFile(str(self.project_path), allow_interactive=False)
        module = project_results.get_3d()
        tree = module.get_tree_items()
        _write(self.run_dir / "result_tree.json", tree)
        queries = list(self.job["result_queries"])
        required = []
        if self.job["kind"] == "metasurface":
            required = ["1D Results\\S-Parameters\\SZmax(1),Zmax(1)",
                        "1D Results\\S-Parameters\\SZmax(2),Zmax(1)"]
        elif self.job["kind"] == "waveguide":
            required = ["1D Results\\S-Parameters\\S1,1", "1D Results\\S-Parameters\\S2,1"]
        for path in required:
            if not any(q["treepath"] == path and q["run_id"] == 0 for q in queries):
                queries.append({"treepath": path, "run_id": 0})
        if not queries:
            raise ValueError("Generic research jobs require explicit result_queries to determine export success")
        metadata = []
        curves = {}
        for index, query in enumerate(queries):
            path, run_id = query["treepath"], query["run_id"]
            if path not in tree:
                raise RuntimeError(f"Requested result is absent: {path}")
            available = module.get_run_ids(path)
            if run_id not in available:
                raise RuntimeError(f"Requested run_id {run_id} is absent for {path}: {available}")
            item = module.get_result_item(path, run_id=run_id)
            x, y = [float(v) for v in item.get_xdata()], [complex(v) for v in item.get_ydata()]
            if not x or len(x) != len(y) or any(not math.isfinite(v) for v in x):
                raise RuntimeError(f"Empty, mismatched or non-finite result array: {path}")
            if any(not math.isfinite(v.real) or not math.isfinite(v.imag) for v in y):
                raise RuntimeError(f"Non-finite result: {path}")
            export = self.run_dir / f"curve_{index:03d}.csv"
            with export.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.writer(stream)
                writer.writerow(["x", "real", "imag"])
                writer.writerows((f, v.real, v.imag) for f, v in zip(x, y))
            metadata.append({"treepath": path, "run_id": run_id, "available_run_ids": available,
                             "xlabel": item.xlabel, "ylabel": item.ylabel, "title": item.title,
                             "csv": str(export), "sha256": _hash(export), "samples": len(x)})
            curves[path] = (x, y)
        _write(self.run_dir / "result_metadata.json", metadata)
        return curves

    def _metasurface_result(self, curves: dict) -> dict:
        co_path = "1D Results\\S-Parameters\\SZmax(1),Zmax(1)"
        cross_path = "1D Results\\S-Parameters\\SZmax(2),Zmax(1)"
        x, co = curves[co_path]
        xc, cross = curves[cross_path]
        p = self.job["parameters"]
        if x != xc or len(x) != p["frequency_samples"]:
            raise RuntimeError("Floquet reflection grids do not match the requested discrete samples")
        if any(b <= a for a, b in zip(x, x[1:])):
            raise RuntimeError("Frequency grid must increase strictly")
        if abs(x[0] - p["fmin_ghz"]) > 1e-6 or abs(x[-1] - p["fmax_ghz"]) > 1e-6:
            raise RuntimeError("Floquet result frequency band or GHz units do not match the job")
        power = [abs(a) ** 2 + abs(b) ** 2 for a, b in zip(co, cross)]
        error = max(abs(value - 1) for value in power)
        with (self.run_dir / "reflection.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["frequency_ghz", "s11_real", "s11_imag", "cross_real", "cross_imag", "total_reflected_power"])
            writer.writerows((f, a.real, a.imag, b.real, b.imag, q) for f, a, b, q in zip(x, co, cross, power))
        check = {"passed": error <= 0.05, "max_reflected_power_error": error,
                 "min_co_reflection_db": min(20 * math.log10(max(abs(v), 1e-300)) for v in co),
                 "max_cross_reflection_db": max(20 * math.log10(max(abs(v), 1e-300)) for v in cross),
                 "tolerance": 0.05, "propagating_modes_summed": ["TE(0,0)", "TM(0,0)"],
                 "scope": "Lossless grounded sub-diffraction unit cell power-balance check; no mesh or modal convergence claim",
                 "mesh_convergence": False, "modal_convergence": False,
                 "phase_reference_plane_z_mm": p["substrate_height_mm"] + p["metal_thickness_mm"]}
        _write(self.run_dir / "numerical_check.json", check)
        return check
