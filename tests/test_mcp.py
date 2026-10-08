"""Exercise the real SDK registry/validation with a CST-free service double."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import mcp  # noqa: F401
except ImportError:
    HAS_MCP = False
else:
    HAS_MCP = True
    from autocst.mcp_server import create_server, main


class StubService:
    def __init__(self):
        self.calls = []

    def doctor(self):
        self.calls.append(("doctor",))
        return {"available": True, "solver_started": False}

    def search(self, query, limit=5):
        self.calls.append(("search", query, limit))
        return {"matches": [{"page": 12, "text": "waveguide"}]}

    def pages(self, start, end):
        self.calls.append(("pages", start, end))
        return {"pages": [{"page": start, "text": "official reference"}]}

    def submit(self, job):
        self.calls.append(("submit", job))
        return {"run_id": "test-run", "state": "queued"}

    def status(self, run_id):
        self.calls.append(("status", run_id))
        return {"run_id": run_id, "state": "queued"}

    def results(self, run_id):
        self.calls.append(("results", run_id))
        return {"run_id": run_id, "available": False}


def structured_result(result):
    """SDK 1.x exposes structured results directly or alongside text blocks."""
    if isinstance(result, dict):
        return result
    if isinstance(result, tuple) and len(result) == 2:
        return result[1]
    return json.loads(result[0].text)


@unittest.skipUnless(HAS_MCP, "Install requirements-mcp.txt to test the MCP bridge")
class MCPBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = StubService()
        self.server = create_server(Path.cwd(), service=self.service)

    async def test_legacy_tools_keep_correct_effect_annotations(self):
        tools = {tool.name: tool for tool in await self.server.list_tools()}
        legacy_names = {
                "cst_environment",
                "cst_search_manual",
                "cst_read_manual_pages",
                "cst_submit_waveguide",
                "cst_run_status",
                "cst_run_results",
            }
        self.assertTrue(legacy_names.issubset(tools))
        for name in legacy_names:
            tool = tools[name]
            self.assertFalse(tool.annotations.openWorldHint)
            self.assertFalse(tool.annotations.destructiveHint)
            if name == "cst_submit_waveguide":
                self.assertFalse(tool.annotations.readOnlyHint)
                self.assertFalse(tool.annotations.idempotentHint)
            else:
                self.assertTrue(tool.annotations.readOnlyHint)
                self.assertTrue(tool.annotations.idempotentHint)
        properties = tools["cst_submit_waveguide"].inputSchema["properties"]
        self.assertEqual(
            set(properties),
            {"a_mm", "b_mm", "length_mm", "fmin_ghz", "fmax_ghz", "timeout_seconds", "solve", "cst_pid"},
        )
        self.assertEqual(properties["a_mm"]["default"], 22.86)
        self.assertEqual(properties["timeout_seconds"]["default"], 300)
        self.assertEqual(properties["solve"]["type"], "boolean")
        self.assertEqual(properties["solve"]["default"], True)
        self.assertIsNone(properties["cst_pid"]["default"])
        self.assertEqual(
            {option["type"] for option in properties["cst_pid"]["anyOf"]},
            {"integer", "null"},
        )
        self.assertEqual(self.service.calls, [])

    async def test_submit_maps_default_job_and_returns_background_run(self):
        result = structured_result(await self.server.call_tool("cst_submit_waveguide", {}))
        self.assertEqual(result, {"run_id": "test-run", "state": "queued"})
        self.assertEqual(
            self.service.calls,
            [
                (
                    "submit",
                    {
                        "kind": "waveguide",
                        "parameters": {
                            "a_mm": 22.86,
                            "b_mm": 10.16,
                            "length_mm": 40.0,
                            "fmin_ghz": 8.2,
                            "fmax_ghz": 12.4,
                        },
                        "timeout_seconds": 300,
                        "solve": True,
                        "cst_pid": None,
                    },
                )
            ],
        )

    async def test_build_only_request_preserves_parameters(self):
        await self.server.call_tool(
            "cst_submit_waveguide",
            {"a_mm": 30, "b_mm": 12, "length_mm": 60, "timeout_seconds": 120, "solve": False},
        )
        job = self.service.calls[0][1]
        self.assertEqual(job["parameters"]["a_mm"], 30)
        self.assertEqual(job["parameters"]["b_mm"], 12)
        self.assertEqual(job["parameters"]["length_mm"], 60)
        self.assertEqual(job["timeout_seconds"], 120)
        self.assertIs(job["solve"], False)

    async def test_explicit_cst_pid_is_forwarded_at_job_top_level(self):
        await self.server.call_tool(
            "cst_submit_waveguide", {"cst_pid": 36684, "solve": False}
        )
        job = self.service.calls[0][1]
        self.assertEqual(job["cst_pid"], 36684)
        self.assertNotIn("cst_pid", job["parameters"])
        self.assertIs(job["solve"], False)

    async def test_invalid_inputs_never_reach_service(self):
        cases = [
            ("cst_submit_waveguide", {"a_mm": -1}),
            ("cst_submit_waveguide", {"b_mm": float("nan")}),
            ("cst_submit_waveguide", {"length_mm": float("inf")}),
            ("cst_submit_waveguide", {"fmin_ghz": 15}),
            ("cst_submit_waveguide", {"timeout_seconds": 0}),
            ("cst_submit_waveguide", {"a_mm": "invalid number"}),
            ("cst_submit_waveguide", {"cst_pid": 0}),
            ("cst_submit_waveguide", {"cst_pid": -1}),
            ("cst_submit_waveguide", {"cst_pid": "any"}),
            ("cst_search_manual", {"query": " "}),
            ("cst_search_manual", {"query": "Port", "limit": 0}),
            ("cst_read_manual_pages", {"start": 0, "end": 1}),
            ("cst_read_manual_pages", {"start": 2, "end": 1}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                with self.assertRaises(Exception):
                    await self.server.call_tool(name, arguments)
        self.assertEqual(self.service.calls, [])

    async def test_read_tools_forward_requests_without_submission(self):
        cases = [
            ("cst_environment", {}),
            ("cst_search_manual", {"query": "Waveguide Port", "limit": 3}),
            ("cst_read_manual_pages", {"start": 12, "end": 13}),
            ("cst_run_status", {"run_id": "test-run"}),
            ("cst_run_results", {"run_id": "test-run"}),
        ]
        for name, arguments in cases:
            result = structured_result(await self.server.call_tool(name, arguments))
            self.assertIsInstance(result, dict)
        self.assertEqual(
            self.service.calls,
            [
                ("doctor",),
                ("search", "Waveguide Port", 3),
                ("pages", 12, 13),
                ("status", "test-run"),
                ("results", "test-run"),
            ],
        )

    def test_http_binding_and_origin_checks_remain_local(self):
        with patch.dict("os.environ", {"FASTMCP_HOST": "0.0.0.0"}):
            server = create_server(Path.cwd(), service=self.service, port=9876)
        self.assertEqual(server.settings.host, "127.0.0.1")
        self.assertEqual(server.settings.port, 9876)
        security = server.settings.transport_security
        self.assertTrue(security.enable_dns_rebinding_protection)
        self.assertEqual(security.allowed_hosts, ["127.0.0.1:9876", "localhost:9876"])
        self.assertEqual(
            security.allowed_origins,
            ["http://127.0.0.1:9876", "http://localhost:9876"],
        )

    def test_cli_defaults_to_stdio_and_accepts_loopback_http(self):
        for arguments, expected in [([], "stdio"), (["--transport", "streamable-http"], "streamable-http")]:
            with self.subTest(expected=expected):
                with patch("autocst.mcp_server.create_server") as factory:
                    self.assertEqual(main(arguments), 0)
                    factory.return_value.run.assert_called_once_with(transport=expected)
        with patch("autocst.mcp_server.create_server") as factory:
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(["--host", "0.0.0.0"])
                with self.assertRaises(SystemExit):
                    main(["--port", "0"])
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
