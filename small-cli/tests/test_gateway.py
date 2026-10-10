"""small gateway: the gateway's sign-in server (templates/gateway/gateway.py) and small gateway on / off.

Run: python -m unittest discover -s tests. Railway is a local mock that keeps variables and domains per service.
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlencode
from wsgiref.util import setup_testing_defaults

from small_cli import __version__, i18n
from small_cli.scaffold import TEMPLATES_DIR
from test_small import TOKEN, MockRailway, run, write

GATEWAY_DIR = TEMPLATES_DIR / "gateway"
STATE = {"name": "demo", "projectId": "proj-1", "environmentId": "env-1", "serviceId": "svc-1",
         "domain": "demo.up.railway.app"}


def load_gateway():
    """templates/gateway/gateway.py, importing magic_link from next to it."""
    sys.path.insert(0, str(GATEWAY_DIR))
    try:
        spec = importlib.util.spec_from_file_location("small_gateway", GATEWAY_DIR / "gateway.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(GATEWAY_DIR))
    return module


def forward_auth(app, uri, cookie=None, method="GET", headers=None):
    """What Caddy's forward_auth sends: GET /_access/check with the original method and URI in X-Forwarded-*."""
    environ = {"PATH_INFO": "/_access/check", "QUERY_STRING": uri.partition("?")[2], "REQUEST_METHOD": "GET",
               "HTTP_X_FORWARDED_METHOD": method, "HTTP_X_FORWARDED_URI": uri, "HTTP_X_FORWARDED_PROTO": "https",
               "HTTP_X_FORWARDED_FOR": "203.0.113.7", "wsgi.input": io.BytesIO(b"")}
    if cookie:
        environ["HTTP_COOKIE"] = cookie
    environ.update(headers or {})
    return call(app, environ)


def call(app, environ):
    setup_testing_defaults(environ)
    captured = {}

    def start_response(status, headers):
        captured.update(status=status, headers=headers)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        body = b"".join(app(environ, start_response)).decode("utf-8")
    return captured["status"], captured["headers"], body, out.getvalue()


class GatewayServerTest(unittest.TestCase):
    """The sign-in server behind Caddy: forward_auth answers and the sign-in pages."""

    KEY = "gateway-key"

    def setUp(self):
        env = mock.patch.dict(os.environ, {
            "ACCESS_TOKEN": "bob:tok-b", "ACCESS_EMAILS": "ivan@company.com", "SESSION_SECRET": "s",
            "LOGIN_LINKS_TO_LOG": "1", "RAILWAY_PUBLIC_DOMAIN": "demo.up.railway.app", "PUBLIC_URL": "",
            "SMALL_GATEWAY_KEY": self.KEY, "ACCESS_DB": "", "RAILWAY_VOLUME_MOUNT_PATH": "", "SESSIONS_REVOKED": "",
            "GOOGLE_CLIENT_ID": "", "MICROSOFT_CLIENT_ID": "", "OWNER_LANG": "en"})
        env.start()
        self.addCleanup(env.stop)
        self.module = load_gateway()
        with contextlib.redirect_stdout(io.StringIO()):
            self.app = self.module.make_app(self.KEY)
        self.magic_link = sys.modules["magic_link"]

    def test_without_a_session_the_visitor_gets_the_sign_in_page(self):
        status, headers, body, _ = forward_auth(self.app, "/reports?page=2")
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("Sign in with your work email", body.replace("Вход по рабочей почте", "Sign in with your work email"))
        self.assertNotIn("X-Small-Auth", dict(headers))

    def test_an_invite_link_sets_the_cookie_and_strips_the_token(self):
        status, headers, _, log = forward_auth(self.app, "/reports?token=tok-b&page=2")
        headers = dict(headers)
        self.assertEqual((status, headers["Location"]), ("302 Found", "/reports?page=2"))
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn('"magic_link": "sign_in"', log)
        cookie = headers["Set-Cookie"].split(";")[0]
        status, headers, _, _ = forward_auth(self.app, "/reports?page=2", cookie=cookie, method="POST")
        headers = dict(headers)
        self.assertEqual(status, "200 OK")
        self.assertEqual((headers["X-Small-User"], headers["X-Small-Method"]), ("bob", "link"))
        self.assertEqual(self.magic_link.gateway_identity(self.KEY, "bob", "link", headers["X-Small-Auth"]),
                         ("bob", "link"))

    def test_an_email_session_is_signed_for_the_app(self):
        gate = self.app.app.gate
        session = gate.new_session("ivan@company.com", "google")
        status, headers, _, _ = forward_auth(self.app, "/", cookie="small_access=" + session)
        headers = dict(headers)
        self.assertEqual(status, "200 OK")
        self.assertEqual((headers["X-Small-User"], headers["X-Small-Method"]), ("ivan@company.com", "google"))
        self.assertIsNone(self.magic_link.gateway_identity("another-key", "ivan@company.com", "google",
                                                           headers["X-Small-Auth"]))
        # The app behind it, with the same key, lets the request in; nothing else gets in.
        with contextlib.redirect_stdout(io.StringIO()):
            behind = self.magic_link.MagicLink(lambda e, s: (s("200 OK", []), [e["magic_link.user"].encode()])[1],
                                               gateway_key=self.KEY)
        environ = {"PATH_INFO": "/", "REQUEST_METHOD": "GET", "wsgi.input": io.BytesIO(b""),
                   **{"HTTP_" + k.upper().replace("-", "_"): v for k, v in headers.items() if k.startswith("X-Small")}}
        self.assertEqual(call(behind, environ)[2], "ivan@company.com")
        self.assertEqual(call(behind, {"PATH_INFO": "/", "wsgi.input": io.BytesIO(b"")})[0], "403 Forbidden")

    def test_the_shared_link_has_no_name(self):
        with mock.patch.dict(os.environ, {"ACCESS_TOKEN": "tok-shared"}), contextlib.redirect_stdout(io.StringIO()):
            app = self.module.make_app(self.KEY)
        cookie = forward_auth(app, "/?token=tok-shared")[1]
        cookie = dict(cookie)["Set-Cookie"].split(";")[0]
        headers = dict(forward_auth(app, "/", cookie=cookie)[1])
        self.assertNotIn("X-Small-User", headers)
        self.assertEqual(self.magic_link.gateway_identity(self.KEY, "", "link", headers["X-Small-Auth"]), ("", "link"))

    def test_sign_in_pages_are_served_directly_not_through_the_check(self):
        self.assertEqual(forward_auth(self.app, "/_access/login")[0], "404 Not Found")
        self.assertEqual(forward_auth(self.app, "/%5F%61ccess/login")[0], "404 Not Found")  # encoded the same path
        environ = {"PATH_INFO": "/_access/login", "REQUEST_METHOD": "POST", "CONTENT_LENGTH": "25",
                   "wsgi.input": io.BytesIO(b"email=ivan%40company.com"), "HTTP_X_FORWARDED_FOR": "198.51.100.1"}
        status, _, body, log = call(self.app, environ)
        self.assertEqual(status, "200 OK")
        self.assertIn("Check your email", body.replace("Проверьте почту", "Check your email"))

    def test_main_needs_its_settings(self):
        with mock.patch.dict(os.environ, {"SMALL_GATEWAY_KEY": "", "UPSTREAM": ""}):
            with self.assertRaises(SystemExit) as caught:
                self.module.main()
        self.assertIn("SMALL_GATEWAY_KEY", str(caught.exception))
        # A reference Railway filled in before the app had a private address: never proxy to "http://:8080".
        with mock.patch.dict(os.environ, {"UPSTREAM": "http://:8080"}):
            with self.assertRaises(SystemExit) as caught:
                self.module.main()
        self.assertIn("UPSTREAM has no host", str(caught.exception))


class GatewayFilesTest(unittest.TestCase):
    def test_dockerfile_and_caddyfile(self):
        dockerfile = (GATEWAY_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^FROM caddy:\d+\.\d+\.\d+-alpine$")  # an exact version, updated on purpose
        self.assertIn('CMD ["python3", "-u", "/srv/gateway.py"]', dockerfile)
        caddyfile = (GATEWAY_DIR / "Caddyfile").read_text(encoding="utf-8")
        route = caddyfile[caddyfile.index("route {"):]
        order = [route.index(part) for part in ("@loop header X-Small-Gateway", "request_header -X-Small-User",
                                                "request_header -X-Small-Method", "request_header -X-Small-Auth",
                                                "respond /_access/check 404", "reverse_proxy /_access/* 127.0.0.1:9000",
                                                "forward_auth 127.0.0.1:9000", "reverse_proxy {$UPSTREAM}")]
        self.assertEqual(order, sorted(order))  # in route, directives run in the order written
        self.assertIn("copy_headers X-Small-User X-Small-Method X-Small-Auth", caddyfile)
        self.assertIn("header_up X-Small-Gateway 1", caddyfile)  # what the loop check looks for
        self.assertIn("admin off", caddyfile)


class GatewayRailway(MockRailway):
    """MockRailway with variables, domains and deploys per service, as the gateway needs."""

    def __init__(self, vars_by_service=None, **kwargs):
        super().__init__(**kwargs)
        self.vars = {sid: dict(v) for sid, v in (vars_by_service or {"svc-1": {}}).items()}
        self.services = {"svc-1": "demo"}
        self.domains = {"dom-1": {"service": "svc-1", "domain": "demo.up.railway.app"}}
        self.redeploys, self.deleted, self.domain_free = [], [], True
        self.gateway_status = "SUCCESS"
        self.project = {"name": "demo", "services": []}

    def graphql(self, payload):
        field = re.search(r"\{\s*(\w+)", payload["query"]).group(1)
        v = payload.get("variables") or {}
        handled = ("variables", "variableCollectionUpsert", "variableDelete", "serviceCreate", "domains",
                   "serviceDomainCreate", "serviceDomainDelete", "serviceDomainUpdate", "serviceDomainAvailable",
                   "serviceInstanceRedeploy", "serviceDelete", "project", "deployment", "serviceInstance")
        if field not in handled:
            return super().graphql(payload)
        self.calls.append(field)
        self.history.append(v)
        self.variables[field] = v
        if field == "variables":
            found = dict(self.vars.get(v["serviceId"], {}))
            if not v.get("unrendered") and v["serviceId"] in self.services:  # Railway's own, as it renders them
                found["RAILWAY_PRIVATE_DOMAIN"] = self.services[v["serviceId"]] + ".railway.internal"
            return {"data": {"variables": found}}
        if field == "variableCollectionUpsert":
            self.vars.setdefault(v["input"]["serviceId"], {}).update(v["input"]["variables"])
            return {"data": {field: True}}
        if field == "variableDelete":
            self.vars.get(v["input"]["serviceId"], {}).pop(v["input"]["name"], None)
            return {"data": {field: True}}
        if field == "serviceCreate":
            sid = "svc-gw" if v["input"]["name"].endswith("-gateway") else "svc-1"
            sid = sid if sid not in self.services else "svc-%d" % len(self.services)
            self.services[sid] = v["input"]["name"]
            return {"data": {"serviceCreate": {"id": sid}}}
        if field == "domains":
            mine = [{"id": i, "domain": d["domain"]} for i, d in self.domains.items() if d["service"] == v["s"]]
            return {"data": {"domains": {"serviceDomains": mine, "customDomains": self.custom_domains}}}
        if field == "serviceDomainCreate":
            i = "dom-%d" % (len(self.domains) + 10)
            self.domains[i] = {"service": v["input"]["serviceId"], "domain": "%s-production-x1.up.railway.app" % i}
            return {"data": {"serviceDomainCreate": {"id": i, "domain": self.domains[i]["domain"]}}}
        if field == "serviceDomainDelete":
            self.domains.pop(v["id"], None)
            return {"data": {field: True}}
        if field == "serviceDomainUpdate":
            self.domains[v["input"]["serviceDomainId"]]["domain"] = v["input"]["domain"]
            return {"data": {field: True}}
        if field == "serviceDomainAvailable":
            taken = any(d["domain"] == v["domain"] for d in self.domains.values())
            return {"data": {field: {"available": self.domain_free and not taken}}}
        if field == "serviceInstanceRedeploy":
            self.redeploys.append(v["s"])
            return {"data": {field: True}}
        if field == "serviceDelete":
            self.deleted.append(v["id"])
            self.services.pop(v["id"], None)
            return {"data": {field: True}}
        if field == "project":
            edges = [{"node": {"id": i, "name": n}} for i, n in self.services.items()]
            return {"data": {"project": {"name": "demo", "services": {"edges": edges}}}}
        if field == "serviceInstance":
            latest = {"id": "dep-gw", "status": "SUCCESS", "createdAt": "2026-10-11T00:00:00Z"}
            return {"data": {"serviceInstance": {"latestDeployment": latest, "sleepApplication": False}}}
        return {"data": {"deployment": {"status": self.gateway_status}}}

    def domain_of(self, service):
        return sorted(d["domain"] for d in self.domains.values() if d["service"] == service)


class GatewayCommandTest(unittest.TestCase):
    APP_VARS = {"ACCESS_TOKEN": "tok-owner", "ACCESS_EMAILS": "@company.com", "SESSION_SECRET": "secret-1",
                "RESEND_API_KEY": "re_key", "DATABASE_URL": "postgres://db", "GOOGLE_CLIENT_ID": "g",
                "GOOGLE_CLIENT_SECRET": "gs", "OWNER_LANG": "ru"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = self.tmp.name
        write(self.app, "small.json", json.dumps({"name": "demo", "env": {"ACCESS_TOKEN": "tok-owner"}}))
        write(self.app, ".small.json", json.dumps(STATE))
        write(self.app, "main.go", "package main\n")  # any app: no magic_link

    def railway(self, **kwargs):
        server = GatewayRailway(vars_by_service={"svc-1": dict(self.APP_VARS)}, **kwargs)
        self.addCleanup(server.close)
        return server

    def cli(self, server, *args):
        return run([args[0], "-C", self.app, *args[1:]], {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": server.url})

    def state(self):
        return json.loads(Path(self.app, ".small.json").read_text(encoding="utf-8"))

    def on(self, server, *extra):
        code, out, err = self.cli(server, "gateway", "on", "--interval", "0", *extra)
        self.assertEqual(code, 0, err)
        return out, err

    def test_on(self):
        server = self.railway()
        out, err = self.on(server)
        self.assertEqual(out.strip(), "https://demo.up.railway.app")
        self.assertEqual(server.services["svc-gw"], "demo-gateway")
        gw, app = server.vars["svc-gw"], server.vars["svc-1"]
        # The gateway: sign-in variables, where to send requests, its key and the address.
        self.assertEqual(gw["UPSTREAM"], "http://${{demo.RAILWAY_PRIVATE_DOMAIN}}:8080")
        self.assertRegex(gw["SMALL_GATEWAY_KEY"], r"^[A-Za-z0-9_-]{40,}$")
        for name in ("ACCESS_TOKEN", "ACCESS_EMAILS", "SESSION_SECRET", "RESEND_API_KEY", "GOOGLE_CLIENT_SECRET",
                     "OWNER_LANG"):
            self.assertEqual(gw[name], self.APP_VARS[name], name)
        self.assertEqual((gw["PUBLIC_URL"], gw["PORT"]), ("https://demo.up.railway.app", "8080"))
        self.assertNotIn("DATABASE_URL", gw)  # the app's own variables stay the app's
        # The app: the key as a reference, a known port, no sign-in variables; shared ones stay.
        self.assertEqual(app["SMALL_GATEWAY_KEY"], "${{demo-gateway.SMALL_GATEWAY_KEY}}")
        self.assertEqual(app["PORT"], "8080")
        for name in ("ACCESS_TOKEN", "ACCESS_EMAILS", "SESSION_SECRET", "GOOGLE_CLIENT_ID"):
            self.assertNotIn(name, app)
        self.assertEqual((app["DATABASE_URL"], app["RESEND_API_KEY"]), ("postgres://db", "re_key"))
        # The address moved to the gateway with its name; the app has none.
        self.assertEqual((server.domain_of("svc-gw"), server.domain_of("svc-1")), (["demo.up.railway.app"], []))
        self.assertEqual(server.uploads[-1], ["./Caddyfile", "./Dockerfile", "./gateway.py", "./magic_link.py"])
        self.assertEqual(server.redeploys, ["svc-1"])
        self.assertEqual(self.state()["gateway"], {"serviceId": "svc-gw", "name": "demo-gateway", "version": __version__})
        self.assertIn("X-Small-User", err)
        # Already on: nothing new.
        code, out, _ = self.cli(server, "gateway", "on")
        self.assertEqual((code, out.strip(), len(server.uploads)), (0, "on", 1))
        code, out, err = self.cli(server, "gateway")
        self.assertEqual(out.strip(), "on")
        self.assertIn("demo-gateway", err)

    def test_port_and_a_new_name_when_the_old_one_is_held(self):
        server = self.railway()
        server.vars["svc-1"]["PORT"] = "3000"
        self.on(server)
        self.assertEqual(server.vars["svc-gw"]["UPSTREAM"], "http://${{demo.RAILWAY_PRIVATE_DOMAIN}}:3000")
        server2 = self.railway()
        Path(self.app, ".small.json").write_text(json.dumps(STATE), encoding="utf-8")
        server2.domain_free = False
        with mock.patch("small_cli.commands.gateway.time.sleep"):
            out, err = self.on(server2, "--port", "4000")
        self.assertNotIn("PORT", server2.vars["svc-1"])  # --port: the app listens there by itself
        self.assertEqual(server2.vars["svc-gw"]["UPSTREAM"], "http://${{demo.RAILWAY_PRIVATE_DOMAIN}}:4000")
        new = self.state()["domain"]
        self.assertNotEqual(new, "demo.up.railway.app")
        self.assertIn(new, err)
        self.assertEqual(server2.vars["svc-gw"]["PUBLIC_URL"], "https://" + new)

    def test_refusals(self):
        server = self.railway()
        server.custom_domains.append({"id": "cd-1", "domain": "app.company.com", "targetPort": None, "status": {}})
        code, _, err = self.cli(server, "gateway", "on")
        self.assertEqual(code, 1)
        self.assertIn("small domain remove", err)
        server.custom_domains.clear()
        write(self.app, "magic_link.py", "# 0.8: knows ACCESS_EMAILS and SESSIONS_REVOKED only\n")
        code, _, err = self.cli(server, "gateway", "on")
        self.assertEqual(code, 1)
        self.assertIn("0.9", err)
        self.assertNotIn("serviceCreate", server.calls)
        write(self.app, "magic_link.py", "# reads SMALL_GATEWAY_KEY\n")
        self.on(server)

    def test_a_gateway_that_does_not_start_is_removed(self):
        server = self.railway()
        server.gateway_status = "FAILED"
        code, _, err = self.cli(server, "gateway", "on", "--interval", "0")
        self.assertEqual(code, 1)
        self.assertIn("приложение не изменилось", err)
        self.assertEqual(server.deleted, ["svc-gw"])
        self.assertNotIn("gateway", self.state())
        self.assertEqual(server.domain_of("svc-1"), ["demo.up.railway.app"])
        self.assertEqual(server.vars["svc-1"]["ACCESS_EMAILS"], "@company.com")

    def test_variables_go_to_their_service(self):
        server = self.railway()
        self.on(server)
        server.redeploys.clear()
        write(self.app, "magic_link.py", "# any version: behind the gateway it doesn't matter\n")
        code, _, err = self.cli(server, "access", "add", "olga@team.com")
        self.assertEqual(code, 0, err)
        self.assertEqual(server.vars["svc-gw"]["ACCESS_EMAILS"], "@company.com, olga@team.com")
        self.assertNotIn("ACCESS_EMAILS", server.vars["svc-1"])
        self.assertEqual(server.redeploys, ["svc-gw"])  # only the gateway restarts
        server.redeploys.clear()
        self.assertEqual(self.cli(server, "env", "set", "FEATURE=1", "MAIL_FROM=Reports <r@company.com>")[0], 0)
        self.assertEqual(server.vars["svc-1"]["FEATURE"], "1")
        self.assertNotIn("FEATURE", server.vars["svc-gw"])
        self.assertEqual((server.vars["svc-1"]["MAIL_FROM"], server.vars["svc-gw"]["MAIL_FROM"]),
                         ("Reports <r@company.com>",) * 2)
        self.assertEqual(sorted(server.redeploys), ["svc-1", "svc-gw"])
        code, out, err = self.cli(server, "access")
        self.assertIn("olga@team.com", out)  # read from the gateway
        self.assertEqual(self.cli(server, "access", "signout", "--all")[0], 0)  # the gateway knows how, whatever the app
        self.assertIn("SESSIONS_REVOKED", server.vars["svc-gw"])

    def test_access_log_reads_the_gateway(self):
        server = self.railway()
        self.on(server)
        with mock.patch("small_cli.railway.Railway.environment_logs", return_value=[]) as logs:
            self.assertEqual(self.cli(server, "access", "log")[0], 0)
        self.assertEqual(logs.call_args[0][1], "@service:svc-gw @magic_link:*")

    def test_deploy_splits_variables_and_updates_an_old_gateway(self):
        server = self.railway()
        self.on(server)
        state = self.state()
        state["gateway"]["version"] = "0.0.1"
        Path(self.app, ".small.json").write_text(json.dumps(state), encoding="utf-8")
        write(self.app, "small.json", json.dumps({"name": "demo", "env": {"ACCESS_EMAILS": "@company.com",
                                                                          "FEATURE": "on"}}))
        code, _, err = run(["deploy", self.app, "--no-wait"], {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": server.url})
        self.assertEqual(code, 0, err)
        self.assertEqual(server.vars["svc-1"]["FEATURE"], "on")
        self.assertEqual(server.vars["svc-gw"]["ACCESS_EMAILS"], "@company.com")
        self.assertNotIn("ACCESS_EMAILS", server.vars["svc-1"])
        self.assertEqual(server.vars["svc-gw"]["PUBLIC_URL"], "https://demo.up.railway.app")
        self.assertEqual(server.uploads[-1], ["./Caddyfile", "./Dockerfile", "./gateway.py", "./magic_link.py"])  # rebuilt
        self.assertEqual(self.state()["gateway"]["version"], __version__)
        uploads = len(server.uploads)
        run(["deploy", self.app, "--no-wait"], {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": server.url})
        self.assertEqual(len(server.uploads), uploads + 1)  # up to date: only the app this time

    def test_off(self):
        server = self.railway()
        self.on(server)
        code, _, err = self.cli(server, "gateway", "off")
        self.assertEqual(code, 1)  # no sign-in of its own: anyone would get in
        self.assertIn("--yes", err)
        code, out, err = self.cli(server, "gateway", "off", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "https://demo.up.railway.app")
        self.assertEqual((server.domain_of("svc-1"), server.deleted), (["demo.up.railway.app"], ["svc-gw"]))
        self.assertEqual(server.vars["svc-1"]["ACCESS_EMAILS"], "@company.com")
        self.assertNotIn("SMALL_GATEWAY_KEY", server.vars["svc-1"])
        self.assertNotIn("gateway", self.state())
        self.assertEqual(self.cli(server, "gateway")[1].strip(), "off")

    def test_off_without_asking_when_the_app_has_its_own_sign_in(self):
        server = self.railway()
        write(self.app, "magic_link.py", "# reads SMALL_GATEWAY_KEY\n")
        self.on(server)
        self.assertEqual(self.cli(server, "gateway", "off")[0], 0)

    def test_other_commands_follow_the_gateway(self):
        server = self.railway()
        self.on(server)
        server.redeploys.clear()
        self.assertEqual(self.cli(server, "sleep", "on")[0], 0)
        self.assertEqual(sorted(server.redeploys), ["svc-1", "svc-gw"])
        self.assertEqual(self.cli(server, "domain", "add", "app.company.com")[0], 0)
        self.assertEqual(server.variables["customDomainCreate"]["input"]["serviceId"], "svc-gw")
        code, _, err = run(["logs", self.app, "--gateway"], {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": server.url})
        self.assertEqual(code, 0, err)
        self.assertEqual(server.variables["serviceInstance"]["serviceId"], "svc-gw")
        code, _, err = run(["destroy", self.app, "--yes"], {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": server.url})
        self.assertEqual(code, 0, err)
        self.assertEqual(server.calls[-1], "projectDelete")  # the gateway is the app's own: the whole project goes


class PrivateByDefaultTest(unittest.TestCase):
    """small deploy of an app without sign-in of its own puts the gateway in front before it has an address."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = self.tmp.name
        write(self.app, "app.py", "print('an app with no sign-in')\n")
        self.server = GatewayRailway(vars_by_service={})
        self.server.services, self.server.domains = {}, {}
        self.addCleanup(self.server.close)

    def deploy(self, *extra):
        return run(["deploy", self.app, "--name", "team-tool", "--interval", "0", *extra],
                   {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": self.server.url})

    def state(self):
        return json.loads(Path(self.app, ".small.json").read_text(encoding="utf-8"))

    def test_first_deploy_is_private(self):
        code, out, err = self.deploy()
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "https://team-tool.up.railway.app")
        server = self.server
        self.assertEqual(server.services, {"svc-1": "team-tool", "svc-gw": "team-tool-gateway"})
        # The address was created for the gateway, never for the app.
        self.assertEqual((server.domain_of("svc-gw"), server.domain_of("svc-1")), (["team-tool.up.railway.app"], []))
        self.assertEqual(server.variables["serviceDomainCreate"]["input"]["serviceId"], "svc-gw")
        # The owner's invite link: in small.json, and on the gateway only.
        token = json.loads(Path(self.app, "small.json").read_text(encoding="utf-8"))["env"]["ACCESS_TOKEN"]
        self.assertEqual(server.vars["svc-gw"]["ACCESS_TOKEN"], token)
        self.assertNotIn("ACCESS_TOKEN", server.vars["svc-1"])
        self.assertEqual(server.vars["svc-1"]["SMALL_GATEWAY_KEY"], "${{team-tool-gateway.SMALL_GATEWAY_KEY}}")
        self.assertEqual(server.vars["svc-gw"]["UPSTREAM"], "http://${{team-tool.RAILWAY_PRIVATE_DOMAIN}}:8080")
        # The app's code, then the gateway's; both waited for.
        self.assertEqual(server.uploads[-1], ["./Caddyfile", "./Dockerfile", "./gateway.py", "./magic_link.py"])
        self.assertIn("./app.py", server.uploads[0])
        self.assertEqual(self.state()["gateway"]["version"], __version__)
        self.assertIn("small open", err)
        # small open has a link right away.
        code, out, _ = run(["link", self.app], {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": self.server.url})
        self.assertEqual(out.strip(), "https://team-tool.up.railway.app/?token=" + token)
        # The next deploy only ships the app.
        uploads = len(server.uploads)
        self.assertEqual(self.deploy()[0], 0)
        self.assertEqual(len(server.uploads), uploads + 1)

    def test_public_opts_out_and_a_warning_follows(self):
        code, out, err = self.deploy("--public")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.server.services, {"svc-1": "team-tool"})
        self.assertEqual(self.server.domain_of("svc-1"), ["team-tool.up.railway.app"])
        self.assertNotIn("small gateway on", err)
        code, _, err = self.deploy()  # already deployed open: say so, but don't change it behind the owner's back
        self.assertEqual(code, 0, err)
        self.assertIn("small gateway on", err)
        self.assertNotIn("svc-gw", self.server.services)

    def test_apps_with_their_own_sign_in_stay_as_they_are(self):
        write(self.app, "magic_link.py", "# a template's sign-in\n")
        code, _, err = self.deploy()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.server.services, {"svc-1": "team-tool"})
        self.assertNotIn("small gateway on", err)

    def test_a_gateway_that_fails_to_build_is_reported(self):
        self.server.gateway_status = "FAILED"
        code, _, err = self.deploy()
        self.assertEqual(code, 1)


def setUpModule():
    i18n.use("ru")


def tearDownModule():
    i18n.use(None)


if __name__ == "__main__":
    unittest.main()
