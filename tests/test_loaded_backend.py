import csv
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from autocst import research_backend as backend
from autocst.models import WAVEGUIDE_DEFAULTS
from autocst.research_models import METASURFACE_DEFAULTS


def element(resistance=50):
    return {"name": "load_1", "type": "rlcserial", "resistance_ohm": resistance,
            "inductance_nh": 0, "capacitance_pf": 1,
            "point1_mm": [0, 0, 1], "point2_mm": [0, 0, 2], "monitor": True}


class LoadedBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def session(self, kind="waveguide", resistance=50, loaded=True):
        # Isolate the numerical validator from native CST and job normalization.
        session = backend.ResearchSession.__new__(backend.ResearchSession)
        parameters = dict(WAVEGUIDE_DEFAULTS if kind == "waveguide" else METASURFACE_DEFAULTS)
        if kind == "metasurface":
            parameters["frequency_samples"] = 3
        session.job = {"kind": kind, "parameters": parameters}
        if loaded:
            session.job["lumped_elements"] = [element(resistance)]
        session.run_dir = self.directory
        return session

    def test_resistive_waveguide_accepts_attenuation_without_uniform_guide_checks(self):
        session = self.session()
        with patch.object(backend, "validate_waveguide", side_effect=AssertionError("uniform model is inapplicable")):
            check = session._waveguide_check([8.2, 10, 12.4], [0.6j] * 3, [0.4] * 3)
        self.assertTrue(check["passed"])
        self.assertTrue(check["dissipative_loading"])
        self.assertFalse(check["power_balance_verified"])
        self.assertAlmostEqual(check["max_estimated_absorbed_power"], 0.48)
        self.assertNotIn("phase_rms_error_deg_after_constant_offset", check)

    def test_loaded_waveguide_rejects_gain_anywhere_in_band(self):
        session = self.session()
        check = session._waveguide_check([8.2, 10, 12.4], [0j] * 3, [0.5, 0.5, 1.04])
        self.assertFalse(check["passed"])
        self.assertFalse(check["checks"]["passivity_with_5_percent_margin"])

    def test_loaded_waveguide_checks_finite_aligned_data_and_frequency_band(self):
        session = self.session()
        samples = [8.2, 10, 12.4]
        invalid = [(samples, [0j], [0.5] * 3),
                   (samples, [0j, complex(math.nan, 0), 0j], [0.5] * 3),
                   ([8.2, 10, math.inf], [0j] * 3, [0.5] * 3),
                   ([8.2, 10, 10], [0j] * 3, [0.5] * 3),
                   ([8.2, 10, 12], [0j] * 3, [0.5] * 3)]
        for frequency, reflection, transmission in invalid:
            with self.subTest(frequency=frequency), self.assertRaises(RuntimeError):
                session._waveguide_check(frequency, reflection, transmission)

    def test_reactive_waveguide_preserves_lossless_balance_but_allows_reflection(self):
        session = self.session(resistance=0)
        check = session._waveguide_check([8.2, 10, 12.4], [0.8] * 3, [0.6j] * 3)
        self.assertTrue(check["passed"])
        self.assertTrue(check["checks"]["lossless_power_balance_within_5_percent"])
        self.assertFalse(session._waveguide_check([8.2, 10, 12.4], [0.1] * 3, [0.5] * 3)["passed"])

    def test_bare_waveguide_keeps_analytic_validator(self):
        session = self.session(loaded=False)
        with patch.object(backend, "validate_waveguide", return_value={"passed": False, "scope": "bare analytic"}) as check:
            result = session._waveguide_check([8.2, 10, 12.4], [0j] * 3, [1] * 3)
        check.assert_called_once()
        self.assertEqual(result["scope"], "bare analytic")

    def metasurface_curves(self, co=0.6, cross=0.0):
        return {r"1D Results\S-Parameters\SZmax(1),Zmax(1)": ([9.5, 10, 10.5], [co] * 3),
                r"1D Results\S-Parameters\SZmax(2),Zmax(1)": ([9.5, 10, 10.5], [cross] * 3)}

    def test_resistive_metasurface_preserves_estimate_without_claiming_balance(self):
        check = self.session(kind="metasurface")._metasurface_result(self.metasurface_curves())
        self.assertTrue(check["passed"])
        self.assertFalse(check["power_balance_verified"])
        self.assertAlmostEqual(check["min_estimated_absorbed_power"], 0.64)
        self.assertNotIn("max_reflected_power_error", check)
        with (self.directory / "reflection.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertAlmostEqual(float(rows[0]["estimated_absorbed_power"]), 0.64)

    def test_small_negative_estimate_is_retained_and_gain_beyond_margin_rejected(self):
        session = self.session(kind="metasurface")
        check = session._metasurface_result(self.metasurface_curves(co=math.sqrt(1.02)))
        self.assertTrue(check["passed"])
        self.assertAlmostEqual(check["min_estimated_absorbed_power"], -0.02)
        self.assertFalse(session._metasurface_result(self.metasurface_curves(co=math.sqrt(1.06)))["passed"])

    def test_bare_and_reactive_metasurface_retain_lossless_balance(self):
        for loaded in (False, True):
            session = self.session(kind="metasurface", resistance=0, loaded=loaded)
            self.assertFalse(session._metasurface_result(self.metasurface_curves())["passed"])
            self.assertTrue(session._metasurface_result(self.metasurface_curves(co=0.6, cross=0.8))["passed"])
            with (self.directory / "reflection.csv").open(newline="") as stream:
                self.assertNotIn("estimated_absorbed_power", next(csv.reader(stream)))

    def saved_session(self, kind="metasurface"):
        session = self.session(kind=kind)
        session.job["solve"] = True
        session.project_path = self.directory / "project.cst"
        session.project_path.write_bytes(b"saved solver fixture")
        session.project = None
        (self.directory / "solver_saved.json").write_text(json.dumps({
            "solver_success": True, "solver_info": {"state": "SUCCESS"},
            "project_sha256": hashlib.sha256(session.project_path.read_bytes()).hexdigest()}))
        return session

    def test_dissipative_metasurface_still_requires_observed_adaptive_success(self):
        session = self.saved_session()
        log = self.directory / "project" / "Result" / "Model.log"
        log.parent.mkdir(parents=True)
        log.write_text('Adaptive mesh refinement pass 8\nStimulation port : Zmax\nMode number : 1\n'
                       'All S-Parameters : 0.05\nCalculation finished successfully.\n')
        with patch.object(session, "_export_curves", return_value=self.metasurface_curves()):
            result = session.finish()
        check = result["numerical_validity"]
        self.assertTrue(check["passivity_check_passed"])
        self.assertIsNone(check["power_balance_passed"])
        self.assertFalse(check["passed"])
        self.assertEqual(check["status"], "adaptation_not_converged")

    def test_resistive_waveguide_export_records_estimated_absorption(self):
        session = self.saved_session(kind="waveguide")
        curves = {r"1D Results\S-Parameters\S1,1": ([8.2, 10, 12.4], [0.6] * 3),
                  r"1D Results\S-Parameters\S2,1": ([8.2, 10, 12.4], [0.4j] * 3)}
        with patch.object(session, "_export_curves", return_value=curves):
            result = session.finish()
        self.assertTrue(result["numerical_validity"]["passed"])
        self.assertTrue(result["lumped_elements"]["dissipative"])
        with (self.directory / "sparameters.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertAlmostEqual(float(rows[0]["estimated_absorbed_power"]), 0.48)

    def test_preparation_persists_resolved_loading_and_history_receipt(self):
        interface = MagicMock()
        de = interface.DesignEnvironment.connect.return_value
        de.pid.return_value = 123
        de.is_connected.return_value = True
        de.list_open_projects.return_value = []
        project = de.new_mws.return_value
        project.model3d.get_active_solver_name.return_value = "HF Frequency Domain"
        project.save.side_effect = lambda *args, **kwargs: (self.directory / "project.cst").write_bytes(b"fixture")
        load = {**element(), "resistance_ohm": "load_resistance_ohm",
                "capacitance_pf": "load_capacitance_pf",
                "point1_mm": [0, 0, 0],
                "point2_mm": [0, 0, METASURFACE_DEFAULTS["substrate_height_mm"] +
                              METASURFACE_DEFAULTS["metal_thickness_mm"]]}
        job = {"kind": "metasurface", "cst_pid": 123, "lumped_elements": [load],
               "parameters": {"load_resistance_ohm": 50, "load_capacitance_pf": 1}}
        with patch.object(backend, "_load_cst", return_value=(self.directory, interface, MagicMock())), \
             patch.object(backend, "get_process_identity", return_value={"alive": True, "creation_time": "456"}):
            backend.ResearchSession(job, self.directory).prepare()
        model = json.loads((self.directory / "model.json").read_text())
        loading = json.loads((self.directory / "lumped_elements.json").read_text())
        self.assertEqual(model["lumped_elements"], loading)
        self.assertTrue(loading["dissipative"])
        self.assertTrue(loading["cst_history_assertions"]["passed"])
        self.assertEqual(loading["elements"][0]["name"], "load_1")
        self.assertEqual(loading["elements"][0]["resistance_ohm"], 50)
        self.assertEqual(loading["elements"][0]["capacitance_pf"], 1)
        self.assertEqual(model["parameters"]["load_resistance_ohm"], 50)
        project.model3d.add_to_history.assert_called_once()


if __name__ == "__main__":
    unittest.main()
