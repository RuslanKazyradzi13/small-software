"""small sso (Google and Microsoft sign-in), small volume, small access log and small access signout.

Run: python -m unittest discover -s tests. Railway, Google and Microsoft are local mocks.
"""

import json
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, urlparse

from small_cli import i18n, mcp
from test_small import TOKEN, MockRailway, run, write

TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"
GOOGLE_ID = "123456-abc123.apps.googleusercontent.com"
MS_CLIENT = "11111111-2222-3333-4444-555555555555"
STATE = {"name": "demo", "projectId": "proj-1", "environmentId": "env-1", "serviceId": "svc-1",
         "domain": "demo.up.railway.app"}


class Railway(MockRailway):
    """MockRailway plus volumes and the environment log."""

    def __init__(self, volume=None, events=None, **kwargs):
        super().__init__(**kwargs)
        self.volume = volume    # {"mountPath": ...} of svc-1, or None
        self.events = events or []  # [(timestamp, message, {attribute: value})]

    def graphql(self, payload):
        field = re.search(r"\{\s*(\w+)", payload["query"]).group(1)
        variables = payload.get("variables") or {}
        if field not in ("environment", "volumeCreate", "environmentLogs"):
            return super().graphql(payload)
        self.calls.append(field)
        self.history.append(variables)
        self.variables[field] = variables
        if field == "environment":
            edges = [{"node": dict({"id": "vol-1", "serviceId": "svc-1", "currentSizeMB": 1.5, "sizeMB": 5000,
                                    "state": "READY"}, **self.volume)}] if self.volume else []
            edges.append({"node": {"id": "vol-x", "serviceId": "svc-other", "mountPath": "/other"}})
            return {"data": {"environment": {"volumeInstances": {"edges": edges}}}}
        if field == "volumeCreate":
            self.volume = {"mountPath": variables["input"]["mountPath"]}
            return {"data": {"volumeCreate": {"id": "vol-1", "name": "demo-volume"}}}
        rows = [{"timestamp": ts, "message": message, "severity": "info",
                 "attributes": [{"key": k, "value": json.dumps(v)} for k, v in attributes.items()]}
                for ts, message, attributes in self.events]
        return {"data": {"environmentLogs": rows[-(variables.get("n") or 100):]}}


class Provider:
    """Google / Microsoft mock: the token endpoint answers the credential check; Microsoft's OpenID configuration
    resolves a domain to a tenant."""

    def __init__(self, error="invalid_grant", status=400):
        self.requests, self.error, self.status = [], error, status
        box = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                box.requests.append(("GET", self.path, {}))
                if self.path == "/company.com/v2.0/.well-known/openid-configuration":
                    return self.reply(200, {"issuer": "https://login.microsoftonline.com/%s/v2.0" % TENANT})
                self.reply(400, {"error": "invalid_tenant"})

            def do_POST(self):
                form = dict(parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
                box.requests.append(("POST", self.path, form))
                self.reply(box.status, {"error": box.error, "error_description": "AADSTS7000215: Invalid client secret"
                                        if box.error == "invalid_client" else "Bad code"})

            def reply(self, status, payload):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = self.tmp.name
        self.config({"name": "demo", "env": {"ACCESS_TOKEN": "tok-owner", "ACCESS_EMAILS": "@company.com"}})
        write(self.app, ".small.json", json.dumps(STATE))
        write(self.app, "magic_link.py", "# reads ACCESS_EMAILS, SESSIONS_REVOKED, GOOGLE_CLIENT_ID (small 0.8)\n")

    def config(self, data=None):
        path = Path(self.app, "small.json")
        if data is not None:
            path.write_text(json.dumps(data), encoding="utf-8")
        return json.loads(path.read_text(encoding="utf-8"))

    def railway(self, **kwargs):
        server = Railway(service_vars={"ACCESS_TOKEN": "tok-owner", "ACCESS_EMAILS": "@company.com"}, **kwargs)
        self.addCleanup(server.close)
        return server

    def provider(self, **kwargs):
        provider = Provider(**kwargs)
        self.addCleanup(provider.close)
        return provider

    def cli(self, server, *args, env=None):
        base = {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": server.url, "GOOGLE_CLIENT_SECRET": "",
                "MICROSOFT_CLIENT_SECRET": "", "GOOGLE_CLIENT_ID": "", "MICROSOFT_CLIENT_ID": "",
                "MICROSOFT_TENANT_ID": ""}
        return run([args[0], "-C", self.app, *args[1:]], dict(base, **(env or {})))


class SsoTest(Base):
    def test_google(self):
        server, google = self.railway(), self.provider()
        code, out, err = self.cli(server, "sso", "google", "--client-id", GOOGLE_ID,
                                  env={"GOOGLE_CLIENT_SECRET": "GOCSPX-secret", "GOOGLE_TOKEN_URL": google.url + "/token"})
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "google")
        self.assertIn("https://demo.up.railway.app/_access/sso/callback", err)  # the redirect URI to register
        self.assertIn("console.cloud.google.com", err)
        self.assertIn("Google принял ID клиента и секрет", err)
        method, path, form = google.requests[0]
        self.assertEqual((method, path), ("POST", "/token"))
        self.assertEqual({k: form[k] for k in ("client_id", "client_secret", "redirect_uri", "grant_type")},
                         {"client_id": GOOGLE_ID, "client_secret": "GOCSPX-secret", "grant_type": "authorization_code",
                          "redirect_uri": "https://demo.up.railway.app/_access/sso/callback"})
        env = self.config()["env"]
        self.assertEqual((env["GOOGLE_CLIENT_ID"], env["GOOGLE_CLIENT_SECRET"]), (GOOGLE_ID, "GOCSPX-secret"))
        self.assertRegex(env["SESSION_SECRET"], r"^[A-Za-z0-9_-]{40,}$")
        upserted = server.variables["variableCollectionUpsert"]["input"]["variables"]
        self.assertEqual(upserted["PUBLIC_URL"], "https://demo.up.railway.app")
        self.assertNotIn("PUBLIC_URL", env)
        self.assertEqual(server.calls[-1], "serviceInstanceRedeploy")
        self.assertNotIn("GOCSPX-secret", out + err)
        self.assertIn("@company.com", err)  # who can sign in

    def test_wrong_credentials_change_nothing(self):
        server, google = self.railway(), self.provider(error="invalid_client", status=401)
        before = self.config()
        code, out, err = self.cli(server, "sso", "google", "--client-id", GOOGLE_ID,
                                  env={"GOOGLE_CLIENT_SECRET": "GOCSPX-wrong", "GOOGLE_TOKEN_URL": google.url + "/token"})
        self.assertEqual(code, 1)
        self.assertIn("invalid_client", err)
        self.assertNotIn("GOCSPX-wrong", out + err)
        self.assertEqual(self.config(), before)
        self.assertNotIn("variableCollectionUpsert", server.calls)
        code, _, err = self.cli(server, "sso", "google", "--client-id", "not-a-google-id",
                                env={"GOOGLE_CLIENT_SECRET": "x"})
        self.assertEqual(code, 1)
        self.assertIn("apps.googleusercontent.com", err)

    def test_unclear_answers_are_reported_but_not_fatal(self):
        server = self.railway()
        code, _, err = self.cli(server, "sso", "google", "--client-id", GOOGLE_ID,
                                env={"GOOGLE_CLIENT_SECRET": "GOCSPX-x", "GOOGLE_TOKEN_URL": "http://127.0.0.1:9/token"})
        self.assertEqual(code, 0, err)
        self.assertIn("Не удалось до конца проверить", err)

    def test_microsoft_with_a_domain_for_the_tenant(self):
        server, ms = self.railway(), self.provider()
        code, out, err = self.cli(server, "sso", "microsoft", "--client-id", MS_CLIENT.upper(), "--tenant", "Company.com",
                                  env={"MICROSOFT_CLIENT_SECRET": "ms~secret", "MICROSOFT_LOGIN_URL": ms.url})
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "microsoft")
        self.assertIn(TENANT, err)
        self.assertEqual([(m, p) for m, p, _ in ms.requests],
                         [("GET", "/company.com/v2.0/.well-known/openid-configuration"),
                          ("POST", "/%s/oauth2/v2.0/token" % TENANT)])
        self.assertEqual(ms.requests[1][2]["client_id"], MS_CLIENT)
        env = self.config()["env"]
        self.assertEqual((env["MICROSOFT_CLIENT_ID"], env["MICROSOFT_TENANT_ID"], env["MICROSOFT_CLIENT_SECRET"]),
                         (MS_CLIENT, TENANT, "ms~secret"))
        code, _, err = self.cli(server, "sso", "microsoft", "--client-id", MS_CLIENT, "--tenant", "unknown.example",
                                env={"MICROSOFT_CLIENT_SECRET": "x", "MICROSOFT_LOGIN_URL": ms.url})
        self.assertEqual(code, 1)
        self.assertIn("unknown.example", err)
        self.assertEqual(self.cli(server, "sso", "microsoft", "--client-id", "nope", "--tenant", TENANT,
                                  env={"MICROSOFT_CLIENT_SECRET": "x"})[0], 1)

    def test_status_and_off(self):
        server = self.railway()
        code, out, err = self.cli(server, "sso")
        self.assertEqual((code, out.strip()), (0, "off"))
        self.assertIn("small sso google", err)
        server.service_vars.update(GOOGLE_CLIENT_ID=GOOGLE_ID, GOOGLE_CLIENT_SECRET="s",
                                   MICROSOFT_CLIENT_ID=MS_CLIENT, MICROSOFT_CLIENT_SECRET="s", MICROSOFT_TENANT_ID=TENANT)
        code, out, err = self.cli(server, "sso")
        self.assertEqual(out.strip(), "google microsoft")
        self.assertIn(TENANT, err)
        self.assertIn("https://demo.up.railway.app/_access/sso/callback", err)
        code, _, err = self.cli(server, "sso", "off", "microsoft")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.cli(server, "sso")[1].strip(), "google")
        self.assertEqual(self.cli(server, "sso", "off")[0], 0)
        self.assertEqual(self.cli(server, "sso")[1].strip(), "off")

    def test_streamlit_gets_the_page_itself_as_redirect(self):
        write(self.app, "app.py", "import streamlit as st\n# require_access() ... gate.sso_finish(...)\n")
        server, google = self.railway(), self.provider()
        code, _, err = self.cli(server, "sso", "google", "--client-id", GOOGLE_ID,
                                env={"GOOGLE_CLIENT_SECRET": "x", "GOOGLE_TOKEN_URL": google.url + "/token"})
        self.assertEqual(code, 0, err)
        self.assertEqual(google.requests[0][2]["redirect_uri"], "https://demo.up.railway.app/")

    def test_custom_domain_and_undeployed_and_old_code(self):
        self.config({"name": "demo", "env": {"PUBLIC_URL": "https://reports.company.com/"}})
        server, google = self.railway(), self.provider()
        self.cli(server, "sso", "google", "--client-id", GOOGLE_ID,
                 env={"GOOGLE_CLIENT_SECRET": "x", "GOOGLE_TOKEN_URL": google.url + "/token"})
        self.assertEqual(google.requests[0][2]["redirect_uri"], "https://reports.company.com/_access/sso/callback")

        write(self.app, ".small.json", "{}")
        self.config({"name": "demo", "env": {}})
        code, _, err = self.cli(server, "sso", "google", "--client-id", GOOGLE_ID, env={"GOOGLE_CLIENT_SECRET": "x"})
        self.assertEqual(code, 1)
        self.assertIn("small deploy", err)

        write(self.app, "magic_link.py", "# old: only ACCESS_EMAILS\n")
        code, _, err = self.cli(server, "sso", "google", "--client-id", GOOGLE_ID, env={"GOOGLE_CLIENT_SECRET": "x"})
        self.assertEqual(code, 1)
        self.assertIn("0.8", err)
        self.assertIn(os.path.join("python-http", "magic_link.py"), err)

    def test_access_says_how_people_sign_in(self):
        server = self.railway()
        server.service_vars.update(GOOGLE_CLIENT_ID=GOOGLE_ID, GOOGLE_CLIENT_SECRET="s")
        code, out, err = self.cli(server, "access")
        self.assertEqual(code, 0, err)
        self.assertIn("Как входят: Google", err)
        self.assertNotIn("small mail resend", err)  # sign-in works without email
        code, _, err = self.cli(server, "access", "add", "olga@company.com")
        self.assertEqual(code, 0, err)
        self.assertIn("войти через Google", err)


class VolumeTest(Base):
    def test_add_and_status(self):
        server = self.railway()
        code, out, err = self.cli(server, "volume")
        self.assertEqual((code, out.strip()), (0, "off"))
        self.assertIn("small volume add", err)
        code, out, err = self.cli(server, "volume", "add")
        self.assertEqual((code, out.strip()), (0, "/data"), err)
        self.assertEqual(server.variables["volumeCreate"]["input"],
                         {"projectId": "proj-1", "environmentId": "env-1", "serviceId": "svc-1", "mountPath": "/data"})
        self.assertEqual(server.calls[-1], "serviceInstanceRedeploy")
        self.assertIn(".magic-link.db", err)
        code, out, err = self.cli(server, "volume")
        self.assertEqual(out.strip(), "/data")
        self.assertIn("1.5", err)
        server.calls.clear()
        code, out, err = self.cli(server, "volume", "add", "--mount", "/files")
        self.assertEqual((code, out.strip()), (0, "/data"))  # one volume per service: the existing one
        self.assertNotIn("volumeCreate", server.calls)

    def test_mount_paths(self):
        server = self.railway()
        for bad in ("/app", "/app/data", "data", "/", "/a b", "/data/../app"):
            code, _, err = self.cli(server, "volume", "add", "--mount", bad)
            self.assertEqual(code, 1, bad)
        self.assertNotIn("volumeCreate", server.calls)
        code, out, _ = self.cli(server, "volume", "add", "--mount", "/srv/files/", "--no-deploy")
        self.assertEqual((code, out.strip()), (0, "/srv/files"))
        self.assertNotIn("serviceInstanceRedeploy", server.calls)


EVENTS = [
    ("2026-10-10T09:00:00.123456789Z", "[magic-link] вход через Google: ivan@company.com, ip 203.0.113.7",
     {"level": "info", "magic_link": "sign_in", "method": "google", "user": "ivan@company.com", "ip": "203.0.113.7",
      "ua": "Firefox"}),
    ("2026-10-10T09:05:00Z", "[magic-link] нет доступа", {"level": "warn", "magic_link": "denied", "method": "microsoft",
                                                          "user": "stranger@evil.example", "ip": "198.51.100.1"}),
    ("2026-10-10T09:06:00Z", "[access-request] ...", {"magic_link": "request", "user": "bob@example.com", "ip": ""}),
    ("2026-10-10T09:07:00Z", "[magic-link] вход по ссылке", {"magic_link": "sign_in", "method": "link", "user": ""}),
    ("2026-10-10T09:08:00Z", "something else", {"magic_link": "unknown_event"}),
]


class AccessLogTest(Base):
    def test_table_and_json(self):
        server = self.railway(events=EVENTS)
        code, out, err = self.cli(server, "access", "log")
        self.assertEqual(code, 0, err)
        self.assertEqual(server.variables["environmentLogs"],
                         {"e": "env-1", "f": "@service:svc-1 @magic_link:*", "n": 50})
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 4)  # the unknown event is skipped
        self.assertRegex(lines[0], r"^2026-10-1\d \d\d:\d\d  вход\s+Google\s+ivan@company\.com\s+203\.0\.113\.7$")
        self.assertIn("отказ", lines[1])
        self.assertIn("Microsoft", lines[1])
        self.assertIn("запрос доступа", lines[2])
        self.assertIn("(общая ссылка)", lines[3])
        code, out, err = self.cli(server, "access", "log", "--json", "-n", "2")
        rows = [json.loads(line) for line in out.strip().splitlines()]
        self.assertEqual(server.variables["environmentLogs"]["n"], 2)
        self.assertEqual(rows[0]["method"], "link")
        self.assertEqual(set(rows[0]), {"time", "event", "method", "user", "ip", "ua"})

    def test_empty_and_limits(self):
        server = self.railway()
        code, out, err = self.cli(server, "access", "log")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("0.8", err)
        self.assertEqual(self.cli(server, "access", "log", "-n", "0")[0], 1)
        write(self.app, ".small.json", "{}")
        self.assertEqual(self.cli(server, "access", "log")[0], 1)  # not deployed

    def test_mcp_tool(self):
        server = self.railway(events=EVENTS)
        with mock.patch.dict(os.environ, {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": server.url}):
            result = mcp.call_tool("access_log", {"path": self.app, "lines": 10})
        self.assertFalse(result["isError"], result)
        rows = [json.loads(line) for line in result["content"][0]["text"].splitlines() if line.startswith("{")]
        self.assertEqual([r["user"] for r in rows], ["ivan@company.com", "stranger@evil.example", "bob@example.com", ""])
        self.assertTrue(mcp.call_tool("access_log", {"path": self.app, "lines": 0})["isError"])


class SignOutTest(Base):
    def revoked(self):
        return dict(item.split("=") for item in self.config()["env"]["SESSIONS_REVOKED"].split(", "))

    def test_an_address_a_domain_everyone(self):
        server = self.railway()
        before = int(time.time())
        code, _, err = self.cli(server, "access", "signout", "Ivan@Company.com")
        self.assertEqual(code, 0, err)
        self.assertIn("small access revoke ivan@company.com", err)  # access itself stays
        moment = int(self.revoked()["ivan@company.com"])
        self.assertLessEqual(before, moment)
        self.assertEqual(server.variables["variableCollectionUpsert"]["input"]["variables"]["SESSIONS_REVOKED"],
                         "ivan@company.com=%d" % moment)
        self.assertEqual(server.calls[-1], "serviceInstanceRedeploy")
        self.assertEqual(self.cli(server, "access", "signout", "@team.com")[0], 0)
        self.assertEqual(self.cli(server, "access", "signout", "--all", "--no-deploy")[0], 0)
        self.assertEqual(sorted(self.revoked()), ["*", "@team.com", "ivan@company.com"])
        self.assertEqual(self.config()["env"]["ACCESS_EMAILS"], "@company.com")  # nobody lost access

    def test_bad_arguments_and_old_code(self):
        server = self.railway()
        for args in ((), ("bob",), ("ivan@company.com", "--all")):
            self.assertEqual(self.cli(server, "access", "signout", *args)[0], 1, args)
        write(self.app, "magic_link.py", "# old\n")
        code, _, err = self.cli(server, "access", "signout", "ivan@company.com")
        self.assertEqual(code, 1)
        self.assertIn("0.8", err)
        self.assertNotIn("SESSIONS_REVOKED", self.config()["env"])

    def test_old_sign_outs_are_forgotten(self):
        old = int(time.time()) - 31 * 24 * 3600
        self.config({"name": "demo", "env": {"ACCESS_EMAILS": "@company.com",
                                             "SESSIONS_REVOKED": "old@company.com=%d, junk" % old}})
        server = self.railway()
        self.cli(server, "access", "signout", "new@company.com")
        self.assertEqual(sorted(self.revoked()), ["new@company.com"])

    def test_revoking_an_address_ends_its_sessions_for_good(self):
        self.config({"name": "demo", "env": {"ACCESS_EMAILS": "ivan@company.com, @team.com"}})
        server = self.railway()
        code, _, err = self.cli(server, "access", "revoke", "ivan@company.com")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(self.revoked()), ["ivan@company.com"])  # added back later, old sessions stay dead
        self.assertEqual(self.config()["env"]["ACCESS_EMAILS"], "@team.com")


def setUpModule():
    i18n.use("ru")  # the checks read small's Russian text; the machine's and CI's language don't matter


def tearDownModule():
    i18n.use(None)


if __name__ == "__main__":
    unittest.main()
