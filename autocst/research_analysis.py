"""Deterministic single-frequency metrics and auditable one-parameter proposals."""
from __future__ import annotations

import bisect
import cmath
import csv
import json
import math
from pathlib import Path


def wrap_phase_deg(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Phase must be finite")
    return (float(value) + 180.0) % 360.0 - 180.0


def _finite(value, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _db(value: complex) -> float:
    return 20.0 * math.log10(max(abs(value), 1e-300))


def analyze_sparameters(csv_path: Path, spec: dict, solver_success: bool = True,
                        numerical_check: dict | None = None) -> dict:
    """Read complex data, interpolate complex samples, and keep evidence levels separate.

    Reflection CSV may contain cross polarization and total reflected modal power.
    A successful solve never promotes untested mesh convergence to a pass.
    """
    objective = spec.get("objective", {})
    if not isinstance(objective, dict):
        raise ValueError("Structured objective with frequency_ghz is required for automated analysis")
    target_frequency = _finite(objective["frequency_ghz"], "objective.frequency_ghz")
    constraints = spec.get("constraints", {})
    if not isinstance(constraints, dict):
        raise ValueError("Automated analysis requires constraints as an object")
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        names = set(reader.fieldnames or ())
        if not {"frequency_ghz", "s11_real", "s11_imag"} <= names:
            raise ValueError("CSV must contain frequency_ghz,s11_real,s11_imag")
        rows = [{key: _finite(value, key) for key, value in row.items() if key is not None and value not in (None, "")}
                for row in reader]
    if not rows:
        raise ValueError("Result CSV is empty")
    for row in rows:
        if not {"frequency_ghz", "s11_real", "s11_imag"} <= row.keys():
            raise ValueError("Result CSV contains an incomplete sample")
    frequencies = [row["frequency_ghz"] for row in rows]
    if any(high <= low for low, high in zip(frequencies, frequencies[1:])):
        raise ValueError("Frequencies must be strictly increasing")
    if not frequencies[0] <= target_frequency <= frequencies[-1]:
        raise ValueError("Objective frequency is outside the exported result band")
    index = bisect.bisect_left(frequencies, target_frequency)
    if index < len(rows) and math.isclose(frequencies[index], target_frequency, rel_tol=0, abs_tol=1e-10):
        sample, interpolation = rows[index], "exact_sample"
    else:
        left, right = rows[index - 1], rows[index]
        if left.keys() != right.keys():
            raise ValueError("Adjacent CSV samples have inconsistent fields")
        weight = (target_frequency - frequencies[index - 1]) / (frequencies[index] - frequencies[index - 1])
        sample = {key: left[key] + weight * (right[key] - left[key]) for key in left}
        interpolation = "linear_complex_interpolation; confirm with an exact frequency sample for final validation"
    s11 = complex(sample["s11_real"], sample["s11_imag"])
    observable = objective.get("observable", objective.get("response", "s11")).lower()
    if observable not in {"s11", "s21"}:
        raise ValueError("Objective observable must be s11 or s21")
    s21 = None
    if {"s21_real", "s21_imag"} <= sample.keys():
        s21 = complex(sample["s21_real"], sample["s21_imag"])
    if observable == "s21" and s21 is None:
        raise ValueError("Transmission objective requires complex S21 data")
    selected = s11 if observable == "s11" else s21
    phase = wrap_phase_deg(math.degrees(cmath.phase(selected))) if abs(selected) > 1e-12 else None
    metrics = {"frequency_ghz": target_frequency, "sample_count": len(rows),
               "s11_real": s11.real, "s11_imag": s11.imag, "s11_db": _db(s11),
               "reflection_magnitude": abs(s11), "s11_phase_deg": wrap_phase_deg(math.degrees(cmath.phase(s11))) if abs(s11) > 1e-12 else None,
               "phase_deg": phase, "observable": observable}
    if s21 is not None:
        metrics.update(s21_db=_db(s21), transmission_magnitude=abs(s21),
                       s21_phase_deg=wrap_phase_deg(math.degrees(cmath.phase(s21))) if abs(s21) > 1e-12 else None)
    if {"cross_real", "cross_imag"} <= sample.keys():
        cross = complex(sample["cross_real"], sample["cross_imag"])
        metrics.update(cross_magnitude=abs(cross), cross_power=abs(cross) ** 2)
    power = sample.get("total_reflected_power")
    if power is not None:
        metrics["total_reflected_power"] = power
        if "estimated_absorbed_power" not in sample:
            metrics["power_balance_error"] = abs(1.0 - power)
    elif s21 is not None:
        power = abs(s11) ** 2 + abs(s21) ** 2
        metrics["two_port_power"] = power
        if "estimated_absorbed_power" not in sample:
            metrics["power_balance_error"] = abs(1.0 - power)
    physical_checks = {}
    if power is not None:
        physical_checks["nonnegative_power"] = power >= 0
        physical_checks["passivity_with_5_percent_margin"] = power <= 1.05
    if "estimated_absorbed_power" in sample:
        metrics["estimated_absorbed_power"] = sample["estimated_absorbed_power"]
        metrics["absorption_scope"] = "Estimated unreturned power from selected S-parameters; independent dissipated-power balance not verified"
        if power is None:
            raise ValueError("Estimated absorbed power requires exported outgoing power")
        if interpolation == "exact_sample":
            physical_checks["absorption_estimate_matches_export"] = math.isclose(sample["estimated_absorbed_power"], 1.0 - power,
                                                                                rel_tol=1e-5, abs_tol=1e-8)
    if "total_reflected_power" in sample and "cross_power" in metrics and interpolation == "exact_sample":
        modal_sum = abs(s11) ** 2 + metrics["cross_power"]
        physical_checks["modal_power_matches_export"] = math.isclose(power, modal_sum, rel_tol=1e-5, abs_tol=1e-8)
    mapping = {"min_reflection_magnitude": ("reflection_magnitude", "min"),
               "max_reflection_magnitude": ("reflection_magnitude", "max"),
               "min_s11_db": ("s11_db", "min"), "max_s11_db": ("s11_db", "max"),
               "min_s21_db": ("s21_db", "min"), "max_s21_db": ("s21_db", "max"),
               "max_cross_power": ("cross_power", "max"),
               "min_estimated_absorbed_power": ("estimated_absorbed_power", "min"),
               "max_estimated_absorbed_power": ("estimated_absorbed_power", "max"),
               "max_power_balance_error": ("power_balance_error", "max")}
    checks = {}
    for name, threshold in constraints.items():
        if name not in mapping:
            raise ValueError(f"Unsupported automated constraint: {name}")
        metric, direction = mapping[name]
        threshold = _finite(threshold, name)
        measured = metrics.get(metric)
        passed = measured is not None and (measured >= threshold if direction == "min" else measured <= threshold)
        checks[name] = {"value": measured, "threshold": threshold, "passed": passed}
    target_phase = objective.get("target_phase_deg")
    tolerance = _finite(objective.get("tolerance_deg", 2.0), "objective.tolerance_deg")
    if tolerance <= 0 or tolerance > 180:
        raise ValueError("Phase tolerance must be greater than 0 and at most 180 degrees")
    phase_error = None if phase is None or target_phase is None else abs(wrap_phase_deg(phase - _finite(target_phase, "objective.target_phase_deg")))
    metrics["phase_error_deg"] = phase_error
    physical_ok = all(physical_checks.values())
    independent_invalid = isinstance(numerical_check, dict) and numerical_check.get("passed") is False
    data_usable = bool(solver_success) and physical_ok and not independent_invalid and phase is not None
    constraints_met = all(check["passed"] for check in checks.values())
    target_met = data_usable and constraints_met and phase_error is not None and phase_error <= tolerance
    science = {"solver": "success" if solver_success else "not_confirmed",
               "data_integrity": "passed", "physical_consistency": "passed" if physical_ok else "failed",
               "independent_numerical_check": "passed" if numerical_check and numerical_check.get("passed") else "failed" if independent_invalid else "not_performed",
               "mesh_convergence": "unknown", "physical_validation": "not_performed",
               "numerical_validity": "pending" if data_usable else "invalid"}
    return {"analysis_version": 1, "metrics": metrics, "constraints": checks,
            "physical_checks": physical_checks, "constraints_met": constraints_met,
            "target_met": target_met, "target_status": "provisional_requires_validation" if target_met else "not_met",
            "usable_for_optimization": data_usable, "scientific_status": science,
            "interpolation": interpolation, "source_csv": str(Path(csv_path).resolve()),
            "evidence_boundary": "Single-mesh numerical screening; a target hit requires convergence and independent confirmation."}


def analyze_run(run: dict) -> dict:
    directory = Path(run["run_directory"])
    result_path = directory / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else run.get("details", {}).get("result", {})
    csv_path = next((directory / name for name in ("reflection.csv", "sparameters.csv") if (directory / name).is_file()), None)
    details = run.get("details", {})
    run_info = details.get("solver_run_info", details.get("solver_info", result.get("solver_run_info", result.get("solver_info", {}))))
    solver_success = result.get("solver_completed") is True or result.get("solver_success") is True or (isinstance(run_info, dict) and run_info.get("state") == "SUCCESS")
    objective = run["spec"].get("objective", {})
    is_phase_objective = isinstance(objective, dict) and "target_phase_deg" in objective
    if not is_phase_objective:
        return {"analysis_version": 1, "run_id": run["run_id"], "parameters": run["job"].get("parameters", {}),
                "spec_version": run.get("spec_version"), "usable_for_optimization": False, "target_met": False,
                "target_status": "not_evaluated", "analysis_required": "Codex or a registered analysis for these custom metrics",
                "exports": result.get("exports", result.get("result_files", result.get("exported_results", []))),
                "scientific_status": {"solver": "success" if solver_success else "not_confirmed",
                    "data_integrity": "exported_not_independently_checked", "numerical_validity": "not_evaluated",
                    "mesh_convergence": "unknown", "physical_validation": "not_performed"},
                "evidence_boundary": "Custom result export is recorded without assigning unsupported objective scores."}
    if csv_path is None:
        raise FileNotFoundError("This run has no exported reflection.csv or sparameters.csv")
    numerical_check = result.get("numerical_check")
    backend_validity = result.get("numerical_validity")
    # A backend's explicit failed validator must not be hidden by a legacy pass
    # field or by the solver's SUCCESS receipt.
    if isinstance(backend_validity, dict) and (numerical_check is None or backend_validity.get("passed") is False):
        numerical_check = backend_validity
    if numerical_check is None and (directory / "numerical_check.json").exists():
        numerical_check = json.loads((directory / "numerical_check.json").read_text(encoding="utf-8"))
    analysis = analyze_sparameters(csv_path, run["spec"], solver_success=solver_success, numerical_check=numerical_check)
    analysis.update(run_id=run["run_id"], parameters=run["job"].get("parameters", {}), spec_version=run.get("spec_version"))
    metadata = directory / "result_metadata.json"
    if metadata.exists():
        analysis["result_metadata"] = json.loads(metadata.read_text(encoding="utf-8"))
    return analysis


def _analysis(run: dict) -> dict:
    return run.get("analysis") or run.get("details", {}).get("analysis") or {}


def comparison_rows(runs: list[dict]) -> list[dict]:
    return [{"run_id": run["run_id"], "spec_version": run.get("spec_version"),
             "state": run["state"], "parameters": run.get("job", {}).get("parameters", {}),
             "decision": run.get("decision", {}), "metrics": _analysis(run).get("metrics", {}),
             "target_status": _analysis(run).get("target_status", "unavailable"),
             "scientific_status": _analysis(run).get("scientific_status", {})} for run in runs]


def propose_next(spec: dict, runs: list[dict]) -> dict:
    """Deterministic bounded coarse scan then local bisection, never a global-optimum claim."""
    base = {"comparison": comparison_rows(runs), "optimizer": "bounded_coarse_then_local_refine_v1"}
    if any(run["state"] not in {"completed", "failed", "cancelled"} for run in runs):
        return {**base, "action": "wait", "reason": "A queued, active or unresolved run still owns the experiment."}
    consecutive_failures = 0
    for run in reversed(runs):
        if run["state"] == "failed" or (run["state"] == "completed" and not _analysis(run).get("usable_for_optimization", False)):
            consecutive_failures += 1
        else:
            break
    if consecutive_failures >= 2:
        return {**base, "action": "stop", "reason": "Two consecutive failed or unusable runs require review.", "stop_code": "consecutive_failures"}
    budgets = spec.get("budgets", {})
    used = 0.0
    for run in runs:
        allocation = run.get("job", {}).get("timeout_seconds", budgets.get("max_run_solver_seconds", 3600))
        elapsed = run.get("details", {}).get("solver_elapsed_seconds", allocation)
        used += elapsed if isinstance(elapsed, (float, int)) and not isinstance(elapsed, bool) and math.isfinite(elapsed) and elapsed >= 0 else allocation
    remaining = budgets.get("max_total_solver_seconds", 14400) - used
    objective = spec.get("objective", {})
    if not isinstance(objective, dict) or "target_phase_deg" not in objective or "frequency_ghz" not in objective:
        return {**base, "action": "stop", "reason": "Set a structured phase target before automated proposals.", "stop_code": "objective_required"}
    bounds = spec.get("parameter_bounds", {})
    if len(bounds) != 1:
        return {**base, "action": "stop", "reason": "The current deterministic optimizer requires exactly one bounded parameter.", "stop_code": "one_parameter_required"}
    parameter, bound = next(iter(bounds.items()))
    low, high = (bound["min"], bound["max"]) if isinstance(bound, dict) else bound
    # Revised objectives must not score measurements against a stale target or constraints.
    compatible = [run for run in runs if run.get("spec", spec).get("objective") == objective
                  and run.get("spec", spec).get("constraints", {}) == spec.get("constraints", {})]
    usable = [run for run in compatible if run["state"] == "completed" and _analysis(run).get("usable_for_optimization")]
    feasible = [run for run in usable if _analysis(run).get("constraints_met")]
    rankable = feasible or usable
    rankable = [run for run in rankable if _analysis(run).get("metrics", {}).get("phase_error_deg") is not None]
    best = min(rankable, key=lambda run: _analysis(run)["metrics"]["phase_error_deg"], default=None)
    if best:
        base["best_run_id"] = best["run_id"]
        base["best_phase_error_deg"] = _analysis(best)["metrics"]["phase_error_deg"]
        if _analysis(best).get("target_met"):
            return {**base, "action": "stop", "reason": "A single-mesh target hit needs a convergence/independent confirmation run.",
                    "stop_code": "target_requires_validation", "requires_validation": True}
    if len(runs) >= budgets.get("max_runs", 8):
        return {**base, "action": "stop", "reason": "Run budget exhausted.", "stop_code": "run_budget"}
    if remaining < budgets.get("max_run_solver_seconds", 3600):
        return {**base, "action": "stop", "reason": "Remaining solver budget cannot reserve another run.", "stop_code": "solver_budget"}
    values = [run.get("job", {}).get("parameters", {}).get(parameter) for run in runs]
    tested = [float(value) for value in values if isinstance(value, (float, int)) and not isinstance(value, bool)]
    tolerance = max(abs(high - low) * 1e-9, 1e-12)
    def untried(value):
        return all(abs(value - prior) > tolerance for prior in tested)
    # Center first supplies a baseline; bounds and quartiles follow reproducibly.
    grid = [(low + high) / 2, low, high, low + (high - low) / 4, low + 3 * (high - low) / 4]
    candidate = next((value for value in grid if untried(value)), None)
    if candidate is not None:
        return {**base, "action": "submit", "stage": "coarse", "parameters": {parameter: candidate},
                "reason": "Measure an untested point in the fixed five-point coarse scan.", "predicted_improvement": "unknown; exploratory measurement"}
    if best is None:
        return {**base, "action": "stop", "reason": "Coarse scan produced no usable candidate.", "stop_code": "no_usable_candidate"}
    center = float(best["job"]["parameters"][parameter])
    neighbors = sorted(set([float(low), float(high)] + [value for value in tested if low <= value <= high]))
    left = max((value for value in neighbors if value < center), default=center)
    right = min((value for value in neighbors if value > center), default=center)
    candidates = sorted({(left + center) / 2, (center + right) / 2}, key=lambda value: (-abs(value - center), value))
    candidate = next((value for value in candidates if low <= value <= high and untried(value)), None)
    if candidate is None:
        return {**base, "action": "stop", "reason": "No unresolved local interval remains.", "stop_code": "resolution_limit"}
    return {**base, "action": "submit", "stage": "refine", "parameters": {parameter: candidate},
            "reason": "Bisect an adjacent untested interval around the best measured candidate, prioritizing feasible results; phase response may be nonmonotonic.",
            "predicted_improvement": "hypothesis requiring the next CST run"}
