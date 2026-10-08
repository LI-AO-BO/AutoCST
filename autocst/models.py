"""Bounded model templates and independent analytic checks (no CST dependency)."""
from __future__ import annotations

import cmath
import math
from collections.abc import Mapping, Sequence

C0 = 299_792_458.0
WAVEGUIDE_DEFAULTS = {
    "a_mm": 22.86, "b_mm": 10.16, "length_mm": 40.0,
    "fmin_ghz": 8.2, "fmax_ghz": 12.4,
}


def waveguide_parameters(parameters: Mapping | None = None) -> dict[str, float]:
    """Accept numeric parameters only, within a short, single-mode guide envelope."""
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, Mapping):
        raise ValueError("parameters must be an object")
    unknown = set(parameters) - set(WAVEGUIDE_DEFAULTS)
    if unknown:
        raise ValueError(f"Unsupported waveguide parameters: {sorted(unknown)}")
    result = dict(WAVEGUIDE_DEFAULTS)
    for key, value in parameters.items():
        if isinstance(value, bool) or not isinstance(value, (float, int)):
            raise ValueError(f"{key} must be a finite number, not an expression")
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be finite and positive")
        result[key] = float(value)
    a, b, length = (result[k] for k in ("a_mm", "b_mm", "length_mm"))
    if not 1 <= a <= 100 or not max(1, 0.1 * a) <= b < a or not max(1, 0.1 * a) <= length <= min(10 * a, 200):
        raise ValueError("Demo requires 1<=a_mm<=100, max(1,0.1*a)<=b<a, max(1,0.1*a)<=length<=min(10*a,200)")
    fc10 = C0 / (2 * a * 1e-3) / 1e9
    fc_next = min(2 * fc10, C0 / (2 * b * 1e-3) / 1e9)
    if result["fmax_ghz"] > 100 or not 1.02 * fc10 < result["fmin_ghz"] < result["fmax_ghz"] < 0.98 * fc_next:
        raise ValueError("Frequency band must lie above TE10 cutoff and below the next mode cutoff with margin")
    return result


def waveguide_history(parameters: Mapping | None = None) -> str:
    """CST 2025 documented VBA commands; no caller-supplied executable text."""
    p = waveguide_parameters(parameters)
    parameters_vba = "\n".join(f'StoreParameter "{k}", "{v:.12g}"' for k, v in p.items())
    blocks = [parameters_vba, '''ChangeSolverType "HF Time Domain"
With Units
    .Geometry "mm"
    .Frequency "GHz"
    .Time "ns"
End With
With Background
    .Reset
    .Type "pec"
    .XminSpace "0"
    .XmaxSpace "0"
    .YminSpace "0"
    .YmaxSpace "0"
    .ZminSpace "0"
    .ZmaxSpace "0"
End With
Component.New "waveguide"
With Brick
    .Reset
    .Name "air_volume"
    .Component "waveguide"
    .Material "Vacuum"
    .Xrange "0", "a_mm"
    .Yrange "0", "b_mm"
    .Zrange "0", "length_mm"
    .Create
End With
With Boundary
    .Xmin "electric"
    .Xmax "electric"
    .Ymin "electric"
    .Ymax "electric"
    .Zmin "electric"
    .Zmax "electric"
    .Xsymmetry "none"
    .Ysymmetry "none"
    .Zsymmetry "none"
End With''']
    for number, side in ((1, "zmin"), (2, "zmax")):
        blocks.append(f'''With Port
    .Reset
    .PortNumber "{number}"
    .NumberOfModes "1"
    .ReferencePlaneDistance "0"
    .Coordinates "Full"
    .Orientation "{side}"
    .PortOnBound "True"
    .Create
End With''')
    blocks.append('''With Mesh
    .LinesPerWavelength "20"
    .MinimumStepNumber "15"
End With
With Solver
    .FrequencyRange "fmin_ghz", "fmax_ghz"
    .StimulationPort "1"
    .StimulationMode "1"
    .SteadyStateLimit "-40"
    .FrequencySamples "401"
    .MeshAdaption "False"
    .UseDistributedComputing "False"
    .HardwareAcceleration "False"
    .MaximumNumberOfThreads "4"
    .RestartAfterInstabilityAbort "False"
    .AutoNormImpedance "False"
End With''')
    return "\n\n".join(blocks) + "\n"


def te10_phase(f_ghz: float, a_mm: float, length_mm: float) -> float:
    """Ideal matched PEC/vacuum guide transmission phase, radians, exp(+jwt)."""
    k = 2 * math.pi * f_ghz * 1e9 / C0
    kc = math.pi / (a_mm * 1e-3)
    if k <= kc:
        raise ValueError("TE10 is below cutoff")
    return -math.sqrt(k * k - kc * kc) * length_mm * 1e-3


def _unwrap(values: Sequence[float]) -> list[float]:
    output = [values[0]]
    for value in values[1:]:
        delta = (value - output[-1] + math.pi) % (2 * math.pi) - math.pi
        output.append(output[-1] + delta)
    return output


def validate_waveguide(frequency_ghz: Sequence[float], s11: Sequence[complex],
                       s21: Sequence[complex], parameters: Mapping | None = None) -> dict:
    """Smoke-check amplitude, power balance, and phase dispersion, not convergence."""
    p = waveguide_parameters(parameters)
    n = len(frequency_ghz)
    if n < 20 or len(s11) != n or len(s21) != n:
        raise ValueError("At least 20 aligned S11/S21 samples are required")
    if any(not math.isfinite(float(f)) for f in frequency_ghz):
        raise ValueError("Frequency samples must be finite")
    if any(b <= a for a, b in zip(frequency_ghz, frequency_ghz[1:])):
        raise ValueError("Frequency samples must increase strictly")
    if abs(frequency_ghz[0] - p["fmin_ghz"]) > 1e-5 or abs(frequency_ghz[-1] - p["fmax_ghz"]) > 1e-5:
        raise ValueError("Exported frequency band does not match this job's GHz band")
    if any(not math.isfinite(complex(z).real) or not math.isfinite(complex(z).imag) for z in list(s11) + list(s21)):
        raise ValueError("Result contains non-finite values")
    if any(abs(z) == 0 for z in s21):
        raise ValueError("Transmission contains zero values")
    db = lambda z: 20 * math.log10(max(abs(z), 1e-300))
    reflection_db = max(db(z) for z in s11)
    transmission_db = [db(z) for z in s21]
    phase = _unwrap([cmath.phase(z) for z in s21])
    theory = [te10_phase(f, p["a_mm"], p["length_mm"]) for f in frequency_ghz]
    residual = [a - b for a, b in zip(phase, theory)]
    offset = sum(residual) / n
    # Constant modal-reference phase is arbitrary; dispersion is independently constrained.
    phase_rms_deg = math.degrees(math.sqrt(sum((v - offset) ** 2 for v in residual) / n))
    span_error_deg = math.degrees(abs((phase[-1] - phase[0]) - (theory[-1] - theory[0])))
    balance_error = max(abs(abs(r) ** 2 + abs(t) ** 2 - 1) for r, t in zip(s11, s21))
    checks = {
        "reflection_below_minus20_db": reflection_db <= -20,
        "transmission_within_minus0p5_plus0p15_db": min(transmission_db) >= -0.5 and max(transmission_db) <= 0.15,
        "power_balance_within_5_percent": balance_error <= 0.05,
        "phase_rms_below_5_deg": phase_rms_deg <= 5,
        "phase_span_error_below_10_deg": span_error_deg <= 10,
    }
    return {
        "passed": all(checks.values()), "checks": checks, "sample_count": n,
        "max_s11_db": reflection_db, "min_s21_db": min(transmission_db),
        "max_s21_db": max(transmission_db), "max_power_balance_error": balance_error,
        "phase_rms_error_deg_after_constant_offset": phase_rms_deg,
        "phase_span_error_deg": span_error_deg,
        "te10_cutoff_ghz": C0 / (2 * p["a_mm"] * 1e-3) / 1e9,
        "scope": "Single-mesh analytic smoke check; not mesh convergence or physical measurement validation.",
    }
