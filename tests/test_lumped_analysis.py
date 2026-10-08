import csv
from pathlib import Path
import tempfile
import unittest

from autocst.research_analysis import analyze_sparameters
from autocst.research_models import normalize_research_job
from integration.render_research_report import _mesh_group


class LoadedAnalysisTests(unittest.TestCase):
    def test_absorption_estimate_can_be_a_constraint_without_claiming_energy_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "loaded.csv"
            with path.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["frequency_ghz", "s11_real", "s11_imag", "cross_real", "cross_imag",
                                 "total_reflected_power", "estimated_absorbed_power"])
                writer.writerow([10, 0, -0.5, 0, 0, 0.25, 0.75])
            spec = {"objective": {"frequency_ghz": 10, "target_phase_deg": -90},
                    "constraints": {"min_estimated_absorbed_power": 0.7}}
            result = analyze_sparameters(path, spec, numerical_check={"passed": True})
            self.assertTrue(result["target_met"])
            self.assertEqual(result["metrics"]["estimated_absorbed_power"], 0.75)
            self.assertNotIn("power_balance_error", result["metrics"])
            text = path.read_text().replace("0.25,0.75", "0.25,0.1")
            path.write_text(text)
            self.assertFalse(analyze_sparameters(path, spec)["usable_for_optimization"])

    def test_different_literal_loads_cannot_be_grouped_as_mesh_checks(self):
        sample = {"version": 1, "physical_parameters": {"patch_mm": 9}, "number": 1,
                  "mesh": 100, "lumped_elements": [{"capacitance_pf": 1}]}
        other = {**sample, "number": 2, "mesh": 200, "lumped_elements": [{"capacitance_pf": 2}]}
        self.assertEqual(_mesh_group([sample, other]), [])
        self.assertEqual(len(_mesh_group([sample, {**other, "lumped_elements": sample["lumped_elements"]}])), 2)

    def test_template_endpoints_cannot_change_period_or_solver_domain(self):
        load = {"name": "load1", "capacitance_pf": 1, "point1_mm": [0, 0, 0], "point2_mm": [0, 0, 1]}
        for kind, point in (("metasurface", [100, 0, 1]), ("waveguide", [0, 100, 1])):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "physical domain"):
                normalize_research_job({"kind": kind, "lumped_elements": [{**load, "point2_mm": point}]})
