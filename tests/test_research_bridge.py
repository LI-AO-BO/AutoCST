"""Research MCP/CLI contracts with service doubles; never invoke a CST worker."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from autocst.__main__ import main as cli_main

try:
    import mcp  # noqa: F401
except ImportError:
    HAS_MCP = False
else:
    HAS_MCP = True
    from autocst.mcp_server import create_server


LEGACY_TOOLS = {
    "cst_environment", "cst_search_manual", "cst_read_manual_pages",
    "cst_submit_waveguide", "cst_run_status", "cst_run_results",
}
RESEARCH_TOOLS = {
    "cst_create_experiment", "cst_revise_experiment", "cst_prepare_simulation", "cst_submit_prepared",
    "cst_experiment_context", "cst_research_run_status", "cst_research_run_results",
    "cst_completion_events", "cst_acknowledge_event", "cst_control_experiment",
    "cst_runner_status", "cst_start_runner",
    "cst_start_optimization", "cst_optimization_status", "cst_control_optimization",
}


class StubResearchService:
    def __init__(self):
        self.calls = []

    def _record(self, name, *args):
        self.calls.append((name, *args))
        return {"operation": name, "arguments": list(args)}

    def environment(self):
        return self._record("environment")

    def create_experiment(self, spec):
        return self._record("create_experiment", spec)

    def revise_experiment(self, experiment_id, spec):
        return self._record("revise_experiment", experiment_id, spec)

    def prepare_job(self, experiment_id, job, decision):
        return self._record("prepare_job", experiment_id, job, decision)

    def submit_job(self, prepared_id, idempotency_key):
        return self._record("submit_job", prepared_id, idempotency_key)

    def experiment_context(self, experiment_id):
        return self._record("experiment_context", experiment_id)

    def run_status(self, run_id):
        return self._record("run_status", run_id)

    def run_results(self, run_id):
        return self._record("run_results", run_id)

    def events(self, experiment_id=None, after=0):
        return self._record("events", experiment_id, after)

    def acknowledge(self, event_id):
        return self._record("acknowledge", event_id)

    def control(self, experiment_id, action):
        return self._record("control", experiment_id, action)

    def start_optimization(self, experiment_id, job_template, *, max_new_runs=4, config=None, idempotency_key):
        return self._record("start_optimization", experiment_id, job_template, max_new_runs, config, idempotency_key)

    def optimization_status(self, batch_id):
        return self._record("optimization_status", batch_id)

    def control_optimization(self, batch_id, action):
        return self._record("control_optimization", batch_id, action)

    def runner_status(self):
        return self._record("runner_status")

    def start_runner(self):
        return self._record("start_runner")


def structured_result(result):
    if isinstance(result, dict):
        return result
    if isinstance(result, tuple) and len(result) == 2:
        return result[1]
    return json.loads(result[0].text)


@unittest.skipUnless(HAS_MCP, "Install requirements-mcp.txt to test the MCP bridge")
class ResearchMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.research = StubResearchService()
        self.legacy = Mock()
        self.server = create_server(
            Path.cwd(), service=self.legacy, research_service=self.research
        )

    async def test_registry_preserves_legacy_and_exposes_bounded_research_operations(self):
        tools = {tool.name: tool for tool in await self.server.list_tools()}
        self.assertEqual(set(tools), LEGACY_TOOLS | RESEARCH_TOOLS)
        reads = {
            "cst_experiment_context", "cst_research_run_status", "cst_research_run_results",
            "cst_completion_events", "cst_runner_status",
            "cst_optimization_status",
        }
        for name in RESEARCH_TOOLS:
            annotations = tools[name].annotations
            self.assertEqual(annotations.readOnlyHint, name in reads)
            self.assertFalse(annotations.openWorldHint)
            self.assertEqual(annotations.destructiveHint, name == "cst_control_experiment")
        self.assertTrue(tools["cst_submit_prepared"].annotations.idempotentHint)
        self.assertTrue(tools["cst_acknowledge_event"].annotations.idempotentHint)
        self.assertTrue(tools["cst_start_optimization"].annotations.idempotentHint)
        self.assertFalse(tools["cst_prepare_simulation"].annotations.idempotentHint)
        self.assertEqual(
            set(tools["cst_submit_prepared"].inputSchema["required"]),
            {"prepared_id", "idempotency_key"},
        )
        self.assertEqual(
            tools["cst_control_experiment"].inputSchema["properties"]["action"]["enum"],
            ["pause", "resume", "cancel"],
        )
        self.assertEqual(
            set(tools["cst_start_optimization"].inputSchema["required"]),
            {"experiment_id", "job_template", "idempotency_key"},
        )
        self.assertEqual(
            tools["cst_control_optimization"].inputSchema["properties"]["action"]["enum"],
            ["pause", "resume", "stop"],
        )
        self.assertEqual(self.research.calls, [])
        self.assertEqual(self.legacy.mock_calls, [])

    async def test_create_prepare_and_submit_are_distinct_operations(self):
        spec = {"objective": "Check TE10 phase", "budgets": {"max_runs": 3}}
        job = {"kind": "waveguide", "parameters": {"length_mm": 50}}
        decision = {"reason": "Test length sensitivity", "evidence": ["prior-run"]}
        await self.server.call_tool("cst_create_experiment", {"spec": spec})
        prepared = structured_result(await self.server.call_tool(
            "cst_prepare_simulation",
            {"experiment_id": "exp-1", "job": job, "decision": decision},
        ))
        self.assertEqual(prepared["operation"], "prepare_job")
        self.assertEqual(self.research.calls, [
            ("create_experiment", spec), ("prepare_job", "exp-1", job, decision),
        ])
        await self.server.call_tool(
            "cst_submit_prepared", {"prepared_id": "prepared-1", "idempotency_key": "step-1"}
        )
        self.assertEqual(self.research.calls[-1], ("submit_job", "prepared-1", "step-1"))
        self.assertEqual(self.legacy.mock_calls, [])

    async def test_optional_decision_defaults_to_an_empty_record(self):
        await self.server.call_tool(
            "cst_prepare_simulation", {"experiment_id": "exp-1", "job": {"kind": "waveguide"}}
        )
        self.assertEqual(self.research.calls, [
            ("prepare_job", "exp-1", {"kind": "waveguide"}, {}),
        ])

    async def test_experiment_revision_is_explicit_and_does_not_submit(self):
        spec = {"objective": "Authorized revised objective", "budgets": {"max_runs": 5}}
        await self.server.call_tool(
            "cst_revise_experiment", {"experiment_id": "exp-1", "spec": spec}
        )
        self.assertEqual(self.research.calls, [("revise_experiment", "exp-1", spec)])

    async def test_events_and_results_do_not_acknowledge_or_submit_implicitly(self):
        for tool, arguments in [
            ("cst_experiment_context", {"experiment_id": "exp-1"}),
            ("cst_research_run_status", {"run_id": "run-1"}),
            ("cst_research_run_results", {"run_id": "run-1"}),
            ("cst_completion_events", {"experiment_id": "exp-1", "after": 12}),
            ("cst_completion_events", {}),
            ("cst_runner_status", {}),
        ]:
            result = structured_result(await self.server.call_tool(tool, arguments))
            self.assertIn("operation", result)
        self.assertEqual(self.research.calls, [
            ("experiment_context", "exp-1"), ("run_status", "run-1"),
            ("run_results", "run-1"), ("events", "exp-1", 12),
            ("events", None, 0), ("runner_status",),
        ])

    async def test_acknowledgement_and_control_are_explicit(self):
        await self.server.call_tool("cst_acknowledge_event", {"event_id": 13})
        await self.server.call_tool("cst_start_runner", {})
        for action in ("pause", "resume", "cancel"):
            await self.server.call_tool(
                "cst_control_experiment", {"experiment_id": "exp-1", "action": action}
            )
        self.assertEqual(self.research.calls, [
            ("acknowledge", 13), ("start_runner",), ("control", "exp-1", "pause"),
            ("control", "exp-1", "resume"), ("control", "exp-1", "cancel"),
        ])

    async def test_optimization_start_is_short_and_preserves_the_retry_key_and_configuration(self):
        job = {"kind": "metasurface", "cst_pid": 1234, "parameters": {"patch_width_mm": 9.223}}
        arguments = {"experiment_id": "exp-1", "job_template": job, "idempotency_key": "batch-1"}
        result = structured_result(await self.server.call_tool("cst_start_optimization", arguments))
        self.assertEqual(result["operation"], "start_optimization")
        self.assertEqual(self.research.calls, [("start_optimization", "exp-1", job, 4, None, "batch-1")])
        await self.server.call_tool("cst_start_optimization", {
            **arguments, "idempotency_key": "batch-2", "max_new_runs": 3, "config": {"seed": 17},
        })
        self.assertEqual(self.research.calls[-1], ("start_optimization", "exp-1", job, 3, {"seed": 17}, "batch-2"))
        self.assertEqual(self.legacy.mock_calls, [])

    async def test_optimization_status_and_controls_do_not_submit_individual_jobs(self):
        await self.server.call_tool("cst_optimization_status", {"batch_id": "batch-1"})
        for action in ("pause", "resume", "stop"):
            await self.server.call_tool("cst_control_optimization", {"batch_id": "batch-1", "action": action})
        self.assertEqual(self.research.calls, [
            ("optimization_status", "batch-1"), ("control_optimization", "batch-1", "pause"),
            ("control_optimization", "batch-1", "resume"), ("control_optimization", "batch-1", "stop"),
        ])
        self.assertEqual(self.legacy.mock_calls, [])

    async def test_invalid_bridge_parameters_do_not_reach_service(self):
        for tool, arguments in [
            ("cst_submit_prepared", {"prepared_id": "prepared-1", "idempotency_key": " "}),
            ("cst_submit_prepared", {"prepared_id": "prepared-1"}),
            ("cst_completion_events", {"after": -1}),
            ("cst_acknowledge_event", {"event_id": 0}),
            ("cst_control_experiment", {"experiment_id": "exp-1", "action": "kill-all"}),
            ("cst_prepare_simulation", {"experiment_id": "exp-1", "job": [1, 2]}),
            ("cst_start_optimization", {"experiment_id": "exp-1", "job_template": {}, "idempotency_key": " "}),
            ("cst_start_optimization", {"experiment_id": "exp-1", "job_template": {}}),
            ("cst_start_optimization", {"experiment_id": "exp-1", "job_template": {}, "idempotency_key": "b", "max_new_runs": 0}),
            ("cst_start_optimization", {"experiment_id": "exp-1", "job_template": {}, "idempotency_key": "b", "config": []}),
            ("cst_control_optimization", {"batch_id": "batch-1", "action": "cancel"}),
        ]:
            with self.subTest(tool=tool, arguments=arguments):
                with self.assertRaises(Exception):
                    await self.server.call_tool(tool, arguments)
        self.assertEqual(self.research.calls, [])


class ResearchCLITests(unittest.TestCase):
    def setUp(self):
        self.research = StubResearchService()

    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("autocst.__main__._research_service", return_value=self.research):
            with patch("autocst.__main__.Service") as legacy:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = cli_main(arguments)
                legacy.assert_not_called()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_json_spec_job_and_decision_are_routed_without_execution(self):
        spec = {"objective": "相位误差", "budgets": {"max_runs": 3}}
        job = {"kind": "waveguide", "parameters": {"length_mm": 50}}
        decision = {"reason": "比较传播相位"}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, value in (("spec", spec), ("job", job), ("decision", decision)):
                (root / f"{name}.json").write_text(json.dumps(value), encoding="utf-8-sig")
            for arguments in [
                ["research", "create-experiment", str(root / "spec.json")],
                ["research", "revise-experiment", "exp-1", str(root / "spec.json")],
                ["research", "prepare", "exp-1", str(root / "job.json"), "--decision", str(root / "decision.json")],
            ]:
                code, stdout, stderr = self.invoke(arguments)
                self.assertEqual(code, 0, stderr)
                self.assertIn("operation", json.loads(stdout))
                self.assertEqual(stderr, "")
        self.assertEqual(self.research.calls, [
            ("create_experiment", spec), ("revise_experiment", "exp-1", spec),
            ("prepare_job", "exp-1", job, decision),
        ])

    def test_research_cli_routes_state_events_and_explicit_actions(self):
        cases = [
            (["environment"], ("environment",)),
            (["submit-prepared", "prepared-1", "--idempotency-key", "step-1"], ("submit_job", "prepared-1", "step-1")),
            (["context", "exp-1"], ("experiment_context", "exp-1")),
            (["status", "run-1"], ("run_status", "run-1")),
            (["results", "run-1"], ("run_results", "run-1")),
            (["events", "--experiment-id", "exp-1", "--after", "12"], ("events", "exp-1", 12)),
            (["events"], ("events", None, 0)),
            (["ack", "13"], ("acknowledge", 13)),
            (["control", "exp-1", "pause"], ("control", "exp-1", "pause")),
            (["control", "exp-1", "cancel"], ("control", "exp-1", "cancel")),
            (["runner-status"], ("runner_status",)),
            (["start-runner"], ("start_runner",)),
            (["optimization-status", "batch-1"], ("optimization_status", "batch-1")),
            (["control-optimization", "batch-1", "pause"], ("control_optimization", "batch-1", "pause")),
            (["control-optimization", "batch-1", "resume"], ("control_optimization", "batch-1", "resume")),
            (["control-optimization", "batch-1", "stop"], ("control_optimization", "batch-1", "stop")),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                code, stdout, stderr = self.invoke(["research", *args])
                self.assertEqual(code, 0, stderr)
                self.assertEqual(json.loads(stdout)["operation"], expected[0])
                self.assertEqual(self.research.calls[-1], expected)

    def test_optimization_cli_routes_the_job_seed_and_bounded_round_count(self):
        job = {"kind": "metasurface", "parameters": {"patch_width_mm": 9.223}}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "job.json"
            path.write_text(json.dumps(job), encoding="utf-8-sig")
            for options, rounds, seed, key in [([], 4, 0, "batch-1"),
                                             (["--max-new-runs", "2", "--seed", "17"], 2, 17, "batch-2")]:
                code, stdout, stderr = self.invoke([
                    "research", "start-optimization", "exp-1", str(path), "--idempotency-key", key, *options,
                ])
                self.assertEqual(code, 0, stderr)
                self.assertEqual(json.loads(stdout)["operation"], "start_optimization")
                self.assertEqual(self.research.calls[-1],
                                 ("start_optimization", "exp-1", job, rounds, {"seed": seed}, key))

    def test_bad_input_returns_json_error_and_does_not_submit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "spec.json"
            path.write_text("[]", encoding="utf-8")
            cases = [
                ["research", "create-experiment", str(path)],
                ["research", "events", "--after", "-1"],
                ["research", "submit-prepared", "prepared-1", "--idempotency-key", " "],
                ["research", "start-optimization", "exp-1", str(path), "--idempotency-key", " "],
                ["research", "start-optimization", "exp-1", str(path), "--idempotency-key", "b", "--max-new-runs", "0"],
                ["research", "start-optimization", "exp-1", str(path), "--idempotency-key", "b"],
            ]
            for args in cases:
                with self.subTest(args=args):
                    code, stdout, stderr = self.invoke(args)
                    self.assertEqual(code, 1)
                    self.assertEqual(stdout, "")
                    self.assertIn("ValueError", json.loads(stderr)["error"])
        self.assertEqual(self.research.calls, [])

    def test_legacy_cli_remains_available_without_research_initialization(self):
        with patch("autocst.__main__._research_service") as research:
            with patch("autocst.__main__.Service") as legacy:
                legacy.return_value.doctor.return_value = {"legacy": True}
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = cli_main(["doctor"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout.getvalue()), {"legacy": True})
                legacy.return_value.doctor.assert_called_once_with()
            research.assert_not_called()


if __name__ == "__main__":
    unittest.main()
