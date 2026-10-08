import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from autocst import research_backend as backend


class LumpedExportTests(unittest.TestCase):
    def export(self, monitor=True, include_voltage=True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        job = {"kind": "waveguide", "lumped_elements": [{"name": "load1", "resistance_ohm": 50,
               "point1_mm": [0, 0, 0], "point2_mm": [0, 1, 0], "monitor": monitor}]}
        session = backend.ResearchSession(job, root)
        paths = [r"1D Results\S-Parameters\S1,1", r"1D Results\S-Parameters\S2,1",
                 r"1D Results\Lumped Elements\Currents\load1 [1]",
                 r"1D Results\Lumped Elements\Voltages\other_load [1]"]
        if include_voltage:
            paths.append(r"1D Results\Lumped Elements\Voltages\load1 [1]")
        library = MagicMock()
        module = library.ProjectFile.return_value.get_3d.return_value
        module.get_tree_items.return_value = paths
        module.get_run_ids.return_value = [0]
        module.get_result_item.return_value = SimpleNamespace(get_xdata=lambda: [9, 10],
            get_ydata=lambda: [0.1, 0.2], xlabel="Frequency / GHz", ylabel="Normalized", title="fixture")
        with patch.object(backend, "_load_cst", return_value=(root, MagicMock(), library)):
            curves = session._export_curves()
        receipt = json.loads((root / "lumped_monitor_exports.json").read_text())
        return curves, receipt

    def test_discovers_exact_requested_monitor_paths_and_exports_labels(self):
        curves, receipt = self.export()
        self.assertTrue(receipt["voltage_current_exported"])
        self.assertEqual(len(receipt["curves"]), 2)
        self.assertFalse(any("other_load" in path for path in curves))
        self.assertTrue(all(item["xlabel"] == "Frequency / GHz" for item in receipt["curves"]))

    def test_missing_voltage_is_reported_without_accepting_another_elements_monitor(self):
        with self.assertRaisesRegex(RuntimeError, "monitors are absent"):
            self.export(include_voltage=False)

    def test_disabled_monitor_does_not_claim_voltage_current_export(self):
        curves, receipt = self.export(monitor=False, include_voltage=False)
        self.assertFalse(receipt["voltage_current_exported"])
        self.assertTrue(receipt["all_requested_monitors_satisfied"])
        self.assertEqual(len(curves), 2)
