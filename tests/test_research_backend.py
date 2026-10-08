import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from autocst import research_backend as backend


class ResearchBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.interface = MagicMock()
        self.de = self.interface.DesignEnvironment.connect.return_value
        self.de.pid.return_value = 123
        self.de.is_connected.return_value = True
        self.de.list_open_projects.return_value = ["user_original.cst"]
        self.project = self.de.new_mws.return_value
        self.de.get_open_project.return_value = self.project
        self.project.filename.return_value = str(self.directory / "project.cst")
        self.project.model3d.get_active_solver_name.return_value = "HF Frequency Domain"
        self.project.model3d.is_solver_running.return_value = False
        self.project.model3d.get_solver_run_info.return_value = {"state": "SUCCESS"}
        self.project.model3d.get_tree_items.return_value = [r"1D Results\S-Parameters\SZmax(1),Zmax(1)"]
        self.project.get_messages.return_value = []
        self.project.save.side_effect = lambda *args, **kwargs: (self.directory / "project.cst").write_bytes(b"CST fixture")
        for context in (
            patch.object(backend, "_load_cst", return_value=(self.directory, self.interface, MagicMock())),
            patch.object(backend, "get_process_identity", return_value={"pid": 123, "alive": True, "creation_time": "777"}),
            patch.object(backend, "matches_process", return_value=True),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.job = {"kind": "metasurface", "cst_pid": 123}

    def test_recovery_never_opens_or_restarts(self):
        session = backend.ResearchSession(self.job, self.directory)
        binding = session.prepare()
        self.assertTrue((self.directory / "prepare_completed.json").exists())
        self.de.get_open_project.reset_mock()
        recovered = backend.ResearchSession(self.job, self.directory).recover(binding)
        self.de.get_open_project.assert_called_once_with(str(self.directory / "project.cst"))
        self.de.open_project.assert_not_called()
        self.project.model3d.start_solver.assert_not_called()
        self.assertIs(recovered.project, self.project)

    def test_start_is_at_most_once(self):
        session = backend.ResearchSession(self.job, self.directory)
        session.prepare()
        session.start()
        with self.assertRaises(FileExistsError):
            session.start()
        self.project.model3d.start_solver.assert_called_once_with(timeout=None)

    def test_native_wait_completes_once_without_asynchronous_start(self):
        session = backend.ResearchSession(self.job, self.directory)
        session.prepare()
        status = session.solve_wait()
        self.assertTrue(status["fresh_run_confirmed"])
        self.assertTrue((self.directory / "solver_completed.json").exists())
        self.assertTrue((self.directory / "solver_waiting.json").exists())
        self.project.model3d.run_solver.assert_called_once_with(timeout=None)
        self.project.model3d.start_solver.assert_not_called()
        with self.assertRaises(FileExistsError):
            session.start()
        with self.assertRaises(FileExistsError):
            session.solve_wait()

    def test_native_wait_error_retains_intent_and_never_retries(self):
        session = backend.ResearchSession(self.job, self.directory)
        session.prepare()
        self.project.model3d.run_solver.side_effect = RuntimeError("solver failed")
        with self.assertRaisesRegex(RuntimeError, "solver failed"):
            session.solve_wait()
        self.assertTrue((self.directory / "solver_wait_error.json").exists())
        self.assertFalse((self.directory / "solver_completed.json").exists())
        with self.assertRaises(FileExistsError):
            session.solve_wait()
        self.project.model3d.run_solver.assert_called_once_with(timeout=None)

    def test_pid_reuse_rejected_without_reopen(self):
        session = backend.ResearchSession(self.job, self.directory)
        binding = session.prepare()
        with patch.object(backend, "matches_process", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "PID was reused"):
                backend.ResearchSession(self.job, self.directory).recover(binding)
        self.de.open_project.assert_not_called()

    def test_build_only_preserves_user_application(self):
        job = {**self.job, "solve": False}
        session = backend.ResearchSession(job, self.directory)
        session.prepare()
        result = session.finish()
        self.assertEqual(result["status"], "model_only")
        self.project.close.assert_called_once()
        self.de.close.assert_not_called()
        self.assertTrue((self.directory / "solver_saved.json").exists())
        self.assertTrue((self.directory / "project_closed.json").exists())

    def test_closed_saved_job_can_finish_without_live_cst(self):
        job = {**self.job, "solve": False}
        session = backend.ResearchSession(job, self.directory)
        binding = session.prepare()
        session.finish()
        (self.directory / "research_result.json").unlink()
        with patch.object(backend, "matches_process", side_effect=AssertionError("must not inspect PID")):
            recovered = backend.ResearchSession(job, self.directory).recover(binding)
            result = recovered.finish()
        self.assertEqual(result["status"], "model_only")

    def test_cancel_uses_only_bound_project_and_confirms_stop(self):
        session = backend.ResearchSession(self.job, self.directory)
        session.prepare()
        self.project.model3d.is_solver_running.side_effect = [True, False]
        result = session.cancel()
        self.assertTrue(result["confirmed_stopped"])
        self.project.model3d.abort_solver.assert_called_once_with(timeout=None)
        self.project.close.assert_called_once()
        self.de.close.assert_not_called()

    def test_power_check_includes_cross_polarization(self):
        job = {**self.job, "parameters": {"frequency_samples": 3}}
        session = backend.ResearchSession(job, self.directory)
        curves = {r"1D Results\S-Parameters\SZmax(1),Zmax(1)": ([9.5, 10, 10.5], [0.6+0j] * 3),
                  r"1D Results\S-Parameters\SZmax(2),Zmax(1)": ([9.5, 10, 10.5], [0.8+0j] * 3)}
        check = session._metasurface_result(curves)
        self.assertTrue(check["passed"])
        self.assertAlmostEqual(check["max_reflected_power_error"], 0)

    def test_solver_log_does_not_accept_an_unrequested_source(self):
        text = ('Adaptive mesh refinement pass 3\nNumber of mesh cells : 12000\n'
                'Stimulation port : Zmax\nMode number : 1\nAll S-Parameters : 0.01\n'
                'Mesh adaptation terminated because the desired accuracy limit is reached.\n')
        evidence = backend._parse_solver_log(text)
        self.assertTrue(evidence["only_zmax_mode_1"])
        self.assertTrue(evidence["adaptive_accuracy_limit_reached"])
        self.assertEqual(evidence["final_mesh_cells"], 12000)
        self.assertFalse(backend._parse_solver_log(text + 'Stimulation port : Zmax\nMode number : 2')['only_zmax_mode_1'])
        self.assertFalse(backend._parse_solver_log('')['only_zmax_mode_1'])

    def test_metasurface_export_requires_observed_excitation(self):
        session = backend.ResearchSession(self.job, self.directory)
        session.prepare()
        session.start()
        with self.assertRaisesRegex(RuntimeError, "requested Zmax mode 1"):
            session.finish()
        self.assertTrue((self.directory / "project_closed.json").exists())
        self.de.close.assert_not_called()

    def test_unconverged_adaptation_keeps_data_but_fails_numerical_check(self):
        job = {**self.job, "parameters": {"frequency_samples": 3}}
        session = backend.ResearchSession(job, self.directory)
        session.prepare()
        session.start()
        log = self.directory / "project" / "Result" / "Model.log"
        log.parent.mkdir(parents=True)
        log.write_text('Adaptive mesh refinement pass 8\nStimulation port : Zmax\nMode number : 1\n'
                       'All S-Parameters : 0.05\nCalculation finished successfully.\n')
        curves = {r"1D Results\S-Parameters\SZmax(1),Zmax(1)": ([9.5, 10, 10.5], [1+0j] * 3),
                  r"1D Results\S-Parameters\SZmax(2),Zmax(1)": ([9.5, 10, 10.5], [0j] * 3)}
        with patch.object(session, "_export_curves", return_value=curves):
            result = session.finish()
        self.assertTrue(result["solver_success"])
        self.assertTrue(result["data_integrity"]["passed"])
        self.assertTrue(result["numerical_validity"]["power_balance_passed"])
        self.assertFalse(result["numerical_validity"]["passed"])
        self.assertEqual(result["numerical_validity"]["status"], "adaptation_not_converged")

    def test_active_user_solver_prevents_creation(self):
        self.project.model3d.is_solver_running.return_value = True
        with self.assertRaisesRegex(RuntimeError, "existing user project is solving"):
            backend.ResearchSession(self.job, self.directory).prepare()
        self.de.new_mws.assert_not_called()
        self.project.model3d.abort_solver.assert_not_called()
        self.de.close.assert_not_called()

    def test_changed_source_is_rejected_before_opening_copy(self):
        source = self.directory / "source.cst"
        source.write_bytes(b"changed source")
        job = {"kind": "existing_project", "cst_pid": 123, "source_project": str(source),
               "source_project_sha256": "incorrect frozen hash"}
        with self.assertRaisesRegex(ValueError, "changed after review"):
            backend.ResearchSession(job, self.directory).prepare()
        self.de.open_project.assert_not_called()
        self.assertEqual(source.read_bytes(), b"changed source")


if __name__ == "__main__":
    unittest.main()
