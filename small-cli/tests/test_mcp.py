"""small mcp: the MCP server for AI agents, the protocol and the tools against a Railway mock.

Run: python -m unittest discover -s tests
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from small_cli import i18n, mcp

from test_small import TOKEN, MockRailway

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class McpBase(unittest.TestCase):
    def setUp(self):
        i18n.use("en")
        self.addCleanup(i18n.use, "ru")
        self.out = io.BytesIO()
        self.server = mcp.Server(self.out)
        self.next_id = 0

    def request(self, method, params=None, **meta):
        """Sends a request; returns (the response, the notifications that arrived before it)."""
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.out.seek(0)
        self.out.truncate()
        self.server.handle_line(json.dumps(message).encode("utf-8") + b"\n")
        lines = [json.loads(line) for line in self.out.getvalue().decode("utf-8").splitlines()]
        self.assertEqual(lines[-1].get("id"), self.next_id, lines)
        return lines[-1], lines[:-1]

    def call(self, tool, progress_token=None, **arguments):
        params = {"name": tool, "arguments": arguments}
        if progress_token is not None:
            params["_meta"] = {"progressToken": progress_token}
        response, notes = self.request("tools/call", params)
        result = response["result"]
        return result["content"][0]["text"], result["isError"], notes


class ProtocolTest(McpBase):
    def test_initialize(self):
        response, _ = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                  "clientInfo": {"name": "test", "version": "1"}})
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "small")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("share_app", result["instructions"])
        response, _ = self.request("initialize", {"protocolVersion": "1999-01-01"})  # unknown: we reply with ours
        self.assertEqual(response["result"]["protocolVersion"], mcp.PROTOCOL_VERSIONS[0])

    def test_tools_list(self):
        tools = self.request("tools/list")[0]["result"]["tools"]
        names = [t["name"] for t in tools]
        self.assertEqual(names, ["init_app", "deploy_app", "share_app", "revoke_access", "list_access",
                                 "access_log", "list_apps", "app_logs", "set_env"])
        self.assertFalse([n for n in names if "destroy" in n or "delete" in n])  # deleting is terminal-only
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertTrue(set(tool["inputSchema"]["required"]) <= set(tool["inputSchema"]["properties"]))
            self.assertIsNone(i18n_cyrillic(json.dumps(tool, ensure_ascii=False)))  # in English for agents

    def test_errors_and_notifications(self):
        self.assertEqual(self.request("ping")[0]["result"], {})
        self.assertEqual(self.request("no/such")[0]["error"]["code"], -32601)
        self.assertEqual(self.request("tools/call", {"name": "destroy_app"})[0]["error"]["code"], -32602)
        for raw, code in ((b"{not json\n", -32700), (b'{"jsonrpc": "2.0", "id": 7}\n', -32600)):
            self.out.seek(0)
            self.out.truncate()
            self.server.handle_line(raw)
            self.assertEqual(json.loads(self.out.getvalue())["error"]["code"], code)
        for raw in (b'{"jsonrpc": "2.0", "method": "notifications/initialized"}\n',  # a notification gets no response
                    b'{"jsonrpc": "2.0", "id": 9, "result": {}}\n'):  # nor does a stray response
            self.out.seek(0)
            self.out.truncate()
            self.server.handle_line(raw)
            self.assertEqual(self.out.getvalue(), b"")

    def test_bad_arguments_are_tool_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = [("deploy_app", {}, "path is required"),
                     ("deploy_app", {"path": os.path.join(tmp, "missing")}, "folder not found"),
                     ("init_app", {"path": tmp, "template": "rails"}, "template must be one of"),
                     ("app_logs", {"path": tmp, "lines": 0}, "lines must be a number"),
                     ("set_env", {"path": tmp}, "nothing to change"),
                     ("share_app", {"path": tmp}, "who is required")]
            for tool, arguments, message in cases:
                text, is_error, _ = self.call(tool, **arguments)
                self.assertTrue(is_error, tool)
                self.assertIn(message, text)


class ToolsTest(McpBase):
    """Agent scenario: create → deploy → share → look → revoke."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = os.path.join(self.tmp.name, "team-budget")
        self.railway = MockRailway(live={"svc-1": "SUCCESS"},
                                   logs={"deploymentLogs": [("2026-10-11T09:00:00Z", "Listening on 0.0.0.0:8080")]})
        self.addCleanup(self.railway.close)
        env = mock.patch.dict(os.environ, {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": self.railway.url})
        env.start()
        self.addCleanup(env.stop)
        sleep = mock.patch("time.sleep")  # polling the deploy status without pauses
        sleep.start()
        self.addCleanup(sleep.stop)

    def config(self):
        return json.loads(Path(self.app, "small.json").read_text(encoding="utf-8"))

    def test_agent_flow(self):
        text, is_error, _ = self.call("init_app", path=self.app)
        self.assertFalse(is_error, text)
        self.assertTrue(Path(self.app, "app.py").is_file())

        text, is_error, notes = self.call("deploy_app", progress_token="dep", path=self.app)
        self.assertFalse(is_error, text)
        url = "https://%s.up.railway.app" % self.config()["name"]
        self.assertTrue(text.startswith(url), text)  # the URL on the first line, the log below
        progress = [n["params"] for n in notes if n["method"] == "notifications/progress"]
        self.assertTrue(progress and all(p["progressToken"] == "dep" for p in progress))
        self.assertEqual([p["progress"] for p in progress], list(range(1, len(progress) + 1)))
        self.assertTrue(any("SUCCESS" in p["message"] for p in progress))

        text, is_error, _ = self.call("share_app", path=self.app, who="ivan@company.com")
        self.assertFalse(is_error, text)
        self.assertIn("ivan@company.com", self.config()["env"]["ACCESS_EMAILS"])
        self.assertIn("ivan@company.com", self.railway.service_vars["ACCESS_EMAILS"])
        self.assertIn("small mail", text)  # mail isn't set up: the agent learns what to tell the user

        text, is_error, _ = self.call("share_app", path=self.app, who="bob")
        self.assertFalse(is_error, text)
        self.assertIn(url + "/?token=", text)

        text, is_error, _ = self.call("list_access", path=self.app)
        self.assertFalse(is_error, text)
        self.assertIn("ivan@company.com", text)
        self.assertIn("bob", text)

        text, is_error, _ = self.call("revoke_access", path=self.app, who="bob")
        self.assertFalse(is_error, text)
        self.assertNotIn("bob:", self.config()["env"]["ACCESS_TOKEN"])

        text, is_error, _ = self.call("set_env", path=self.app, set={"DEBUG": "1", "GREETING": "hello world"})
        self.assertFalse(is_error, text)
        self.assertEqual(self.config()["env"]["GREETING"], "hello world")
        text, is_error, _ = self.call("set_env", path=self.app, unset=["DEBUG"], set={"MODE": "fast"})
        self.assertFalse(is_error, text)
        self.assertNotIn("DEBUG", self.config()["env"])
        self.assertEqual(self.config()["env"]["MODE"], "fast")

        text, is_error, _ = self.call("list_apps", root=self.tmp.name)
        self.assertFalse(is_error, text)
        apps = json.loads(text)
        self.assertEqual([(a["name"], a["path"], a["url"]) for a in apps],
                         [(self.config()["name"], str(Path(self.app).resolve()), url)])

        text, is_error, _ = self.call("app_logs", path=self.app, lines=20)
        self.assertFalse(is_error, text)
        self.assertIn("Listening on 0.0.0.0:8080", text)

    def test_deploy_without_token_explains_what_to_do(self):
        self.call("init_app", path=self.app)
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("RAILWAY_API_TOKEN", "RAILWAY_TOKEN"):
                os.environ.pop(name, None)
            text, is_error, _ = self.call("deploy_app", path=self.app)
        self.assertTrue(is_error)
        self.assertIn("RAILWAY_API_TOKEN", text)

    def test_tool_output_follows_small_language(self):
        i18n.use("ru")
        text, is_error, _ = self.call("deploy_app", path=os.path.join(self.tmp.name, "missing"))
        self.assertTrue(is_error)
        self.assertIn("папка не найдена", text)


class StdioTest(unittest.TestCase):
    """A real `small mcp` process: stdout carries only protocol messages, one per line."""

    def test_stdio_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PYTHONPATH=str(PACKAGE_ROOT), SMALL_LANG="en")
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "init_app", "arguments": {"path": os.path.join(tmp, "демо app")}}},
            ]
            stdin = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode("utf-8")
            started = time.monotonic()
            proc = subprocess.run([sys.executable, "-m", "small_cli", "mcp"], input=stdin, capture_output=True,
                                  env=env, cwd=tmp, timeout=60)
            self.assertLess(time.monotonic() - started, 60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = proc.stdout.decode("utf-8").splitlines()
            replies = [json.loads(line) for line in lines]  # fails on any stray line
            self.assertEqual([r["id"] for r in replies], [1, 2])
            self.assertFalse(replies[1]["result"]["isError"], replies[1])
            self.assertTrue(Path(tmp, "демо app", "small.json").is_file())


def i18n_cyrillic(text):
    import re

    return re.search("[Ѐ-ӿ]", text)


if __name__ == "__main__":
    unittest.main()
