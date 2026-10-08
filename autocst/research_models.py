"""Auditable CST 2025 research templates and JSON-only job normalization."""
from __future__ import annotations

import math
from pathlib import Path
import re

from .models import C0, waveguide_history, waveguide_parameters

METASURFACE_DEFAULTS = {
    "period_mm": 15.0, "patch_mm": 10.0, "substrate_height_mm": 1.6,
    "epsilon_r": 2.2, "metal_thickness_mm": 0.035, "air_height_mm": 10.0,
    "fmin_ghz": 9.5, "fmax_ghz": 10.5, "frequency_samples": 21,
    "mesh_steps_per_wavelength": 10,
}


def _numbers(parameters: dict) -> dict:
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be an object")
    result = {}
    for key, value in parameters.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key):
            raise ValueError(f"Invalid CST parameter name: {key!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be numeric, not executable text")
        try:
            number = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"{key} must be finite") from exc
        if not math.isfinite(number):
            raise ValueError(f"{key} must be finite")
        result[key] = number
    return result


def metasurface_parameters(parameters: dict | None = None) -> dict:
    supplied = _numbers({} if parameters is None else parameters)
    unknown = set(supplied) - set(METASURFACE_DEFAULTS) - {"mesh_cells_per_box"}
    if unknown:
        raise ValueError(f"Unsupported metasurface parameters: {sorted(unknown)}")
    p = {**METASURFACE_DEFAULTS, **supplied}
    if not 2 <= p["period_mm"] <= 100:
        raise ValueError("period_mm must lie in [2,100]")
    if not 0.01 * p["period_mm"] <= p["patch_mm"] <= 0.98 * p["period_mm"]:
        raise ValueError("patch_mm must lie within [0.01,0.98] times period_mm")
    if not 0.1 <= p["substrate_height_mm"] <= 0.5 * p["period_mm"]:
        raise ValueError("substrate_height_mm must lie in [0.1,period_mm/2]")
    if not 1 <= p["epsilon_r"] <= 30:
        raise ValueError("epsilon_r must lie in [1,30]")
    if not 0.005 <= p["metal_thickness_mm"] <= 0.25 * p["substrate_height_mm"]:
        raise ValueError("metal_thickness_mm must be positive and thin relative to the substrate")
    if not 0.25 * p["period_mm"] <= p["air_height_mm"] <= 2 * p["period_mm"]:
        raise ValueError("air_height_mm must lie within [0.25,2] times period_mm")
    diffraction_cutoff = C0 / (p["period_mm"] * 1e-3) / 1e9
    if not 0 < p["fmin_ghz"] < p["fmax_ghz"] <= min(100, 0.95 * diffraction_cutoff):
        raise ValueError("Band must be below the first air-side diffraction order at normal incidence")
    count = p["frequency_samples"]
    if count != int(count) or not 3 <= count <= 201 or int(count) % 2 == 0:
        raise ValueError("frequency_samples must be an odd integer in [3,201] so the center adaptation point is included")
    p["frequency_samples"] = int(count)
    mesh = p["mesh_steps_per_wavelength"]
    if mesh != int(mesh) or not 8 <= mesh <= 40:
        raise ValueError("mesh_steps_per_wavelength must be an integer in [8,40]")
    p["mesh_steps_per_wavelength"] = int(mesh)
    if "mesh_cells_per_box" in p:
        box = p["mesh_cells_per_box"]
        if box != int(box) or not 8 <= box <= 40:
            raise ValueError("mesh_cells_per_box must be an integer in [8,40]")
        p["mesh_cells_per_box"] = int(box)
    return p


def normalize_research_job(job: dict) -> dict:
    if not isinstance(job, dict):
        raise ValueError("A research job must be an object")
    normalized = dict(job)
    kind = job.get("kind")
    if kind not in {"metasurface", "waveguide", "history", "existing_project"}:
        raise ValueError("Unsupported research kind")
    parameters = job.get("parameters", {})
    if kind == "metasurface":
        parameters = metasurface_parameters(parameters)
    elif kind == "waveguide":
        parameters = waveguide_parameters(parameters)
    else:
        parameters = _numbers(parameters)
    pid = job.get("cst_pid")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
        raise ValueError("cst_pid must be an explicit positive integer")
    timeout = job.get("timeout_seconds", 3600)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 10 <= timeout <= 7 * 24 * 3600:
        raise ValueError("timeout_seconds must be an integer between 10 and 604800")
    solve = job.get("solve", True)
    if not isinstance(solve, bool):
        raise ValueError("solve must be boolean")
    history = job.get("history_text", "")
    if not isinstance(history, str) or len(history.encode("utf-8")) > 2_000_000 or "\0" in history:
        raise ValueError("history_text must be at most 2 MB of text without NUL characters")
    if kind == "history" and not history.strip():
        raise ValueError("history jobs require explicit frozen history_text")
    source = job.get("source_project")
    if kind == "existing_project" and (not isinstance(source, str) or not source.lower().endswith(".cst")):
        raise ValueError("existing_project requires source_project ending in .cst")
    queries = job.get("result_queries", [])
    if not isinstance(queries, list) or len(queries) > 100:
        raise ValueError("result_queries must be a list of at most 100 exact tree paths")
    normalized_queries = []
    for query in queries:
        if isinstance(query, str):
            query = {"treepath": query}
        if not isinstance(query, dict):
            raise ValueError("Each result query must be an object or exact tree path")
        treepath = query.get("treepath")
        run_id = query.get("run_id", 0)
        if not isinstance(treepath, str) or not treepath or "\0" in treepath or len(treepath) > 1024:
            raise ValueError("Invalid result tree path")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 0:
            raise ValueError("run_id must be a nonnegative integer")
        normalized_queries.append({**query, "treepath": treepath, "run_id": run_id})
    normalized.update(kind=kind, parameters=parameters, cst_pid=pid,
                      timeout_seconds=timeout, solve=solve, history_text=history,
                      source_project=source, result_queries=normalized_queries)
    return normalized


def _parameter_history(parameters: dict) -> str:
    return "\n".join(f'StoreParameter "{key}", "{value:.12g}"' for key, value in parameters.items())


def metasurface_history(parameters: dict | None = None) -> str:
    p = metasurface_parameters(parameters)
    script = _parameter_history(p) + '''
ChangeSolverType "HF Frequency Domain"
With Units
    .Geometry "mm"
    .Frequency "GHz"
    .Time "ns"
End With
With Background
    .Reset
    .Type "normal"
    .Epsilon "1"
    .Mu "1"
    .XminSpace "0"
    .XmaxSpace "0"
    .YminSpace "0"
    .YmaxSpace "0"
    .ZminSpace "0"
    .ZmaxSpace "air_height_mm"
End With
With Material
    .Reset
    .Name "AutoCST_Lossless_Substrate"
    .Type "Normal"
    .Epsilon "epsilon_r"
    .Mu "1"
    .Sigma "0"
    .TanD "0"
    .Create
End With
Component.New "metasurface"
'''
    for name, material, width, z0, z1 in [
        ("ground", "PEC", "period_mm", "-metal_thickness_mm", "0"),
        ("substrate", "AutoCST_Lossless_Substrate", "period_mm", "0", "substrate_height_mm"),
        ("patch", "PEC", "patch_mm", "substrate_height_mm", "substrate_height_mm+metal_thickness_mm"),
    ]:
        script += f'''With Brick
    .Reset
    .Name "{name}"
    .Component "metasurface"
    .Material "{material}"
    .Xrange "-{width}/2", "{width}/2"
    .Yrange "-{width}/2", "{width}/2"
    .Zrange "{z0}", "{z1}"
    .Create
End With
'''
    script += f'''With Boundary
    .Xmin "unit cell"
    .Xmax "unit cell"
    .Ymin "unit cell"
    .Ymax "unit cell"
    .Zmin "electric"
    .Zmax "open"
    .Xsymmetry "none"
    .Ysymmetry "none"
    .Zsymmetry "none"
    .UnitCellFitToBoundingBox "True"
    .UnitCellAngle "90"
    .SetPeriodicBoundaryAngles "0", "0"
End With
With FloquetPort
    .Reset
    .Port "Zmax"
    .SetDialogFrequency "(fmin_ghz+fmax_ghz)/2"
    .SetDialogTheta "0"
    .SetDialogPhi "0"
    .SetDialogMaxOrderX "1"
    .SetDialogMaxOrderYPrime "1"
    .SetSortCode "+beta/pw"
    .SetCustomizedListFlag "False"
    .SetNumberOfModesConsidered "18"
    .SetDistanceToReferencePlane "-air_height_mm"
End With
Dim autoCST_TE As Long
Dim autoCST_TM As Long
With FloquetPort
    .Port "Zmax"
    .GetModeNumberByName autoCST_TE, "TE(0,0)"
    .GetModeNumberByName autoCST_TM, "TM(0,0)"
End With
If autoCST_TE <> 1 Or autoCST_TM <> 2 Then
    Err.Raise 513, "AutoCST", "Unexpected Floquet fundamental-mode ordering"
End If
Solver.FrequencyRange "fmin_ghz", "fmax_ghz"
With Mesh
    .StepsPerWavelengthTet "{p['mesh_steps_per_wavelength']}"
End With
With MeshAdaption3D
    .SetType "HighFrequencyTet"
    .MinPasses "3"
    .MaxPasses "8"
    .MaxDeltaS "0.02"
    .NumberOfDeltaSChecks "1"
    .EnableInnerSParameterAdaptation "True"
End With
With FDSolver
    .SetMethod "Tetrahedral", "Discrete samples only"
    .OrderTet "Second"
    .AccuracyTet "1e-6"
    .MeshAdaptionTet "True"
    .ResetSampleIntervals "all"
    .AddSampleInterval "(fmin_ghz+fmax_ghz)/2", "(fmin_ghz+fmax_ghz)/2", "1", "Single", "True"
    .AddSampleInterval "fmin_ghz", "fmax_ghz", "{p['frequency_samples']}", "Equidistant", "False"
    .Stimulation "List", "List"
    .ResetExcitationList
    .AddToExcitationList "Zmax", "TE(0,0)"
    .AutoNormImpedance "False"
    .UseDistributedComputing "False"
    .UseParallelization "True"
    .MaxCPUs "4"
End With
If FDSolver.GetStimulationPort <> "List" Or FDSolver.GetStimulationMode <> "List" Then
    Err.Raise 513, "AutoCST", "Unexpected frequency-domain excitation selection"
End If
'''
    if "mesh_cells_per_box" in p:
        box = p["mesh_cells_per_box"]
        # CST 2025's current tetrahedral mesher uses MeshSettings. Keep the
        # legacy wavelength setting separate; geometric density is explicit.
        settings = f'''With MeshSettings
    .SetMeshType "Tet"
    .Set "Version", 1%
    .Set "CellsPerWavelengthPolicy", "automatic"
    .Set "StepsPerBoxNear", "{box}"
    .Set "StepsPerBoxFar", "{box}"
    .Set "ModelBoxDescrNear", "maxedge"
    .Set "ModelBoxDescrFar", "maxedge"
    If CStr(.Get("StepsPerBoxNear")) <> "{box}" Or CStr(.Get("StepsPerBoxFar")) <> "{box}" Then
        Err.Raise 513, "AutoCST", "Unexpected geometric tetrahedral mesh settings"
    End If
End With
'''
        script = script.replace("With MeshAdaption3D\n", settings + "With MeshAdaption3D\n", 1)
    return script


def render_history(job: dict) -> str:
    normalized = normalize_research_job(job)
    if normalized["kind"] == "metasurface":
        return metasurface_history(normalized["parameters"])
    if normalized["kind"] == "waveguide":
        return waveguide_history(normalized["parameters"])
    parameter_text = _parameter_history(normalized["parameters"])
    return parameter_text + "\n" + normalized["history_text"] + "\n"


def metasurface_metadata(parameters: dict) -> dict:
    p = metasurface_parameters(parameters)
    return {"topology": "square PEC patch / lossless dielectric / continuous PEC ground",
            "parameters": p, "incidence": "normal, Zmax TE(0,0)",
            "periodic_boundaries": ["x", "y"], "floquet_modes_considered": 18,
            "excitation_assertions": {"mode_1_name": "TE(0,0)", "mode_2_name": "TM(0,0)",
                                      "solver_selection": "List/List", "list_item_readback": False,
                                      "postsolve_required_evidence": "solver log only Zmax mode 1"},
            "propagating_reflection_modes": ["TE(0,0)", "TM(0,0)"],
            "phase_reference_plane_z_mm": p["substrate_height_mm"] + p["metal_thickness_mm"],
            "phase_deembedding_mm": -p["air_height_mm"],
            "mesh_adaptation": {"enabled": True,
                                "frequency_ghz": (p["fmin_ghz"] + p["fmax_ghz"]) / 2,
                                "minimum_passes": 3, "maximum_passes": 8,
                                "max_complex_delta_s": 0.02, "consecutive_checks": 1},
            "numerical_scope": "single adaptive-mesh run; convergence achievement requires solver evidence; no independent mesh, Floquet-mode-count or reference-plane convergence claim",
            "physical_measurements": False}
