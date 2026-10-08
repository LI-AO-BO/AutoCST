import unittest

from autocst.research_models import metasurface_parameters, normalize_research_job, render_history


class ResearchModelTests(unittest.TestCase):
    def test_reference_and_floquet_excitation_are_explicit(self):
        text = render_history({"kind": "metasurface"})
        self.assertIn('.AddToExcitationList "Zmax", "TE(0,0)"', text)
        self.assertIn('.SetDistanceToReferencePlane "-air_height_mm"', text)
        self.assertIn('Unexpected Floquet fundamental-mode ordering', text)
        self.assertIn('.MeshAdaptionTet "True"', text)
        self.assertIn('.MaxDeltaS "0.02"', text)
        self.assertIn('"1", "Single", "True"', text)
        self.assertNotIn('FDSolver.Start', text)

    def test_diffraction_and_geometry_bounds(self):
        for parameters in ({"fmax_ghz": 21}, {"patch_mm": 15}, {"epsilon_r": 0}, {"frequency_samples": 2.5}):
            with self.assertRaises(ValueError):
                metasurface_parameters(parameters)

    def test_long_job_budget_supported(self):
        job = normalize_research_job({"kind": "metasurface", "timeout_seconds": 86400,
                                      "cst_creation_time": "123", "script_sha256": "abc"})
        self.assertEqual(job["timeout_seconds"], 86400)
        self.assertEqual(job["cst_creation_time"], "123")
        self.assertEqual(job["script_sha256"], "abc")

    def test_mesh_resolution_is_reviewable_and_bounded(self):
        text = render_history({"kind": "metasurface", "parameters": {"mesh_steps_per_wavelength": 20}})
        self.assertIn('.StepsPerWavelengthTet "20"', text)
        with self.assertRaises(ValueError):
            metasurface_parameters({"mesh_steps_per_wavelength": 1000})

    def test_explicit_history_is_preserved(self):
        script = 'Component.New "user_component"'
        job = normalize_research_job({"kind": "history", "history_text": script,
                                      "result_queries": [r"1D Results\custom"]})
        self.assertEqual(job["history_text"], script)
        self.assertEqual(job["result_queries"][0]["run_id"], 0)

    def test_geometric_mesh_control_uses_current_settings_and_readback(self):
        text = render_history({"kind": "metasurface", "parameters": {"mesh_cells_per_box": 20}})
        self.assertIn('.Set "StepsPerBoxNear", "20"', text)
        self.assertIn('.Set "StepsPerBoxFar", "20"', text)
        self.assertIn('.Get("StepsPerBoxNear")', text)
        self.assertIn('.StepsPerWavelengthTet "10"', text)
        for value in (True, 7, 20.5, 41):
            with self.assertRaises(ValueError):
                metasurface_parameters({"mesh_cells_per_box": value})

    def test_parameter_injection_and_boolean_pid_rejected(self):
        for job in ({"kind": "metasurface", "parameters": {"patch_mm": '10"\nQuit'}},
                    {"kind": "history", "history_text": "x", "parameters": {'a"': 1}},
                    {"kind": "metasurface", "cst_pid": True}):
            with self.assertRaises(ValueError):
                normalize_research_job(job)


if __name__ == "__main__":
    unittest.main()
