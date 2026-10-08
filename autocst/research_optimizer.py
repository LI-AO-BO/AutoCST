"""Reproducible bounded GP/EI proposals; this module never starts a simulation.

GP prediction uses the Cholesky construction in GPML, Algorithm 2.1:
https://gaussianprocess.org/gpml/chapters/RW2.pdf
Expected improvement for minimization follows Snoek et al., equation (2):
https://arxiv.org/pdf/1206.2944
Diagonal noise is both an observation-noise assumption and numerical regularizer:
https://scikit-learn.org/stable/modules/gaussian_process.html

This small isotropic RBF model is a screening surrogate, not a global-optimum or
mesh-convergence certificate. The caller owns persistence, budgets and execution.
"""
from __future__ import annotations

import math
import re

import numpy as np


_METHOD = "bayesian_gp_rbf_expected_improvement_v1"
_DUPLICATE_TOLERANCE = 1e-8
_NOISE_VARIANCE = 1e-6
_CONFIG_KEYS = {"seed", "candidate_count", "initial_samples"}


def _finite(value, name: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)):
        raise ValueError(f"{name} must be a finite number, not a boolean or string")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _integer(value, name: str, low: int, high: int) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} must be an integer")
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f"{name} must lie in [{low}, {high}]")
    return number


def _bounds(spec: dict) -> tuple[list[str], np.ndarray, np.ndarray]:
    bounds = spec.get("parameter_bounds")
    if not isinstance(bounds, dict) or not 1 <= len(bounds) <= 8:
        raise ValueError("Bayesian optimization requires 1 to 8 continuous parameter bounds")
    if any(not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in bounds):
        raise ValueError("Parameter names must be identifiers")
    names = sorted(bounds)
    lower, upper = [], []
    for name in names:
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
            raise ValueError("Parameter names must be identifiers")
        bound = bounds[name]
        if isinstance(bound, dict) and set(bound) == {"min", "max"}:
            low, high = bound["min"], bound["max"]
        elif isinstance(bound, (list, tuple)) and len(bound) == 2:
            low, high = bound
        else:
            raise ValueError(f"Invalid continuous bounds for {name}")
        low, high = _finite(low, f"{name}.min"), _finite(high, f"{name}.max")
        if high <= low or not math.isfinite(high - low):
            raise ValueError(f"{name}.max must exceed min with a finite span")
        lower.append(low)
        upper.append(high)
    return names, np.asarray(lower), np.asarray(upper)


def _analysis(run: dict) -> dict:
    analysis = run.get("analysis") or run.get("details", {}).get("analysis") or {}
    return analysis if isinstance(analysis, dict) else {}


def _compatible(spec: dict, run: dict) -> bool:
    previous = run.get("spec", spec)
    return isinstance(previous, dict) and all(previous.get(key, {}) == spec.get(key, {})
                                               for key in ("objective", "constraints", "model"))


def _invalid_reason(run: dict, analysis: dict) -> str | None:
    if run.get("state") == "failed":
        return "failed_run"
    if run.get("state") != "completed":
        return "not_completed"
    if analysis.get("usable_for_optimization") is not True:
        return "analysis_not_usable"
    science = analysis.get("scientific_status", {})
    if isinstance(science, dict) and any(science.get(key) in {"failed", "invalid", "not_confirmed"}
                                       for key in ("solver", "data_integrity", "numerical_validity",
                                                   "independent_numerical_check", "physical_consistency")):
        return "failed_numerical_evidence"
    if any(value is False for value in analysis.get("physical_checks", {}).values()):
        return "failed_physical_consistency"
    result = run.get("details", {}).get("result", {})
    if isinstance(result, dict) and any(isinstance(result.get(key), dict) and result[key].get("passed") is False
                                        for key in ("numerical_validity", "numerical_check")):
        return "backend_numerical_check_failed"
    return None


def _untried(points: np.ndarray, tested: list[np.ndarray]) -> np.ndarray:
    mask = np.ones(len(points), dtype=bool)
    for prior in tested:
        mask &= np.max(np.abs(points - prior), axis=1) > _DUPLICATE_TOLERANCE
    return points[mask]


def _representable(points: np.ndarray, lower: np.ndarray, upper: np.ndarray) -> np.ndarray:
    # Round through actual parameter floats before checking duplicates; extremely
    # narrow ranges can have no representable interior value.
    values = np.clip(lower + (upper - lower) * points, lower, upper)
    return (values - lower) / (upper - lower)


def _latin_hypercube(count: int, dimension: int, rng) -> np.ndarray:
    points = np.empty((count, dimension))
    for column in range(dimension):
        points[:, column] = (rng.permutation(count) + rng.random(count)) / count
    return points


def _kernel(left: np.ndarray, right: np.ndarray, scale: float) -> np.ndarray:
    distance = np.sum((left[:, None, :] - right[None, :, :]) ** 2, axis=2)
    return np.exp(-0.5 * distance / scale ** 2)


def _predict(training: np.ndarray, scores: np.ndarray, candidates: np.ndarray) -> tuple:
    center, spread = float(np.mean(scores)), max(float(np.std(scores)), 1e-9)
    normalized = (scores - center) / spread
    best = None
    # A fixed, small hyperparameter grid keeps restart decisions reproducible.
    for scale in np.asarray([0.05, 0.1, 0.2, 0.35, 0.5, 0.8, 1.2, 2.0]) * math.sqrt(training.shape[1]):
        covariance = _kernel(training, training, float(scale))
        covariance.flat[::len(training) + 1] += _NOISE_VARIANCE
        factor = np.linalg.cholesky(covariance)
        alpha = np.linalg.solve(factor.T, np.linalg.solve(factor, normalized))
        likelihood = -0.5 * float(normalized @ alpha) - float(np.log(np.diag(factor)).sum())
        if best is None or likelihood > best[0]:
            best = (likelihood, float(scale), factor, alpha)
    _, scale, factor, alpha = best
    cross = _kernel(training, candidates, scale)
    mean = center + spread * (cross.T @ alpha)
    projection = np.linalg.solve(factor, cross)
    deviation = spread * np.sqrt(np.maximum(1 - np.sum(projection ** 2, axis=0), 0))
    improvement = float(scores.min()) - mean
    ratio = improvement / np.maximum(deviation, 1e-15)
    cdf = np.fromiter((0.5 * (1 + math.erf(float(value) / math.sqrt(2))) for value in ratio),
                      dtype=float, count=len(ratio))
    density = np.exp(-0.5 * ratio ** 2) / math.sqrt(2 * math.pi)
    expected = np.maximum(improvement * cdf + deviation * density, 0)
    return mean, deviation, expected, scale, center, spread


def propose_bayesian(spec: dict, runs: list[dict], config: dict) -> dict:
    """Propose one untested point from finite bounds using valid recorded feedback.

    Config: seed (0..2**32-1), candidate_count (64..65536), initial_samples
    (2..64). Numerically valid but infeasible observations receive a documented
    finite penalty above every valid phase error. Pending independent mesh
    validation does not invalidate a screening score. Two consecutive invalid
    outcomes stop the batch; a feasible target hit stops as a candidate only.
    """
    if not isinstance(spec, dict) or not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
        raise ValueError("spec must be an object and runs must be a list of objects")
    if not isinstance(config, dict) or set(config) - _CONFIG_KEYS:
        raise ValueError("Optimizer config only supports seed, candidate_count and initial_samples")
    names, lower, upper = _bounds(spec)
    dimension = len(names)
    seed = _integer(config.get("seed", 0), "seed", 0, 2 ** 32 - 1)
    candidate_count = _integer(config.get("candidate_count", 2048), "candidate_count", 64, 65536)
    initial_samples = _integer(config.get("initial_samples", min(2 * dimension + 1, 8)), "initial_samples", 2, 64)
    objective = spec.get("objective", {})
    base = {"method": _METHOD, "seed": seed, "training_run_ids": [], "penalized_run_ids": [],
            "excluded_runs": [], "score_definition": "phase_error_deg; infeasible=360+error+180*failed_fraction"}
    if not isinstance(objective, dict) or "target_phase_deg" not in objective or "frequency_ghz" not in objective:
        return {**base, "action": "stop", "stop_code": "objective_required",
                "reason": "A structured phase objective is required for the recorded score."}
    _finite(objective["target_phase_deg"], "objective.target_phase_deg")
    frequency = _finite(objective["frequency_ghz"], "objective.frequency_ghz")
    tolerance = _finite(objective.get("tolerance_deg", 2), "objective.tolerance_deg")
    if frequency <= 0 or not 0 < tolerance <= 180:
        raise ValueError("Frequency must be positive and phase tolerance must lie in (0, 180]")
    constraints = spec.get("constraints", {})
    if not isinstance(constraints, dict):
        raise ValueError("constraints must be an object")
    for name, value in constraints.items():
        _finite(value, f"constraints.{name}")
    compatible = [run for run in runs if _compatible(spec, run)]
    if any(run.get("state") not in {"completed", "failed", "cancelled"} for run in compatible):
        return {**base, "action": "wait", "reason": "An active or unresolved run still owns the experiment."}
    span, tested, training, scores, observations = upper - lower, [], [], [], []
    invalid_streak = 0
    target_run = None
    for index, run in enumerate(runs):
        run_id = str(run.get("run_id", f"run-{index + 1}"))
        if not _compatible(spec, run):
            base["excluded_runs"].append({"run_id": run_id, "reason": "incompatible_experiment"})
            continue
        parameters = run.get("job", {}).get("parameters", {})
        analysis = _analysis(run)
        reason = _invalid_reason(run, analysis)
        try:
            point = np.asarray([_finite(parameters[name], f"{run_id}.{name}") for name in names])
            if np.any(point < lower) or np.any(point > upper):
                raise ValueError("Out-of-bounds observation")
            point = (point - lower) / span
            tested.append(point)
        except (KeyError, TypeError, ValueError):
            reason = "invalid_or_missing_parameters"
        try:
            score = _finite(analysis.get("metrics", {}).get("phase_error_deg"), f"{run_id}.phase_error_deg")
            if not 0 <= score <= 180:
                raise ValueError("Phase error must lie in [0,180]")
        except (TypeError, ValueError):
            reason = reason or "invalid_or_missing_score"
        if reason:
            base["excluded_runs"].append({"run_id": run_id, "reason": reason})
            invalid_streak = invalid_streak + 1 if run.get("state") in {"completed", "failed"} else 0
            continue
        invalid_streak = 0
        checks = analysis.get("constraints", {})
        if not isinstance(checks, dict):
            raise ValueError(f"{run_id}.constraints analysis must be an object")
        failed = sum(isinstance(check, dict) and check.get("passed") is not True for check in checks.values())
        feasible = analysis.get("constraints_met") is True and failed == 0
        penalty = 0.0 if feasible else 360.0 + 180.0 * (failed / len(checks) if checks and failed else 1.0)
        training.append(point)
        scores.append(score + penalty)
        base["training_run_ids"].append(run_id)
        if not feasible:
            base["penalized_run_ids"].append(run_id)
        observations.append({"run_id": run_id, "phase_error_deg": score, "score": score + penalty,
                             "constraint_penalty": penalty, "constraints_met": feasible})
        if analysis.get("target_met") is True and feasible and (target_run is None or score < target_run[0]):
            target_run = (score, run_id)
    base["training_observations"] = observations
    if target_run:
        return {**base, "action": "stop", "stop_code": "target_requires_validation", "best_run_id": target_run[1],
                "best_phase_error_deg": target_run[0],
                "requires_validation": True, "candidate_only": True,
                "reason": "A numerically usable feasible target hit is a candidate requiring independent validation."}
    if invalid_streak >= 2:
        return {**base, "action": "stop", "stop_code": "repeated_invalid",
                "reason": "Two consecutive invalid outcomes require review before more simulations."}
    rng = np.random.default_rng(seed)
    design = np.vstack([np.full(dimension, 0.5), _latin_hypercube(initial_samples - 1, dimension, rng)])
    pool = _latin_hypercube(candidate_count, dimension, rng)
    if training:
        best_point = training[int(np.argmin(scores))]
        local_count = candidate_count // 4
        pool[-local_count:] = np.clip(best_point + rng.normal(0, 0.12, size=(local_count, dimension)), 0, 1)
    pool = _untried(_representable(pool, lower, upper), tested)
    if len(training) < initial_samples:
        initial = _untried(_representable(design, lower, upper), tested)
        if len(initial):
            selected = initial[0]
        elif len(pool):
            distances = np.full(len(pool), np.inf)
            for prior in tested:
                distances = np.minimum(distances, np.sum((pool - prior) ** 2, axis=1))
            selected = pool[int(np.argmax(distances))]
        else:
            return {**base, "action": "stop", "stop_code": "candidate_pool_exhausted",
                    "reason": "The reproducible candidate pool contains no untested point."}
        return {**base, "action": "submit", "stage": "initial_design",
                "parameters": {name: float(value) for name, value in zip(names, lower + span * selected)},
                "reason": "Acquire an untested point in the reproducible initial design before fitting the surrogate."}
    if not len(pool):
        return {**base, "action": "stop", "stop_code": "candidate_pool_exhausted",
                "reason": "The reproducible candidate pool contains no untested point."}
    mean, uncertainty, acquisition, scale, center, spread = _predict(np.asarray(training), np.asarray(scores), pool)
    selected = int(np.argmax(acquisition))
    return {**base, "action": "submit", "stage": "bayesian_optimization",
            "parameters": {name: float(value) for name, value in zip(names, lower + span * pool[selected])},
            "predicted_score": float(mean[selected]), "uncertainty": float(uncertainty[selected]),
            "acquisition": float(acquisition[selected]), "candidate_pool_size": len(pool),
            "kernel": {"name": "RBF", "length_scale": scale, "noise_variance": _NOISE_VARIANCE,
                       "input_scaling": "unit_interval", "score_center": center, "score_scale": spread},
            "reason": "Select the untested bounded candidate with the largest expected improvement of the recorded penalized score."}
