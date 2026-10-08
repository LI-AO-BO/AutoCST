import cmath
import csv
import json
import math
from pathlib import Path
import tempfile
import unittest

from autocst.research_analysis import analyze_run, analyze_sparameters, propose_next, wrap_phase_deg


def spec():
    return {"objective": {"frequency_ghz": 10, "target_phase_deg": -90, "tolerance_deg": 2},
            "constraints": {"min_reflection_magnitude": 0.9},
            "parameter_bounds": {"patch_width_mm": [2, 8]},
            "budgets": {"max_runs": 12, "max_total_solver_seconds": 14400, "max_run_solver_seconds": 3600}}


class ResearchAnalysisTests(unittest.TestCase):
    def csv_file(self, phases, magnitudes=None, frequencies=None, powers=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "reflection.csv"
        magnitudes = magnitudes or [1] * len(phases)
        frequencies = frequencies or [10] * len(phases)
        powers = powers or [value * value for value in magnitudes]
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["frequency_ghz", "s11_real", "s11_imag", "cross_real", "cross_imag", "total_reflected_power"])
            for frequency, phase, magnitude, power in zip(frequencies, phases, magnitudes, powers):
                z = magnitude * cmath.exp(1j * math.radians(phase))
                writer.writerow([frequency, z.real, z.imag, 0, 0, power])
        return path

    def test_phase_wrap_and_target_hit_are_provisional(self):
        self.assertEqual(wrap_phase_deg(181), -179)
        self.assertEqual(wrap_phase_deg(-181), 179)
        report = analyze_sparameters(self.csv_file([-90]), spec())
        self.assertTrue(report["target_met"])
        self.assertTrue(report["usable_for_optimization"])
        self.assertEqual(report["scientific_status"]["mesh_convergence"], "unknown")
        self.assertEqual(report["scientific_status"]["numerical_validity"], "pending")
        self.assertEqual(report["target_status"], "provisional_requires_validation")

    def test_circular_interpolation_does_not_average_179_and_minus179_to_zero(self):
        target = spec()
        target["objective"]["target_phase_deg"] = 180
        path = self.csv_file([179, -179], frequencies=[9, 11])
        report = analyze_sparameters(path, target)
        self.assertLess(report["metrics"]["phase_error_deg"], 1e-10)
        self.assertIn("linear_complex", report["interpolation"])

    def test_amplitude_failure_and_solver_failure_are_separate(self):
        report = analyze_sparameters(self.csv_file([-90], [0.5]), spec())
        self.assertTrue(report["usable_for_optimization"])
        self.assertFalse(report["constraints_met"])
        self.assertFalse(report["target_met"])
        report = analyze_sparameters(self.csv_file([-90]), spec(), solver_success=False)
        self.assertFalse(report["usable_for_optimization"])
        self.assertEqual(report["scientific_status"]["solver"], "not_confirmed")
        report = analyze_sparameters(self.csv_file([-90]), spec(), numerical_check={"passed": False})
        self.assertFalse(report["usable_for_optimization"])

    def test_invalid_power_and_nonfinite_data_cannot_drive_optimization(self):
        report = analyze_sparameters(self.csv_file([-90], [1.2]), spec())
        self.assertFalse(report["usable_for_optimization"])
        self.assertEqual(report["scientific_status"]["physical_consistency"], "failed")
        with self.assertRaises(ValueError):
            analyze_sparameters(self.csv_file([float("nan")]), spec())
        with self.assertRaisesRegex(ValueError, "outside"):
            analyze_sparameters(self.csv_file([-90], frequencies=[9]), spec())

    def test_minimum_reflection_db_constraint(self):
        target = spec()
        target["constraints"] = {"min_s11_db": -1}
        report = analyze_sparameters(self.csv_file([-90], [0.9]), target)
        self.assertTrue(report["constraints_met"])
        self.assertAlmostEqual(report["metrics"]["s11_db"], -0.915149811, places=7)

    def run_record(self, number, value, error=20, state="completed", target_met=False):
        return {"run_id": str(number), "state": state, "spec": spec(),
                "job": {"parameters": {"patch_width_mm": value}, "timeout_seconds": 3600},
                "details": {"solver_elapsed_seconds": 1, "analysis": {
                    "usable_for_optimization": state == "completed", "constraints_met": True,
                    "target_met": target_met, "metrics": {"phase_error_deg": error}}}}

    def test_deterministic_scan_then_local_refinement_and_no_repeat(self):
        runs = []
        selected = []
        for index in range(5):
            proposal = propose_next(spec(), runs)
            self.assertEqual(proposal["stage"], "coarse")
            value = proposal["parameters"]["patch_width_mm"]
            selected.append(value)
            runs.append(self.run_record(index, value, abs(value - 5) + 10))
        self.assertEqual(selected, [5, 2, 8, 3.5, 6.5])
        proposal = propose_next(spec(), runs)
        self.assertEqual(proposal["stage"], "refine")
        self.assertEqual(proposal["parameters"]["patch_width_mm"], 4.25)
        self.assertEqual(proposal["best_run_id"], "0")

    def test_wait_failure_budget_and_target_stops(self):
        self.assertEqual(propose_next(spec(), [self.run_record(0, 5, state="needs_attention")])["action"], "wait")
        self.assertEqual(propose_next(spec(), [self.run_record(i, i + 2, state="failed") for i in range(2)])["stop_code"], "consecutive_failures")
        target = spec()
        target["budgets"]["max_runs"] = 1
        self.assertEqual(propose_next(target, [self.run_record(0, 5)])["stop_code"], "run_budget")
        self.assertTrue(propose_next(spec(), [self.run_record(0, 5, error=0, target_met=True)])["requires_validation"])

    def test_revised_target_does_not_reuse_stale_target_hit(self):
        record = self.run_record(0, 5, error=0, target_met=True)
        target = spec()
        target["objective"]["target_phase_deg"] = 30
        self.assertEqual(propose_next(target, [record])["action"], "submit")

    def test_run_analysis_reads_runner_success_evidence(self):
        path = self.csv_file([-90])
        run = {"run_id": "one", "run_directory": str(path.parent), "spec": spec(),
               "job": {"parameters": {"patch_width_mm": 5}}, "details": {"solver_info": {"state": "SUCCESS"}}}
        self.assertTrue(analyze_run(run)["target_met"])
        run["details"] = {}
        (path.parent / "result.json").write_text(json.dumps({"solver_success": True}), encoding="utf-8")
        self.assertTrue(analyze_run(run)["target_met"])

    def test_custom_result_exports_are_recorded_without_unsupported_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            run = {"run_id": "custom", "run_directory": directory,
                   "spec": {"objective": "Inspect near-field uniformity"},
                   "job": {"kind": "existing_project"},
                   "details": {"result": {"solver_success": True, "exports": ["field.csv"]}}}
            report = analyze_run(run)
            self.assertEqual(report["exports"], ["field.csv"])
            self.assertEqual(report["scientific_status"]["solver"], "success")
            self.assertEqual(report["scientific_status"]["numerical_validity"], "not_evaluated")
            self.assertFalse(report["usable_for_optimization"])
            run["spec"] = spec()
            with self.assertRaises(FileNotFoundError):
                analyze_run(run)

    def test_backend_numerical_failure_vetoes_success_and_legacy_pass(self):
        path = self.csv_file([-90])
        run = {"run_id": "one", "run_directory": str(path.parent), "spec": spec(),
               "job": {"parameters": {"patch_width_mm": 5}}, "details": {}}
        (path.parent / "result.json").write_text(json.dumps({"solver_info": {"state": "SUCCESS"},
            "numerical_check": {"passed": True}, "numerical_validity": {"passed": False}}), encoding="utf-8")
        report = analyze_run(run)
        self.assertEqual(report["scientific_status"]["solver"], "success")
        self.assertEqual(report["scientific_status"]["independent_numerical_check"], "failed")
        self.assertEqual(report["scientific_status"]["mesh_convergence"], "unknown")
        self.assertFalse(report["usable_for_optimization"])
        self.assertFalse(report["target_met"])


if __name__ == "__main__":
    unittest.main()
