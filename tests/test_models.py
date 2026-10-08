import cmath
import math
import unittest
from pathlib import Path
import tempfile
import types
from unittest.mock import MagicMock, patch

from autocst.models import C0, te10_phase, validate_waveguide, waveguide_history, waveguide_parameters
from autocst import cst_backend


class WaveguideTests(unittest.TestCase):
    def test_band_rejects_cutoff_and_multimode(self):
        for parameters in ({"fmin_ghz": 6.0}, {"fmax_ghz": 14.0}, {"length_mm": 201}):
            with self.assertRaises(ValueError):
                waveguide_parameters(parameters)

    def test_code_and_nonfinite_parameters_rejected(self):
        for value in ('22.86"\nQuit', math.nan, math.inf, True):
            with self.assertRaises(ValueError):
                waveguide_history({"a_mm": value})

    def test_analytic_signal_passes_and_wrong_length_fails(self):
        x = [8.2 + 4.2 * i / 100 for i in range(101)]
        reflection = [0j] * len(x)
        matched = [cmath.exp(1j * (te10_phase(f, 22.86, 40) + 0.7)) for f in x]
        wrong = [cmath.exp(1j * te10_phase(f, 22.86, 20)) for f in x]
        self.assertTrue(validate_waveguide(x, reflection, matched)["passed"])
        self.assertFalse(validate_waveguide(x, reflection, wrong)["passed"])

    def test_attenuation_and_reflection_fail(self):
        x = [8.2 + 4.2 * i / 100 for i in range(101)]
        transmission = [0.8 * cmath.exp(1j * te10_phase(f, 22.86, 40)) for f in x]
        self.assertFalse(validate_waveguide(x, [0.3 + 0j] * len(x), transmission)["passed"])

    def test_wrong_units_or_frequency_order_fail(self):
        x = [8.2 + 4.2 * i / 100 for i in range(101)]
        for invalid in ([v * 1e9 for v in x], list(reversed(x))):
            with self.assertRaises(ValueError):
                validate_waveguide(invalid, [0j] * len(x), [1 + 0j] * len(x))

    def test_te10_cutoff(self):
        self.assertAlmostEqual(C0 / (2 * 22.86e-3) / 1e9, 6.557140376202975)
        with self.assertRaises(ValueError):
            te10_phase(6, 22.86, 40)


class CSTOwnershipTests(unittest.TestCase):
    def fake_cst(self):
        package = types.ModuleType("cst")
        interface = types.ModuleType("cst.interface")
        results = types.ModuleType("cst.results")
        de = MagicMock()
        de.pid.return_value = 36684
        de.is_connected.return_value = True
        project = de.new_mws.return_value
        project.model3d.get_active_solver_name.return_value = "HF Time Domain"
        project.get_messages.return_value = []
        interface.DesignEnvironment = MagicMock()
        interface.DesignEnvironment.connect.return_value = de
        package.interface = interface
        package.results = results
        return {"cst": package, "cst.interface": interface, "cst.results": results}, interface, de, project

    def test_explicit_attach_only_closes_new_project(self):
        modules, interface, de, project = self.fake_cst()
        with tempfile.TemporaryDirectory() as directory, patch.dict("sys.modules", modules), \
             patch.object(cst_backend, "find_cst", return_value=Path(directory)), \
             patch.object(cst_backend, "launch_permission_diagnostic", return_value={"blocked": True}):
            result = cst_backend.run_cst({"kind": "waveguide", "solve": False, "cst_pid": 36684},
                                        Path(directory) / "run", lambda event, data: None)
        self.assertEqual(result["status"], "model_built_without_solver")
        interface.DesignEnvironment.connect.assert_called_once_with(36684)
        interface.DesignEnvironment.new.assert_not_called()
        de.set_quiet_mode.assert_not_called()
        de.close.assert_not_called()
        project.close.assert_called_once()

    def test_cleanup_failure_preserved_when_diagnostic_write_fails(self):
        modules, interface, de, project = self.fake_cst()
        project.close.side_effect = RuntimeError("project close failed")
        original_json = cst_backend._json
        def diagnostic_write(path, value):
            if path.name == "cleanup_required.json":
                raise OSError("disk full")
            original_json(path, value)
        with tempfile.TemporaryDirectory() as directory, patch.dict("sys.modules", modules), \
             patch.object(cst_backend, "find_cst", return_value=Path(directory)), \
             patch.object(cst_backend, "launch_permission_diagnostic", return_value={"blocked": True}), \
             patch.object(cst_backend, "_json", side_effect=diagnostic_write):
            with self.assertRaises(cst_backend.CSTCleanupRequired) as raised:
                cst_backend.run_cst({"kind": "waveguide", "solve": False, "cst_pid": 36684},
                                   Path(directory) / "run", lambda event, data: None)
            self.assertTrue(raised.exception.cleanup_required)
        de.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
