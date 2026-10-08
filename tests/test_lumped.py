"""Lumped-element units, safe formulas and frozen experiment contracts.

These tests never contact CST. They check the inputs before a solver is allowed
to consume research time, including repeated preparation of a batch template.
"""

from copy import deepcopy
import math
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from autocst.lumped import (
    lumped_metadata,
    normalize_lumped_elements,
    referenced_parameters,
    render_lumped_history,
)
from autocst.models import WAVEGUIDE_DEFAULTS
from autocst.research_models import (
    METASURFACE_DEFAULTS,
    normalize_research_job,
    render_history,
)
from autocst.research_service import ResearchService


def element(**changes):
    return {"name": "load1", "capacitance_pf": 1,
            "point1_mm": [0, 0, 0], "point2_mm": [0, 0, 1], **changes}


def native_value(history, setter):
    match = re.search(r"\." + setter + r'\s+"([^"\n]+)"', history)
    if match is None:
        raise AssertionError(f"Native {setter} value was not present")
    return float(match.group(1))


class LumpedInputTests(unittest.TestCase):
    def test_capacitance_and_inductance_are_converted_to_native_si(self):
        source = [element(resistance_ohm=50, inductance_nh=1, capacitance_pf=1)]
        before = deepcopy(source)
        metadata = lumped_metadata(source, {})
        native = metadata["elements"][0]["native_si"]
        self.assertEqual(native["resistance_ohm"], 50)
        self.assertAlmostEqual(native["inductance_h"], 1e-9, delta=1e-20)
        self.assertAlmostEqual(native["capacitance_f"], 1e-12, delta=1e-23)
        self.assertTrue(metadata["dissipative"])
        history = render_lumped_history(source, {})
        self.assertEqual(native_value(history, "SetR"), 50)
        self.assertAlmostEqual(native_value(history, "SetL"), 1e-9, delta=1e-20)
        self.assertAlmostEqual(native_value(history, "SetC"), 1e-12, delta=1e-23)
        self.assertEqual(source, before)

    def test_safe_formula_references_are_recomputed_each_round(self):
        source = [element(capacitance_pf="(cap + 3) / (2 * scale) - -1",
                          point2_mm=["gap + 1", 0, "height / 2"])]
        self.assertEqual(referenced_parameters(source), {"cap", "scale", "gap", "height"})
        parameters = {"cap": 2, "scale": 0.5, "gap": 1, "height": 4}
        first = lumped_metadata(source, parameters)["elements"][0]
        second = lumped_metadata(source, {**parameters, "cap": 4})["elements"][0]
        self.assertAlmostEqual(first["capacitance_pf"], 6)
        self.assertEqual(first["point2_mm"], [2, 0, 2])
        self.assertAlmostEqual(second["capacitance_pf"], 8)
        self.assertAlmostEqual(native_value(render_lumped_history(source, parameters), "SetC"),
                               6e-12, delta=1e-23)
        self.assertAlmostEqual(native_value(render_lumped_history(source, {**parameters, "cap": 4}), "SetC"),
                               8e-12, delta=1e-23)

    def test_defaults_and_parallel_branch_omission_are_explicit(self):
        default = normalize_lumped_elements([element()], {})[0]
        self.assertEqual(default["type"], "rlcserial")
        self.assertTrue(default["monitor"])
        self.assertEqual(default["resistance_ohm"], 0)
        self.assertEqual(default["inductance_nh"], 0)
        source = [element(type="rlcparallel", resistance_ohm=50,
                          inductance_nh=0, capacitance_pf=0, monitor=False)]
        history = render_lumped_history(source, {})
        self.assertIn('.SetType "rlcparallel"', history)
        self.assertEqual(native_value(history, "SetR"), 50)
        self.assertEqual(native_value(history, "SetL"), 0)
        self.assertEqual(native_value(history, "SetC"), 0)
        self.assertIn('.SetMonitor "False"', history)
        self.assertFalse(lumped_metadata([element()], {})["dissipative"])

    def test_arbitrary_code_and_unsupported_operators_are_rejected(self):
        formulas = ["__import__('os')", "sin(cap)", "cap.real", "[1][0]",
                    "2**3", "2//3", "1%2", "2 << 3", "lambda: 1",
                    "cap if cap else 1", "True", "", '1"\nQuit', "1pF"]
        for formula in formulas:
            with self.subTest(formula=formula), self.assertRaises(ValueError):
                normalize_lumped_elements([element(capacitance_pf=formula)], {"cap": 1})

    def test_missing_references_and_nonfinite_expression_results_are_rejected(self):
        for formula, parameters in [("missing", {}), ("1/0", {}),
                                    ("1e308 * 1e308", {}),
                                    ("cap", {"cap": math.inf}),
                                    ("cap", {"cap": True})]:
            with self.subTest(formula=formula, parameters=parameters), self.assertRaises(ValueError):
                normalize_lumped_elements([element(capacitance_pf=formula)], parameters)

    def test_invalid_values_types_and_empty_circuit_are_rejected(self):
        for field in ("resistance_ohm", "inductance_nh", "capacitance_pf"):
            for value in (-1, True, None, math.nan, math.inf, -math.inf):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    normalize_lumped_elements([element(**{field: value})], {})
        for circuit in ("rlcserial", "rlcparallel"):
            with self.subTest(circuit=circuit), self.assertRaises(ValueError):
                normalize_lumped_elements([element(type=circuit, capacitance_pf=0)], {})
        for bad in [element(type="diode"), element(type=[]), element(type={}),
                    element(monitor=1), element(units="pF"),
                    element(folder="loads"), element(resistance="50 ohm"), None, "load1"]:
            with self.subTest(element=bad), self.assertRaises(ValueError):
                normalize_lumped_elements([bad], {})

    def test_endpoint_dimensions_and_resolved_distance_are_checked(self):
        for field in ("point1_mm", "point2_mm"):
            for value in (None, "0,0,1", [0, 1], [0, 0, 0, 1], [True, 0, 1],
                          [0, math.nan, 1], [0, math.inf, 1]):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    normalize_lumped_elements([element(**{field: value})], {})
        with self.assertRaises(ValueError):
            normalize_lumped_elements([element(point2_mm=[0, 0, 0])], {})
        with self.assertRaises(ValueError):
            normalize_lumped_elements([element(point2_mm=[0, 0, "gap-gap"])], {"gap": 3})
        for field in ("point1_mm", "point2_mm"):
            missing = element()
            del missing[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                normalize_lumped_elements([missing], {})

    def test_safe_unique_ascii_names_are_required(self):
        for name in ("", "load 1", "load-1", "folder:load1", 'load"1', "电容", "1load", "load\nQuit"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                normalize_lumped_elements([element(name=name)], {})
        missing = element()
        del missing["name"]
        with self.assertRaises(ValueError):
            normalize_lumped_elements([missing], {})
        with self.assertRaises(ValueError):
            normalize_lumped_elements([element(), element()], {})
        self.assertEqual(len(normalize_lumped_elements([element(), element(name="load2")], {})), 2)


class LumpedJobTests(unittest.TestCase):
    def test_metasurface_load_cannot_move_port_reference_plane(self):
        top = METASURFACE_DEFAULTS["substrate_height_mm"] + METASURFACE_DEFAULTS["metal_thickness_mm"]
        ground_bottom = -METASURFACE_DEFAULTS["metal_thickness_mm"]
        accepted = {"kind": "metasurface", "lumped_elements": [
            element(point1_mm=[0, 0, ground_bottom], point2_mm=[0, 0, top])]}
        self.assertEqual(normalize_research_job(accepted)["lumped_elements"][0]["point2_mm"][2], top)
        for z in (top + 0.001, top + METASURFACE_DEFAULTS["air_height_mm"], ground_bottom - 0.001):
            with self.subTest(z=z), self.assertRaises(ValueError):
                normalize_research_job({"kind": "metasurface", "lumped_elements": [element(point2_mm=[0, 0, z])]})

    def test_legacy_normalization_shape_is_unchanged(self):
        for kind, defaults in (("metasurface", METASURFACE_DEFAULTS),
                               ("waveguide", WAVEGUIDE_DEFAULTS)):
            with self.subTest(kind=kind):
                job = normalize_research_job({"kind": kind})
                self.assertEqual(set(job), {"kind", "parameters", "cst_pid", "timeout_seconds",
                                           "solve", "history_text", "source_project", "result_queries"})
                self.assertEqual(job["parameters"], defaults)
                self.assertNotIn("With LumpedElement", render_history(job))

    def test_builtin_templates_allow_only_extra_parameters_used_by_elements(self):
        for kind in ("metasurface", "waveguide"):
            with self.subTest(kind=kind):
                job = {"kind": kind, "parameters": {"cap": 2, "gap": 0.5},
                       "lumped_elements": [element(capacitance_pf="cap", point2_mm=[0, 0, "gap"])]}
                normalized = normalize_research_job(job)
                self.assertEqual(normalized["parameters"]["cap"], 2)
                self.assertEqual(normalized["parameters"]["gap"], 0.5)
                history = render_history(job)
                self.assertEqual(history.count("With LumpedElement"), 1)
                self.assertAlmostEqual(native_value(history, "SetC"), 2e-12, delta=1e-23)
                with self.assertRaises(ValueError):
                    normalize_research_job({**job, "parameters": {**job["parameters"], "unrelated": 7}})

    def test_explicit_history_and_elements_render_once(self):
        job = {"kind": "history", "history_text": 'Component.New "user_component"',
               "parameters": {"cap": 3}, "lumped_elements": [element(capacitance_pf="cap")]}
        history = render_history(job)
        self.assertEqual(history.count('Component.New "user_component"'), 1)
        self.assertEqual(history.count("With LumpedElement"), 1)
        self.assertAlmostEqual(native_value(history, "SetC"), 3e-12, delta=1e-23)


class LumpedPreparationTests(unittest.TestCase):
    def fixture(self, root, *, with_elements=True, kind="metasurface"):
        root = Path(root)
        (root / "autocst").mkdir()
        (root / "autocst" / "frozen.py").write_text("# test implementation\n", encoding="utf-8")
        model = {"kind": kind}
        if with_elements:
            model["lumped_elements"] = [element(capacitance_pf="cap")]
        spec = {"objective": {"kind": "phase_target", "frequency_ghz": 10, "target_phase_deg": -90},
                "model": model,
                "parameter_bounds": {"cap": [0.2, 5]} if with_elements else {"patch_mm": [4, 12]},
                "budgets": {"max_runs": 5, "max_total_solver_seconds": 3000,
                            "max_run_solver_seconds": 600}}
        service = ResearchService(root)
        experiment = service.create_experiment(spec)["experiment_id"]
        return service, experiment

    @patch("autocst.research_service.get_process_identity",
           return_value={"pid": 123, "alive": True, "creation_time": "fixture-instance"})
    def test_batch_repreparation_recomputes_elements_without_duplicate_history(self, identity):
        for kind in ("metasurface", "history"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                service, experiment = self.fixture(temp, kind=kind)
                job = {"kind": kind, "parameters": {"cap": 1}, "cst_pid": 123,
                       "lumped_elements": [element(capacitance_pf="cap")]}
                if kind == "history":
                    job["history_text"] = 'Component.New "user_component"'
                first = service.prepare_job(experiment, job)
                first_history = (Path(first["directory"]) / "history.vba").read_text(encoding="utf-8")
                next_job = deepcopy(first["job"])
                next_job["parameters"]["cap"] = 2
                second = service.prepare_job(experiment, next_job)
                second_history = (Path(second["directory"]) / "history.vba").read_text(encoding="utf-8")
                for history in (first_history, second_history):
                    self.assertEqual(history.count("With LumpedElement"), 1)
                    if kind == "history":
                        self.assertEqual(history.count('Component.New "user_component"'), 1)
                self.assertAlmostEqual(native_value(first_history, "SetC"), 1e-12, delta=1e-23)
                self.assertAlmostEqual(native_value(second_history, "SetC"), 2e-12, delta=1e-23)
                self.assertEqual(first["job"]["parameters"]["cap"], 1)
                self.assertEqual(service.store.runs(), [])
                self.assertTrue(second["job"]["execution_history_frozen"])
                if kind == "history":
                    self.assertEqual(second["job"]["base_history_text"], job["history_text"])

    def test_experiment_topology_cannot_be_added_removed_or_changed_silently(self):
        with tempfile.TemporaryDirectory() as temp:
            service, experiment = self.fixture(temp)
            job = {"kind": "metasurface", "parameters": {"cap": 1}, "cst_pid": 123,
                   "lumped_elements": [element(capacitance_pf="cap")]}
            for elements in ([], [element(capacitance_pf="cap", type="rlcparallel")],
                             [element(capacitance_pf="cap", point2_mm=[0, 0, 2])],
                             [element(capacitance_pf="cap", name="load2")]):
                with self.subTest(elements=elements), self.assertRaises(ValueError):
                    service.prepare_job(experiment, {**job, "lumped_elements": elements})
        with tempfile.TemporaryDirectory() as temp:
            service, experiment = self.fixture(temp, with_elements=False)
            with self.assertRaises(ValueError):
                service.prepare_job(experiment, {"kind": "metasurface", "parameters": {"patch_mm": 8},
                                                  "cst_pid": 123, "lumped_elements": [element()]})


if __name__ == "__main__":
    unittest.main()
