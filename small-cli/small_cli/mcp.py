"""small's MCP server: an AI agent (Claude Code, Cursor and others) creates, deploys and shares apps on its own.

The protocol is JSON-RPC 2.0 over stdin/stdout, one message per line (https://modelcontextprotocol.io). Tools
call the same commands as the terminal: the command's output is collected and returned to the agent, progress lines
go out as notifications/progress. Deleting apps (small destroy) is deliberately absent here: terminal only.

stdout belongs to the protocol: commands write to captured streams, and messages go straight to the byte stdout.
"""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from . import __version__
from .common import CliError
from .config import ConfigError
from .i18n import tr
from .railway import RailwayError, token_from_env

# Newest protocol versions first. If the client asks for one we know, we answer with it, otherwise with our newest:
# small uses only tools/list and tools/call, and they are the same in all versions.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "small deploys small team apps to Railway and keeps them private: only people you share an app with can open "
    "it. Typical flow: init_app (or use an existing app folder) -> deploy_app -> share_app with a work email. "
    "Always pass absolute folder paths. Prefer sharing by email: the person signs in with Google or Microsoft (if "
    "the user set it up with `small sso`) or with a one-time link sent to that address, so nothing secret passes "
    "through the chat. A name instead of an email creates a personal link that works like a password. Deleting apps "
    "and entering provider secrets are not available here; the user runs `small destroy` and `small sso` in a "
    "terminal."
)

PATH = {"type": "string", "description": "Absolute path to the app folder."}
WHO = {"type": "string", "description": "A work email (ivan@company.com), a whole domain (@company.com), "
                                        "or a short name (bob) for a personal invite link."}

TOOLS = [
    {
        "name": "init_app",
        "title": "Create an app",
        "description": "Create a ready-to-deploy app in a folder, already protected by invite-only sign-in. "
                       "Templates: python-http (default, Python standard library), fastapi, node-http, streamlit. "
                       "Then write the app's code into the created files and call deploy_app.",
        "inputSchema": {"type": "object", "properties": {
            "path": PATH,
            "template": {"type": "string", "enum": ["python-http", "fastapi", "node-http", "streamlit"],
                         "description": "App template (default: python-http)."},
            "name": {"type": "string", "description": "Project name in Railway (default: from the folder name)."},
        }, "required": ["path"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "deploy_app",
        "title": "Deploy an app",
        "description": "Deploy a folder to Railway and return its HTTPS URL. Works for a folder from init_app or any "
                       "existing app (app.py, main.py, requirements.txt, package.json, Dockerfile...). Usually takes "
                       "1-3 minutes. The app stays private: only people added with share_app can open it.",
        "inputSchema": {"type": "object", "properties": {
            "path": PATH,
            "wait": {"type": "boolean", "description": "Wait for the build to finish (default: true). "
                                                       "With false, check later with list_apps or app_logs."},
            "new": {"type": "boolean", "description": "Create a new Railway project even if the folder is "
                                                      "already linked to one (default: false)."},
        }, "required": ["path"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False,
                        "openWorldHint": True},
    },
    {
        "name": "share_app",
        "title": "Share an app",
        "description": "Give someone access to a deployed app. With an email or @domain the person opens the app "
                       "and signs in with Google or Microsoft (set up once with `small sso` in a terminal) or with "
                       "a one-time link emailed to them (set up once with `small mail`); the result warns when "
                       "neither is set up. With a name, returns a personal invite link that works like a password: "
                       "show it to the user only and suggest sending it privately. The app restarts to apply the "
                       "change.",
        "inputSchema": {"type": "object", "properties": {"path": PATH, "who": WHO}, "required": ["path", "who"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": True},
    },
    {
        "name": "revoke_access",
        "title": "Revoke access",
        "description": "Take access away from an email, a domain or a personal-link name. Their sessions and links "
                       "stop working after the app restarts; everyone else keeps access.",
        "inputSchema": {"type": "object", "properties": {"path": PATH, "who": WHO}, "required": ["path", "who"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True,
                        "openWorldHint": True},
    },
    {
        "name": "list_access",
        "title": "Who has access",
        "description": "List the emails, domains and personal-link names that can open an app.",
        "inputSchema": {"type": "object", "properties": {"path": PATH}, "required": ["path"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "access_log",
        "title": "Who signed in",
        "description": "The app's sign-in log across deploys, oldest first, one JSON object per line: time, event "
                       "(sign_in, denied, sign_out_all, request), method (email, link, google, microsoft), user, "
                       "ip. Railway keeps logs for a limited time depending on the plan.",
        "inputSchema": {"type": "object", "properties": {
            "path": PATH,
            "lines": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "How many entries (default 50)."},
        }, "required": ["path"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "list_apps",
        "title": "List apps",
        "description": "Find small apps under a folder and report each app's URL and latest deploy status.",
        "inputSchema": {"type": "object", "properties": {
            "root": {"type": "string", "description": "Absolute path of the folder to search (with subfolders)."},
        }, "required": ["root"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "app_logs",
        "title": "App logs",
        "description": "Show the latest deploy's log: the running app's log (default) or the build log. Use it when "
                       "a deploy fails or the app misbehaves.",
        "inputSchema": {"type": "object", "properties": {
            "path": PATH,
            "build": {"type": "boolean", "description": "Show the build log instead of the app log."},
            "lines": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "How many lines (default 100)."},
        }, "required": ["path"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "set_env",
        "title": "Set environment variables",
        "description": "Set or remove the app's environment variables in Railway and small.json, then restart it. "
                       "small.json is git-ignored, so secrets such as API keys belong here, not in the code.",
        "inputSchema": {"type": "object", "properties": {
            "path": PATH,
            "set": {"type": "object", "additionalProperties": {"type": "string"},
                    "description": "Variables to set, e.g. {\"DEBUG\": \"1\"}."},
            "unset": {"type": "array", "items": {"type": "string"}, "description": "Variable names to remove."},
        }, "required": ["path"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True,
                        "openWorldHint": True},
    },
]


class ToolError(Exception):
    """An error in a tool's arguments: the agent will see it and can fix the call."""


def serve(stdin=None, stdout=None):
    """Reads requests until the end of stdin. Returns the exit code."""
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    server = Server(stdout)
    for raw in iter(stdin.readline, b""):
        if raw.strip():
            server.handle_line(raw)
    return 0


class Server:
    def __init__(self, stdout):
        self.stdout = stdout

    def send(self, message):
        self.stdout.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.stdout.flush()

    def handle_line(self, raw):
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self.send(_error(None, -32700, "Parse error"))
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            if isinstance(message, dict) and ("result" in message or "error" in message):
                return None  # a client's response to our request; we never send any
            return self.send(_error(message.get("id") if isinstance(message, dict) else None, -32600,
                                    "Invalid Request"))
        request_id, method = message.get("id"), message["method"]
        params = message.get("params") or {}
        if request_id is None:
            return None  # a notification: notifications/initialized, notifications/cancelled; no response expected
        try:
            result = self.dispatch(method, params, request_id)
        except _RpcError as e:
            return self.send(_error(request_id, e.code, str(e)))
        except Exception as e:  # noqa: BLE001 — the server must not crash because of one request
            return self.send(_error(request_id, -32603, "Internal error: %s" % type(e).__name__))
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def dispatch(self, method, params, request_id):
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "small", "title": "small", "version": __version__},
                    "instructions": INSTRUCTIONS}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name = params.get("name")
            if name not in HANDLERS:
                raise _RpcError(-32602, "Unknown tool: %s" % name)
            token = (params.get("_meta") or {}).get("progressToken")
            progress = self.progress_sender(token) if token is not None else None
            return call_tool(name, params.get("arguments") or {}, progress)
        raise _RpcError(-32601, "Method not found: %s" % method)

    def progress_sender(self, token):
        count = [0]

        def send(line):
            count[0] += 1
            self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                       "params": {"progressToken": token, "progress": count[0], "message": line}})
        return send


class _RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def call_tool(name, arguments, progress=None):
    """The tools/call result: text for the agent; isError means the command failed or the arguments are wrong."""
    try:
        if not isinstance(arguments, dict):
            raise ToolError(tr("arguments must be an object", "аргументы должны быть объектом"))
        code, text = HANDLERS[name](arguments, progress)
    except ToolError as e:
        code, text = 1, tr("Error: %s", "Ошибка: %s") % e
    return {"content": [{"type": "text", "text": text.strip() or "OK"}], "isError": code != 0}


# --- tools ---

def _init(args, progress):
    argv = ["init", _path(args, must_exist=False), "--template", _choice(args, "template", "python-http")]
    if args.get("name"):
        argv += ["--name", _string(args, "name")]
    return run_cli(argv, progress)


def _deploy(args, progress):
    argv = ["deploy", _path(args)]
    if args.get("wait") is False:
        argv.append("--no-wait")
    if args.get("new") is True:
        argv.append("--new")
    return run_cli(argv, progress)


def _share(args, progress):
    return run_cli(["access", "add", "-C", _path(args), _string(args, "who")], progress)


def _revoke(args, progress):
    return run_cli(["access", "revoke", "-C", _path(args), _string(args, "who")], progress)


def _list_access(args, progress):
    return run_cli(["access", "list", "-C", _path(args)], progress)


def _logs(args, progress):
    lines = _lines(args, 100)
    return run_cli(["logs", _path(args), "-n", str(lines)] + (["--build"] if args.get("build") else []), progress)


def _access_log(args, progress):
    return run_cli(["access", "log", "-C", _path(args), "--json", "-n", str(_lines(args, 50))], progress)


def _lines(args, default):
    lines = args.get("lines", default)
    if not isinstance(lines, int) or isinstance(lines, bool) or not 1 <= lines <= 1000:
        raise ToolError(tr("lines must be a number from 1 to 1000", "lines — число от 1 до 1000"))
    return lines


def _set_env(args, progress):
    path = _path(args)
    values, names = args.get("set") or {}, args.get("unset") or []
    if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
        raise ToolError(tr("set must map names to string values", "set — имена и строковые значения"))
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ToolError(tr("unset must be a list of names", "unset — список имён"))
    if not values and not names:
        raise ToolError(tr("nothing to change: pass set or unset", "нечего менять: передайте set или unset"))
    text = []
    if names:  # remove first without a restart: set will restart, and if there is no set, the call below does
        code, out = run_cli(["env", "unset", "-C", path] + (["--no-deploy"] if values else []) + names, progress)
        text.append(out)
        if code:
            return code, "\n".join(text)
    if values:
        code, out = run_cli(["env", "set", "-C", path] + ["%s=%s" % kv for kv in values.items()], progress)
        text.append(out)
    return code, "\n".join(text)


def _list_apps(args, progress):
    from .dashboard import add_live_status, scan_apps

    root = _path(args, key="root")
    apps = scan_apps(root)
    add_live_status(apps, token_from_env())
    rows = [{"name": app["name"], "path": app["path"], "url": app["url"],
             "deployStatus": (app.get("deployment") or {}).get("status") or app["status"],
             "errors": app["errors"]} for app in apps]
    return 0, json.dumps(rows, ensure_ascii=False, indent=2) if rows else tr(
        "No small apps under %s.", "Под %s нет приложений small.") % root


HANDLERS = {"init_app": _init, "deploy_app": _deploy, "share_app": _share, "revoke_access": _revoke,
            "list_access": _list_access, "access_log": _access_log, "list_apps": _list_apps, "app_logs": _logs,
            "set_env": _set_env}


def run_cli(argv, progress=None):
    """A small command run as in the terminal: (code, stdout + log). ask() never prompts: stdin is not a terminal."""
    from .cli import build_parser

    out, err = io.StringIO(), _Lines(progress)
    with redirect_stdout(out), redirect_stderr(err):
        try:
            args = build_parser().parse_args(argv)
            code = args.func(args) or 0
        except SystemExit as e:  # argparse rejected the arguments and has already said why
            code = e.code if isinstance(e.code, int) else 2
        except (CliError, ConfigError, RailwayError) as e:
            print(tr("Error: %s", "Ошибка: %s") % e, file=sys.stderr)
            code = 1
    result, log = out.getvalue().strip(), err.getvalue().strip()
    return code, "\n\n".join(part for part in (result, log) if part)


class _Lines(io.StringIO):
    """Captured stderr: accumulates text and passes each complete line to progress."""

    def __init__(self, progress):
        super().__init__()
        self.progress, self.pending = progress, ""

    def write(self, text):
        if self.progress:
            self.pending += text
            *lines, self.pending = self.pending.split("\n")
            for line in lines:
                if line.strip():
                    self.progress(line.strip())
        return super().write(text)


def _path(args, key="path", must_exist=True):
    value = _string(args, key)
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = Path.cwd() / path
    if must_exist and not path.is_dir():
        raise ToolError(tr("folder not found: %s", "папка не найдена: %s") % path)
    return str(path)


def _string(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(tr("%s is required", "нужен параметр %s") % key)
    return value.strip()


def _choice(args, key, default):
    value = args.get(key, default)
    allowed = next(t for t in TOOLS if t["name"] == "init_app")["inputSchema"]["properties"][key]["enum"]
    if value not in allowed:
        raise ToolError(tr("%s must be one of: %s", "%s — одно из: %s") % (key, ", ".join(allowed)))
    return value
