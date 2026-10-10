"""Сквозные тесты `small deploy` против локального мока Railway API.

Запуск: python -m unittest discover -s tests
"""

import contextlib
import http.client
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from small_cli.archive import IgnoreRules
from small_cli.cli import main
from small_cli.config import load_config
from small_cli.dashboard import DashboardServer, add_live_status, add_usage, scan_apps, start_server
from small_cli.scaffold import TEMPLATES_DIR

TOKEN = "test-token"


class MockRailway:
    """Имитирует нужную часть backboard.railway.com: /graphql/v2 и /project/.../up."""

    def __init__(self, final_status="SUCCESS", workspaces=None, domain_free=True, projects=None, domain=None,
                 live=None, delay=0, service_vars=None, project=None, logs=None, metrics=None, workflow="Complete"):
        self.final_status = final_status
        self.sleep = {}            # serviceId -> режим сна
        self.metrics = metrics or {}  # projectId -> [(MEASUREMENT, [(часов до конца периода, значение), ...])]
        self.workflow = workflow   # статус workflowStatus для развёртывания шаблона
        self.custom_domains = []
        self.service_vars = service_vars or {}  # переменные сервиса в Railway
        self.project = project  # {"name", "services": [{"id", "name"}]} для project(id); None — «не найден»
        failure = [("2026-10-10T12:00:00Z", "ERROR: No matching distribution for flask==99")]
        self.logs = logs or {"buildLogs": failure, "deploymentLogs": failure}  # поле -> [(timestamp, message)]
        self.live = live or {}  # serviceId -> статус последнего деплоя ("" — деплоев не было)
        self.delay = delay      # задержка ответа GraphQL, для проверки таймаутов
        self.workspaces = workspaces if workspaces is not None else [{"id": "ws-1", "name": "Personal"}]
        self.domain_free = domain_free
        self.projects = projects or []  # уже существующие проекты (узлы в формате API)
        self.calls = []        # имена GraphQL-полей и "UPLOAD" в порядке вызова
        self.history = []      # переменные каждого GraphQL-вызова (параллельно calls, без UPLOAD)
        self.variables = {}    # последние переменные по имени поля
        self.uploads = []      # списки файлов из каждого загруженного архива
        self.statuses = []
        self.domain = domain
        self.service_name = None
        mock_server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    return self._send(200, {"errors": [{"message": "Not Authorized"}]})
                path = urlparse(self.path)
                if path.path == "/graphql/v2":
                    if mock_server.delay:
                        time.sleep(mock_server.delay)
                    return self._send(200, mock_server.graphql(json.loads(body)))
                m = re.fullmatch(r"/project/([^/]+)/environment/([^/]+)/up", path.path)
                if m and self.headers.get("Content-Type") == "application/gzip":
                    service_id = parse_qs(path.query).get("serviceId", [""])[0]
                    return self._send(200, mock_server.upload(m.group(1), service_id, body))
                self._send(404, {"message": "not found"})

            def _send(self, code, payload):
                raw = json.dumps(payload).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except ConnectionError:
                    pass  # клиент уже ушёл по таймауту (тест медленного Railway)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def graphql(self, payload):
        field = re.search(r"\{\s*(\w+)", payload["query"]).group(1)
        variables = payload.get("variables") or {}
        self.calls.append(field)
        self.history.append(variables)
        self.variables[field] = variables
        if field == "me":
            return {"data": {"me": {"workspaces": self.workspaces}}}
        if field == "serviceInstance":
            status = self.live.get(variables["serviceId"])
            if status is None:
                return {"errors": [{"message": "ServiceInstance not found"}]}
            latest = {"id": "dep-live", "status": status, "createdAt": "2026-10-10T12:00:00Z"} if status else None
            return {"data": {"serviceInstance": {"latestDeployment": latest,
                                                 "sleepApplication": self.sleep.get(variables["serviceId"], False)}}}
        if field == "serviceInstanceUpdate":
            if "sleepApplication" in variables["input"]:
                self.sleep[variables["s"]] = variables["input"]["sleepApplication"]
            return {"data": {"serviceInstanceUpdate": True}}
        if field == "metrics":
            end = datetime.fromisoformat(variables["end"].replace("Z", "+00:00")).timestamp()
            series = [{"measurement": m, "values": [{"ts": int(end - hours * 3600), "value": v} for hours, v in points]}
                      for m, points in self.metrics.get(variables["p"], [])]
            return {"data": {"metrics": series}}
        if field == "template":
            config = {"services": {"tpl-svc": {"name": "Postgres", "variables": {
                "DATABASE_URL": {"defaultValue": "postgresql://u:${{POSTGRES_PASSWORD}}@db:5432/railway"},
                "SSL_CERT_DAYS": {"defaultValue": "", "isOptional": True}}}}}
            return {"data": {"template": {"id": "tpl-pg", "name": "PostgreSQL", "serializedConfig": config}}}
        if field == "templateDeployV2":
            if self.project is not None:
                self.project["services"].append({"id": "svc-pg", "name": "Postgres"})
            return {"data": {"templateDeployV2": {"projectId": variables["input"]["projectId"], "workflowId": "wf-1"}}}
        if field == "workflowStatus":
            return {"data": {"workflowStatus": {"status": self.workflow,
                                                "error": "boom" if self.workflow == "Error" else None}}}
        if field == "customDomainCreate":
            item = {"id": "cd-%d" % (len(self.custom_domains) + 1), "domain": variables["input"]["domain"],
                    "targetPort": variables["input"].get("targetPort"), "status": {
                        "dnsRecords": [{"recordType": "DNS_RECORD_TYPE_CNAME", "hostlabel": "app",
                                        "fqdn": variables["input"]["domain"], "requiredValue": "abc123.up.railway.app",
                                        "currentValue": "", "status": "DNS_RECORD_STATUS_REQUIRES_UPDATE",
                                        "purpose": "DNS_RECORD_PURPOSE_TRAFFIC_ROUTE"}],
                        "verified": False, "verificationDnsHost": "_railway-verify.app",
                        "verificationToken": "railway-verify=xyz", "certificateStatus": "CERTIFICATE_STATUS_TYPE_VALIDATING_OWNERSHIP"}}
            self.custom_domains.append(item)
            return {"data": {"customDomainCreate": item}}
        if field == "customDomainDelete":
            self.custom_domains = [d for d in self.custom_domains if d["id"] != variables["id"]]
            return {"data": {"customDomainDelete": True}}
        if field == "projects":
            return {"data": {"projects": {"edges": [{"node": p} for p in self.projects]}}}
        if field == "variableCollectionUpsert":
            self.service_vars.update(variables["input"]["variables"])
            return {"data": {"variableCollectionUpsert": True}}
        if field == "variableDelete":
            self.service_vars.pop(variables["input"]["name"], None)
            return {"data": {"variableDelete": True}}
        if field == "serviceInstanceRedeploy":
            return {"data": {"serviceInstanceRedeploy": True}}
        if field == "projectCreate":
            edges = [{"node": {"id": "env-1", "name": "production"}}]
            return {"data": {"projectCreate": {"id": "proj-1", "environments": {"edges": edges}}}}
        if field == "serviceCreate":
            self.service_name = variables["input"]["name"]
            return {"data": {"serviceCreate": {"id": "svc-1"}}}
        if field == "serviceDomainCreate":
            self.domain = "%s-production-a1b2.up.railway.app" % self.service_name
            return {"data": {"serviceDomainCreate": {"id": "dom-1", "domain": self.domain}}}
        if field == "serviceDomainAvailable":
            return {"data": {"serviceDomainAvailable": {"available": self.domain_free}}}
        if field == "serviceDomainUpdate":
            self.domain = variables["input"]["domain"]
            return {"data": {"serviceDomainUpdate": True}}
        if field == "domains":
            domains = [{"id": "dom-1", "domain": self.domain}] if self.domain else []
            return {"data": {"domains": {"serviceDomains": domains, "customDomains": self.custom_domains}}}
        if field == "deployment":
            status = self.statuses.pop(0) if self.statuses else self.final_status
            return {"data": {"deployment": {"status": status}}}
        if field in ("buildLogs", "deploymentLogs"):
            lines = [line for line in self.logs.get(field, [])
                     if not variables.get("startDate") or line[0] >= variables["startDate"]]
            lines = lines[-(variables.get("limit") or len(lines) or 1):]
            return {"data": {field: [{"timestamp": ts, "message": msg, "severity": "info"} for ts, msg in lines]}}
        if field == "variables":
            return {"data": {"variables": self.service_vars}}
        if field == "project":
            if self.project is None:
                return {"errors": [{"message": "Project not found"}]}
            edges = [{"node": service} for service in self.project["services"]]
            return {"data": {"project": {"name": self.project["name"], "services": {"edges": edges}}}}
        if field in ("projectDelete", "serviceDelete"):
            return {"data": {field: True}}
        return {"errors": [{"message": "unknown field " + field}]}

    def upload(self, project_id, service_id, body):
        self.calls.append("UPLOAD")
        self.history.append(None)
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
            self.uploads.append(sorted(tar.getnames()))
        self.statuses = ["QUEUED", "BUILDING", "DEPLOYING"]
        n = len(self.uploads)
        return {"deploymentId": "dep-%d" % n, "url": "", "deploymentDomain": "",
                "logsUrl": "https://railway.com/project/%s/service/%s?id=dep-%d" % (project_id, service_id, n)}


def run(argv, env):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env, clear=False), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        for var in ("RAILWAY_API_TOKEN", "RAILWAY_TOKEN", "RAILWAY_WORKSPACE_ID"):
            if var not in env:
                os.environ.pop(var, None)
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def write(root, rel, text="x"):
    path = Path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = self.tmp.name
        write(self.app, "app.py", "print('hi')\n")
        write(self.app, "requirements.txt", "flask\n")
        write(self.app, "lib/util.py")
        write(self.app, ".gitignore", "secret.txt\nbuild/\n*.log\n!keep.log\n")
        for junk in ("secret.txt", "build/out.txt", "debug.log", "keep.log",
                     "node_modules/a.js", "__pycache__/app.pyc", ".git/HEAD"):
            write(self.app, junk)

    def tearDown(self):
        self.tmp.cleanup()

    def deploy(self, server, *extra, name="Demo App"):
        env = {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": server.url}
        name_args = ["--name", name] if name else []
        return run(["deploy", self.app, *name_args, "--interval", "0", *extra], env)

    def test_first_deploy_creates_everything_and_prints_url(self):
        server = MockRailway()
        self.addCleanup(server.close)
        code, out, err = self.deploy(server)

        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "https://demo-app.up.railway.app")
        self.assertEqual(server.calls, ["me", "projects", "projectCreate", "serviceCreate", "UPLOAD",
                                        "serviceDomainCreate", "serviceDomainAvailable", "serviceDomainUpdate",
                                        "domains", "deployment", "deployment", "deployment", "deployment"])
        self.assertEqual(server.variables["projectCreate"]["input"], {"name": "demo-app", "workspaceId": "ws-1"})
        self.assertEqual(server.uploads, [["./.gitignore", "./app.py", "./keep.log",
                                           "./lib/util.py", "./requirements.txt"]])
        for status in ("QUEUED", "BUILDING", "DEPLOYING", "SUCCESS"):
            self.assertIn(status, err)
        state = json.loads(Path(self.app, ".small.json").read_text(encoding="utf-8"))
        self.assertEqual(state, {"name": "demo-app", "projectId": "proj-1", "environmentId": "env-1",
                                 "serviceId": "svc-1", "domain": "demo-app.up.railway.app"})

    def test_redeploy_reuses_project_and_domain(self):
        server = MockRailway()
        self.addCleanup(server.close)
        self.assertEqual(self.deploy(server)[0], 0)
        server.calls.clear()

        code, out, err = self.deploy(server)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "https://demo-app.up.railway.app")
        self.assertEqual(set(server.calls), {"UPLOAD", "deployment"})
        self.assertEqual(len(server.uploads), 2)

    def test_keeps_generated_domain_when_name_is_taken(self):
        server = MockRailway(domain_free=False)
        self.addCleanup(server.close)
        code, out, _ = self.deploy(server)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "https://demo-app-production-a1b2.up.railway.app")
        self.assertNotIn("serviceDomainUpdate", server.calls)

    def test_failed_build_prints_logs_and_exits_1(self):
        server = MockRailway(final_status="FAILED")
        self.addCleanup(server.close)
        code, out, err = self.deploy(server)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("No matching distribution", err)
        self.assertIn("FAILED", err)
        self.assertIn("buildLogs", server.calls)

    def test_crash_shows_runtime_logs(self):
        server = MockRailway(final_status="CRASHED")
        self.addCleanup(server.close)
        code, _, _ = self.deploy(server)
        self.assertEqual(code, 1)
        self.assertIn("deploymentLogs", server.calls)

    def test_no_wait_prints_url_without_polling(self):
        server = MockRailway()
        self.addCleanup(server.close)
        code, out, _ = self.deploy(server, "--no-wait")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "https://demo-app.up.railway.app")
        self.assertNotIn("deployment", server.calls)

    def test_multiple_workspaces_require_choice(self):
        server = MockRailway(workspaces=[{"id": "ws-1", "name": "Personal"}, {"id": "ws-2", "name": "Team"}])
        self.addCleanup(server.close)
        code, _, err = self.deploy(server)
        self.assertEqual(code, 1)
        self.assertIn("--workspace", err)
        self.assertIn("ws-2", err)
        self.assertNotIn("projectCreate", server.calls)

        code, _, _ = self.deploy(server, "--workspace", "ws-2")
        self.assertEqual(code, 0)
        self.assertEqual(server.variables["projectCreate"]["input"]["workspaceId"], "ws-2")

    def test_missing_token(self):
        code, _, err = run(["deploy", self.app], {})
        self.assertEqual(code, 1)
        self.assertIn("RAILWAY_API_TOKEN", err)

    def test_token_copied_with_brackets_quotes_or_spaces(self):
        from small_cli.railway import token_from_env
        for raw in ("<%s>" % TOKEN, ' "%s" ' % TOKEN, "'%s'\n" % TOKEN):
            with mock.patch.dict(os.environ, {"RAILWAY_API_TOKEN": raw}):
                self.assertEqual(token_from_env(), TOKEN, raw)
        with mock.patch.dict(os.environ, {"RAILWAY_API_TOKEN": " <> "}):
            self.assertIsNone(token_from_env())
        server = MockRailway()
        self.addCleanup(server.close)
        code, _, err = run(["deploy", self.app, "--interval", "0"],
                           {"RAILWAY_API_TOKEN": "<%s>" % TOKEN, "SMALL_API_URL": server.url})
        self.assertEqual(code, 0, err)  # мок пускает только «Bearer test-token» — значит, скобки отброшены

    def test_bad_token(self):
        server = MockRailway()
        self.addCleanup(server.close)
        code, _, err = run(["deploy", self.app, "--interval", "0"],
                           {"RAILWAY_API_TOKEN": "wrong", "SMALL_API_URL": server.url})
        self.assertEqual(code, 1)
        self.assertIn("Not Authorized", err)
        self.assertFalse(Path(self.app, ".small.json").exists())


def project_node(project_id, name, services=("svc-old",), deleted=False):
    return {"id": project_id, "name": name, "deletedAt": "2026-01-01T00:00:00Z" if deleted else None,
            "environments": {"edges": [{"node": {"id": "env-staging", "name": "staging"}},
                                       {"node": {"id": "env-old", "name": "production"}}]},
            "services": {"edges": [{"node": {"id": sid, "name": name if i == 0 else "worker"}}
                                   for i, sid in enumerate(services)]}}


class ConfigFileTest(unittest.TestCase):
    """small.json: имя проекта/сервиса и переменные окружения."""

    CONFIG = {"name": "my-custom-app-name",
              "env": {"OPENAI_API_KEY": "sk-test-123", "DEBUG": True, "WORKERS": 2}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        write(self.app, "app.py", "print('hi')\n")
        write(self.app, "conf/small.json", "{}")  # не корневой small.json — обычный файл приложения

    def write_config(self, config, raw=None):
        # utf-8-sig: так сохраняет Блокнот Windows, BOM не должен мешать.
        Path(self.app, "small.json").write_text(raw if raw is not None else json.dumps(config), encoding="utf-8-sig")

    def deploy(self, server, *extra):
        return run(["deploy", self.app, "--interval", "0", *extra],
                   {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": server.url})

    def server(self, **kwargs):
        server = MockRailway(**kwargs)
        self.addCleanup(server.close)
        return server

    def state(self):
        return json.loads(Path(self.app, ".small.json").read_text(encoding="utf-8"))

    def test_name_and_env_are_applied_before_upload(self):
        self.write_config(self.CONFIG)
        server = self.server()
        code, out, err = self.deploy(server)

        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "https://my-custom-app-name.up.railway.app")
        self.assertEqual(server.variables["projectCreate"]["input"]["name"], "my-custom-app-name")
        self.assertEqual(server.variables["serviceCreate"]["input"]["name"], "my-custom-app-name")
        self.assertEqual(server.variables["variableCollectionUpsert"]["input"], {
            "projectId": "proj-1", "environmentId": "env-1", "serviceId": "svc-1",
            "variables": {"OPENAI_API_KEY": "sk-test-123", "DEBUG": "true", "WORKERS": "2"},
            "replace": False, "skipDeploys": True,
        })
        self.assertLess(server.calls.index("variableCollectionUpsert"), server.calls.index("UPLOAD"))
        # Корневой small.json с секретами не уходит в Railway, вложенный conf/small.json — уходит.
        self.assertEqual(server.uploads, [["./app.py", "./conf/small.json"]])
        self.assertIn("DEBUG, OPENAI_API_KEY, WORKERS", err)
        self.assertNotIn("sk-test-123", err + out)

    def test_redeploy_updates_env_without_new_project(self):
        self.write_config(self.CONFIG)
        server = self.server()
        self.assertEqual(self.deploy(server)[0], 0)
        server.calls.clear()

        self.write_config({"name": "my-custom-app-name", "env": {"DEBUG": "false"}})
        code, _, err = self.deploy(server)
        self.assertEqual(code, 0, err)
        self.assertNotIn("projects", server.calls)
        self.assertNotIn("projectCreate", server.calls)
        self.assertEqual(server.variables["variableCollectionUpsert"]["input"]["variables"], {"DEBUG": "false"})
        self.assertLess(server.calls.index("variableCollectionUpsert"), server.calls.index("UPLOAD"))

    def test_binds_to_existing_project_with_same_name(self):
        self.write_config(self.CONFIG)
        server = self.server(
            projects=[project_node("proj-other", "other-app"),
                      project_node("proj-deleted", "my-custom-app-name", deleted=True),
                      project_node("proj-old", "my-custom-app-name", services=("svc-old", "svc-worker"))],
            domain="my-custom-app-name-production-zz.up.railway.app")
        code, out, err = self.deploy(server)

        self.assertEqual(code, 0, err)
        self.assertIn("Найден проект my-custom-app-name", err)
        for call in ("projectCreate", "serviceCreate", "serviceDomainCreate"):
            self.assertNotIn(call, server.calls)
        self.assertEqual(self.state(), {
            "name": "my-custom-app-name", "projectId": "proj-old", "environmentId": "env-old",
            "serviceId": "svc-old", "domain": "my-custom-app-name-production-zz.up.railway.app"})
        self.assertEqual(server.variables["variableCollectionUpsert"]["input"]["serviceId"], "svc-old")
        self.assertEqual(out.strip(), "https://my-custom-app-name-production-zz.up.railway.app")

    def test_ambiguous_project_name_is_an_error(self):
        self.write_config(self.CONFIG)
        server = self.server(projects=[project_node("proj-a", "my-custom-app-name"),
                                       project_node("proj-b", "my-custom-app-name")])
        code, _, err = self.deploy(server)
        self.assertEqual(code, 1)
        self.assertIn("несколько проектов", err)
        self.assertNotIn("UPLOAD", server.calls)

    def test_new_flag_creates_project_even_if_name_exists(self):
        self.write_config(self.CONFIG)
        server = self.server(projects=[project_node("proj-old", "my-custom-app-name")])
        code, _, err = self.deploy(server, "--new")
        self.assertEqual(code, 0, err)
        self.assertNotIn("projects", server.calls)
        self.assertEqual(self.state()["projectId"], "proj-1")

    def test_cli_name_overrides_config(self):
        self.write_config({"name": "from-config"})
        server = self.server()
        code, _, _ = self.deploy(server, "--name", "From Flag")
        self.assertEqual(code, 0)
        self.assertEqual(server.variables["projectCreate"]["input"]["name"], "from-flag")
        self.assertNotIn("variableCollectionUpsert", server.calls)  # env не задан — переменные не трогаем

    def test_config_name_ignored_for_already_linked_folder(self):
        server = self.server()
        self.assertEqual(self.deploy(server)[0], 0)  # без small.json: имя из папки
        linked = self.state()
        self.write_config({"name": "renamed-app"})
        server.calls.clear()

        code, _, err = self.deploy(server)
        self.assertEqual(code, 0)
        self.assertEqual(self.state(), linked)
        self.assertNotIn("projectCreate", server.calls)
        self.assertIn("renamed-app не применено", err)

    def test_invalid_config_is_rejected_before_any_api_call(self):
        cases = {
            "{не json": "некорректный JSON",
            '["x"]': "ожидается объект",
            '{"nmae": "x"}': "неизвестные поля nmae",
            '{"name": ""}': "name должен быть непустой строкой",
            '{"env": ["A=1"]}': "env должен быть объектом",
            '{"env": {"1BAD": "x"}}': "недопустимое имя переменной '1BAD'",
            '{"env": {"KEY": null}}': "значение KEY должно быть строкой",
        }
        server = self.server()
        for raw, message in cases.items():
            self.write_config(None, raw=raw)
            code, _, err = self.deploy(server)
            self.assertEqual(code, 1, raw)
            self.assertIn(message, err, raw)
        self.assertEqual(server.calls, [])


MAGIC_LINK_DIR = Path(__file__).resolve().parents[2] / "magic-link"  # исходники middleware


def http_get(port, path, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
    res = conn.getresponse()
    body = res.read().decode("utf-8", "replace")
    conn.close()
    return res.status, res, body


class InitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = os.path.join(self.tmp.name, "My Cool App")

    def init(self, *args, path=None):
        return run(["init", path or self.app, *args], {})

    def read(self, rel):
        return Path(self.app, rel).read_text(encoding="utf-8")

    def config(self):
        return load_config(Path(self.app, "small.json"))  # тот же парсер, что у small deploy

    def test_default_template_creates_project(self):
        code, out, err = self.init()
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        self.assertEqual(sorted(os.listdir(self.app)),
                         [".gitignore", ".railwayignore", "app.py", "magic_link.py", "small.json"])
        config = self.config()
        self.assertRegex(config["name"], r"^my-cool-app-[0-9a-f]{4}$")
        self.assertEqual(list(config["env"]), ["ACCESS_TOKEN"])
        self.assertGreaterEqual(len(config["env"]["ACCESS_TOKEN"]), 40)
        gitignore = self.read(".gitignore").splitlines()
        for line in ("small.json", ".small.json", ".env", "__pycache__/"):
            self.assertIn(line, gitignore)
        self.assertIn("*.md", self.read(".railwayignore").splitlines())
        self.assertIn("small deploy", err)
        self.assertNotIn(config["env"]["ACCESS_TOKEN"], err)  # токен не печатается

    def test_names_and_tokens_are_unique(self):
        other = os.path.join(self.tmp.name, "other")
        self.assertEqual(self.init()[0], 0)
        self.assertEqual(self.init(path=other)[0], 0)
        first, second = self.config(), load_config(Path(other, "small.json"))
        self.assertNotEqual(first["env"]["ACCESS_TOKEN"], second["env"]["ACCESS_TOKEN"])
        self.assertTrue(second["name"].startswith("other-"))

    def test_explicit_name(self):
        self.assertEqual(self.init("--name", "Billing API")[0], 0)
        self.assertEqual(self.config()["name"], "billing-api")

    def test_node_template(self):
        code, _, err = self.init("--template", "node-http")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(os.listdir(self.app)),
                         [".gitignore", ".railwayignore", "magic-link.js", "package.json", "server.js", "small.json"])
        self.assertEqual(json.loads(self.read("package.json"))["scripts"]["start"], "node server.js")
        self.assertIn("node_modules/", self.read(".gitignore").splitlines())

    def test_unknown_template_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as exit_:
            main(["init", self.app, "--template", "ruby"])
        self.assertEqual(exit_.exception.code, 2)
        self.assertIn("python-http", err.getvalue())
        self.assertFalse(os.path.exists(self.app))

    def test_existing_files_are_not_overwritten(self):
        write(self.app, "app.py", "my code\n")
        write(self.app, "small.json", '{"name": "mine"}')
        code, _, err = self.init()
        self.assertEqual(code, 1)
        self.assertIn("app.py, small.json", err)
        self.assertIn("--force", err)
        self.assertEqual(self.read("app.py"), "my code\n")
        self.assertEqual(sorted(os.listdir(self.app)), ["app.py", "small.json"])  # ничего не дописано

    def test_force_overwrites(self):
        write(self.app, "app.py", "my code\n")
        code, _, err = self.init("--force")
        self.assertEqual(code, 0, err)
        self.assertIn("MagicLink(app)", self.read("app.py"))
        self.assertRegex(err, r"app\.py\s+перезаписан")

    def test_interactive_confirmation(self):
        write(self.app, "app.py", "my code\n")
        for answer, expected_code, expected_content in (("n", 1, "my code\n"), ("да", 0, None)):
            with mock.patch.object(sys, "stdin") as stdin, mock.patch("builtins.input", return_value=answer):
                stdin.isatty.return_value = True
                code, _, err = self.init()
            self.assertEqual(code, expected_code, answer)
            self.assertIn("Перезаписать? [y/N]", err)
            if expected_content:
                self.assertEqual(self.read("app.py"), expected_content)
            else:
                self.assertIn("MagicLink(app)", self.read("app.py"))

    def test_existing_ignore_files_are_merged(self):
        write(self.app, ".gitignore", "dist/\n.env")
        code, _, err = self.init()
        self.assertEqual(code, 0, err)
        lines = self.read(".gitignore").splitlines()
        self.assertEqual(lines[:2], ["dist/", ".env"])           # строки пользователя на месте
        self.assertEqual(lines.count(".env"), 1)                 # без дублей
        self.assertIn("small.json", lines)
        self.assertRegex(err, r"\.gitignore\s+дополнен")

        os.remove(os.path.join(self.app, "app.py"))
        before = self.read(".gitignore")
        self.assertEqual(self.init("--force")[0], 0)
        self.assertEqual(self.read(".gitignore"), before)         # повторный init ничего не дублирует

    def test_init_then_deploy(self):
        self.assertEqual(self.init()[0], 0)
        config = self.config()
        server = MockRailway()
        self.addCleanup(server.close)
        code, out, err = run(["deploy", self.app, "--interval", "0"],
                             {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": server.url})
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "https://%s.up.railway.app" % config["name"])
        self.assertEqual(server.variables["projectCreate"]["input"]["name"], config["name"])
        self.assertEqual(server.variables["variableCollectionUpsert"]["input"]["variables"], config["env"])
        self.assertEqual(server.uploads, [["./.gitignore", "./.railwayignore", "./app.py", "./magic_link.py"]])

    def check_running_app(self, command, token):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = dict(os.environ, PORT=str(port), HOST="127.0.0.1", ACCESS_TOKEN=token)
        proc = subprocess.Popen(command, cwd=self.app, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        status, _, body = http_get(port, "/")
        self.assertEqual(status, 403)
        self.assertIn("Запросить доступ", body)
        status, res, _ = http_get(port, "/?token=" + token)
        self.assertEqual((status, res.getheader("Location")), (302, "/"))
        status, _, body = http_get(port, "/", cookie=res.getheader("Set-Cookie").split(";")[0])
        self.assertEqual(status, 200)
        self.assertIn("Работает!", body)

    def test_python_template_runs(self):
        self.assertEqual(self.init()[0], 0)
        self.check_running_app([sys.executable, "app.py"], self.config()["env"]["ACCESS_TOKEN"])

    @unittest.skipUnless(shutil.which("node"), "node не установлен")
    def test_node_template_runs(self):
        self.assertEqual(self.init("--template", "node-http")[0], 0)
        self.check_running_app(["node", "server.js"], self.config()["env"]["ACCESS_TOKEN"])

    @unittest.skipUnless(MAGIC_LINK_DIR.is_dir(), "нет папки magic-link с исходниками middleware")
    def test_template_middleware_matches_source(self):
        for template, name in (("python-http", "magic_link.py"), ("fastapi", "magic_link.py"),
                               ("node-http", "magic-link.js")):
            self.assertEqual((TEMPLATES_DIR / template / name).read_bytes(), (MAGIC_LINK_DIR / name).read_bytes(),
                             "%s в шаблоне %s разошёлся с magic-link/%s" % (name, template, name))


SECRET = "super-secret-token-value"


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        files = {
            "apps/billing/small.json": {"name": "billing", "env": {"DEBUG": "true", "ACCESS_TOKEN": SECRET}},
            "apps/billing/.small.json": {"name": "billing", "projectId": "proj-1", "environmentId": "env-1",
                                         "serviceId": "svc-1", "domain": "billing-x1.up.railway.app"},
            "apps/billing/conf/small.json": {},          # внутри приложения — не отдельное приложение
            "fresh/small.json": {"name": "fresh-app"},
            "legacy/.small.json": {"name": "legacy", "projectId": "proj-2", "environmentId": "env-2",
                                   "serviceId": "svc-2"},
            "unnamed/small.json": {},
            "evil/.small.json": {"projectId": "../x", "serviceId": "svc-3", "domain": "javascript:alert(1)"},
            "node_modules/pkg/small.json": {},           # служебные папки не сканируются
        }
        for rel, data in files.items():
            write(self.root, rel, json.dumps(data))
        write(self.root, "broken/small.json", "{oops")
        write(self.root, "plain/app.py", "")              # без small.json — не приложение

    def apps(self, **kwargs):
        return {a["relPath"]: a for a in scan_apps(self.root, **kwargs)}

    def test_scan_finds_apps_and_describes_them(self):
        apps = self.apps()
        self.assertEqual(list(apps), ["apps/billing", "broken", "evil", "fresh", "legacy", "unnamed"])
        self.assertEqual(apps["apps/billing"], {
            "name": "billing", "path": str(Path(self.root, "apps", "billing").resolve()), "relPath": "apps/billing",
            "status": "linked", "url": "https://billing-x1.up.railway.app",
            "railwayUrl": "https://railway.com/project/proj-1/service/svc-1",
            "envNames": ["ACCESS_TOKEN", "DEBUG"], "errors": [],
            "serviceId": "svc-1", "environmentId": "env-1", "projectId": "proj-1", "deployment": None,
            "sleep": None, "costUsd": None})
        self.assertEqual((apps["fresh"]["name"], apps["fresh"]["status"], apps["fresh"]["url"]),
                         ("fresh-app", "not_deployed", None))
        self.assertEqual((apps["legacy"]["name"], apps["legacy"]["status"], apps["legacy"]["url"]),
                         ("legacy", "linked", None))
        self.assertEqual(apps["legacy"]["railwayUrl"], "https://railway.com/project/proj-2/service/svc-2")
        self.assertEqual(apps["unnamed"]["name"], "unnamed")  # имя папки, если имени нигде нет

    def test_scan_never_exposes_env_values(self):
        self.assertNotIn(SECRET, json.dumps(scan_apps(self.root)))

    def test_scan_reports_broken_files_and_rejects_unsafe_links(self):
        apps = self.apps()
        self.assertEqual(apps["broken"]["status"], "not_deployed")
        self.assertIn("некорректный JSON", apps["broken"]["errors"][0])
        self.assertEqual((apps["evil"]["url"], apps["evil"]["railwayUrl"]), (None, None))
        self.assertIn("некорректный домен", apps["evil"]["errors"][0])

    def test_scan_depth_limit_and_root_app(self):
        write(self.root, "a/b/c/deep/small.json", "{}")
        self.assertNotIn("a/b/c/deep", self.apps(max_depth=3))
        self.assertIn("a/b/c/deep", self.apps(max_depth=4))
        root_app = scan_apps(Path(self.root, "apps", "billing"))
        self.assertEqual([(a["relPath"], a["name"]) for a in root_app], [(".", "billing")])

    def serve(self):
        server = start_server(self.root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_http_page_and_api(self):
        port = self.serve()
        status, res, body = http_get(port, "/")
        self.assertEqual(status, 200)
        self.assertEqual(res.getheader("Content-Type"), "text/html; charset=utf-8")
        self.assertIn('<script src="https://cdn.tailwindcss.com/3.4.17"></script>', body)
        self.assertIn('id="refresh"', body)

        status, res, body = http_get(port, "/api/apps")
        self.assertEqual(status, 200)
        self.assertEqual(res.getheader("Content-Type"), "application/json; charset=utf-8")
        data = json.loads(body)
        self.assertEqual(data["root"], str(Path(self.root).resolve()))
        self.assertEqual(len(data["apps"]), 6)
        self.assertNotIn(SECRET, body)

        write(self.root, "added-later/small.json", '{"name": "added-later"}')  # кнопка «Обновить» = новый запрос
        names = [a["name"] for a in json.loads(http_get(port, "/api/apps")[2])["apps"]]
        self.assertIn("added-later", names)
        self.assertEqual(http_get(port, "/nope")[0], 404)

    def test_foreign_host_is_rejected(self):
        port = self.serve()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/apps", headers={"Host": "evil.example:%d" % port})
        res = conn.getresponse()
        self.assertEqual(res.status, 403)
        self.assertNotIn(b"billing", res.read())
        conn.close()
        self.assertEqual(http_get(port, "/api/apps")[0], 200)

    def test_busy_port_falls_back_to_free_one(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            taken = busy.getsockname()[1]
            server = start_server(self.root, port=taken)
            self.addCleanup(server.server_close)
            self.assertNotEqual(server.server_address[1], taken)

    def test_command_prints_url_and_opens_browser(self):
        for extra, browser_opened in (([], True), (["--no-browser"], False)):
            with mock.patch("webbrowser.open") as open_browser, \
                    mock.patch.object(DashboardServer, "serve_forever", side_effect=KeyboardInterrupt):
                code, out, err = run(["dashboard", self.root, "--port", "0", *extra], {})
            self.assertEqual(code, 0, err)
            url = out.strip()
            self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/$")
            self.assertEqual(open_browser.called, browser_opened)
            if browser_opened:
                open_browser.assert_called_once_with(url)
            self.assertIn("приложений найдено: 6", err)
            self.assertIn("Статусы деплоя из Railway: выключены", err)
            self.assertIn("Дашборд остановлен", err)

    def test_missing_folder(self):
        code, _, err = run(["dashboard", os.path.join(self.root, "nope"), "--no-browser"], {})
        self.assertEqual(code, 1)
        self.assertIn("папка не найдена", err)


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DashboardLiveStatusTest(unittest.TestCase):
    """Живой статус деплоя из Railway в дашборде (Railway — локальный мок)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        for name, service in (("billing", "svc-1"), ("worker", "svc-2"), ("empty", "svc-3")):
            write(self.root, name + "/.small.json", json.dumps({
                "name": name, "projectId": "proj-1", "environmentId": "env-1", "serviceId": service}))
        write(self.root, "fresh/small.json", '{"name": "fresh"}')  # не привязан — в Railway не спрашиваем

    def railway(self, **kwargs):
        server = MockRailway(**kwargs)
        self.addCleanup(server.close)
        patcher = mock.patch.dict(os.environ, {"SMALL_API_URL": server.url})
        patcher.start()
        self.addCleanup(patcher.stop)
        return server

    def live(self, token=TOKEN, timeout=5):
        apps = scan_apps(self.root)
        mode = add_live_status(apps, token, timeout=timeout)
        return mode, {a["relPath"]: a["deployment"] for a in apps}

    def test_statuses_from_railway(self):
        server = self.railway(live={"svc-1": "SUCCESS", "svc-2": "BUILDING", "svc-3": ""})
        mode, deployments = self.live()
        self.assertEqual(mode, "on")
        self.assertEqual(deployments, {
            "billing": {"status": "SUCCESS", "createdAt": "2026-10-10T12:00:00Z"},
            "worker": {"status": "BUILDING", "createdAt": "2026-10-10T12:00:00Z"},
            "empty": None,   # деплоев ещё не было
            "fresh": None,
        })
        self.assertEqual(sorted(server.calls), ["serviceInstance"] * 3)

    def test_no_token_means_offline_without_requests(self):
        server = self.railway(live={"svc-1": "SUCCESS"})
        mode, deployments = self.live(token=None)
        self.assertEqual(mode, "off")
        self.assertEqual(set(deployments.values()), {None})
        self.assertEqual(server.calls, [])

    def test_one_failing_service_does_not_hide_others(self):
        self.railway(live={"svc-1": "FAILED"})  # svc-2, svc-3 — «не найдены»
        mode, deployments = self.live()
        self.assertEqual(mode, "on")
        self.assertEqual(deployments["billing"]["status"], "FAILED")
        self.assertIsNone(deployments["worker"])

    def test_network_error_falls_back_to_offline(self):
        with mock.patch.dict(os.environ, {"SMALL_API_URL": "http://127.0.0.1:%d" % closed_port()}):
            mode, deployments = self.live(timeout=2)
        self.assertEqual(mode, "error")
        self.assertEqual(set(deployments.values()), {None})

    def test_slow_railway_hits_timeout(self):
        self.railway(live={"svc-1": "SUCCESS", "svc-2": "SUCCESS", "svc-3": "SUCCESS"}, delay=3)
        started = time.monotonic()
        mode, deployments = self.live(timeout=0.5)
        self.assertLess(time.monotonic() - started, 2.5)  # запросы идут параллельно и не ждут ответа
        self.assertEqual(mode, "error")
        self.assertEqual(set(deployments.values()), {None})

    def test_rejected_token_falls_back(self):
        self.railway(live={"svc-1": "SUCCESS"})
        mode, deployments = self.live(token="wrong-token")
        self.assertEqual(mode, "error")
        self.assertEqual(set(deployments.values()), {None})

    def test_unexpected_status_is_ignored(self):
        self.railway(live={"svc-1": "<b>pwned</b>", "svc-2": "SUCCESS", "svc-3": ""})
        _, deployments = self.live()
        self.assertIsNone(deployments["billing"])
        self.assertEqual(deployments["worker"]["status"], "SUCCESS")

    def serve(self, token):
        server = start_server(self.root, port=0, token=token)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_api_returns_live_status_but_never_the_token(self):
        self.railway(live={"svc-1": "SUCCESS", "svc-2": "CRASHED", "svc-3": ""})
        status, _, body = http_get(self.serve(TOKEN), "/api/apps")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["live"], "on")
        statuses = {a["relPath"]: (a["deployment"] or {}).get("status") for a in data["apps"]}
        self.assertEqual(statuses, {"billing": "SUCCESS", "empty": None, "fresh": None, "worker": "CRASHED"})
        self.assertNotIn(TOKEN, body)

    def test_api_survives_railway_outage(self):
        with mock.patch.dict(os.environ, {"SMALL_API_URL": "http://127.0.0.1:%d" % closed_port()}):
            status, _, body = http_get(self.serve(TOKEN), "/api/apps")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["live"], "error")
        self.assertEqual(len(data["apps"]), 4)

    def test_command_reports_live_mode(self):
        with mock.patch("webbrowser.open"), \
                mock.patch.object(DashboardServer, "serve_forever", side_effect=KeyboardInterrupt):
            code, _, err = run(["dashboard", self.root, "--port", "0", "--no-browser"], {"RAILWAY_API_TOKEN": TOKEN})
        self.assertEqual(code, 0, err)
        self.assertIn("Статусы деплоя из Railway: включены", err)


HAS_STREAMLIT = importlib.util.find_spec("streamlit") is not None


class StreamlitTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = os.path.join(self.tmp.name, "dash")
        code, _, err = run(["init", self.app, "--template", "streamlit"], {})
        self.assertEqual(code, 0, err)
        self.token = load_config(Path(self.app, "small.json"))["env"]["ACCESS_TOKEN"]

    def test_files(self):
        self.assertEqual(sorted(os.listdir(self.app)),
                         [".gitignore", ".railwayignore", "app.py", "requirements.txt", "small.json"])
        self.assertRegex(Path(self.app, "requirements.txt").read_text(encoding="utf-8"), r"(?m)^streamlit\b")
        source = Path(self.app, "app.py").read_text(encoding="utf-8")
        compile(source, "app.py", "exec")
        self.assertIn("require_access()", source)
        gitignore = Path(self.app, ".gitignore").read_text(encoding="utf-8").splitlines()
        for line in ("small.json", ".small.json", "__pycache__/", ".streamlit/secrets.toml"):
            self.assertIn(line, gitignore)
        self.assertGreaterEqual(len(self.token), 40)

    def test_deploy_uploads_app_without_secrets(self):
        server = MockRailway()
        self.addCleanup(server.close)
        code, _, err = run(["deploy", self.app, "--interval", "0"],
                           {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": server.url})
        self.assertEqual(code, 0, err)
        self.assertEqual(server.uploads, [["./.gitignore", "./.railwayignore", "./app.py", "./requirements.txt"]])
        self.assertEqual(server.variables["variableCollectionUpsert"]["input"]["variables"], {"ACCESS_TOKEN": self.token})

    def test_help_lists_all_templates(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit_:
            main(["init", "--help"])
        self.assertEqual(exit_.exception.code, 0)
        for template in ("python-http", "node-http", "streamlit"):
            self.assertIn(template, out.getvalue())

    # --- ниже — на настоящем Streamlit (нужен `pip install streamlit`) ---

    def app_test(self, token=None, env=None):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(os.path.join(self.app, "app.py"), default_timeout=30)
        if token is not None:
            at.query_params["token"] = token
        with mock.patch.dict(os.environ, dict({"ACCESS_TOKEN": self.token}, **(env or {}))):
            at.run()
        return at

    @unittest.skipUnless(HAS_STREAMLIT, "streamlit не установлен")
    def test_streamlit_gate_without_token(self):
        at = self.app_test()
        self.assertFalse(at.exception)
        self.assertIn("Доступ по приглашению", at.markdown[0].value)
        self.assertEqual([b.label for b in at.button], ["Запросить доступ"])
        self.assertEqual(len(at.title), 0)  # интерфейс приложения не показан

    @unittest.skipUnless(HAS_STREAMLIT, "streamlit не установлен")
    def test_streamlit_gate_with_wrong_token(self):
        at = self.app_test(token="wrong")
        self.assertIn("Ссылка недействительна", at.markdown[0].value)
        self.assertEqual(len(at.title), 0)

    @unittest.skipUnless(HAS_STREAMLIT, "streamlit не установлен")
    def test_streamlit_app_with_valid_token(self):
        at = self.app_test(token=self.token)
        self.assertFalse(at.exception)
        self.assertEqual(at.title[0].value, "🚀 small + Streamlit")
        self.assertEqual([m.label for m in at.metric], ["Альфа", "Бета", "Гамма"])
        at.sidebar.slider[0].set_value(300).run()  # интерактивность: перерисовка с другими параметрами
        self.assertFalse(at.exception)

    @unittest.skipUnless(HAS_STREAMLIT, "streamlit не установлен")
    def test_streamlit_personal_token(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            at = self.app_test(token=self.token, env={"ACCESS_TOKEN": "owner-tok,bob:" + self.token})
        self.assertEqual(at.title[0].value, "🚀 small + Streamlit")
        self.assertIn("вход по ссылке (bob)", out.getvalue())
        revoked = self.app_test(token=self.token, env={"ACCESS_TOKEN": "owner-tok"})  # bob отозван
        self.assertIn("Ссылка недействительна", revoked.markdown[0].value)

    @unittest.skipUnless(HAS_STREAMLIT, "streamlit не установлен")
    def test_streamlit_access_request_is_logged(self):
        at = self.app_test()
        at.text_input[0].input("me@example.com\n[access-request] fake")  # попытка подделать строку лога
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, {"ACCESS_TOKEN": self.token}):
            at.button[0].click().run()
        self.assertIn("Запрос отправлен", at.success[0].value)
        self.assertIn("[access-request] contact='me@example.com\\n[access-request] fake'", out.getvalue())
        self.assertEqual(out.getvalue().count("\n"), 1)  # одна строка лога, перевод строки экранирован

    @unittest.skipUnless(HAS_STREAMLIT, "streamlit не установлен")
    def test_python_app_py_starts_streamlit_server(self):
        # Так приложение запускает Railway: `python app.py` должен поднять сервер Streamlit на $PORT.
        port = closed_port()
        env = dict(os.environ, PORT=str(port), HOST="127.0.0.1", ACCESS_TOKEN=self.token)
        proc = subprocess.Popen([sys.executable, "app.py"], cwd=self.app, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        for _ in range(150):
            try:
                if http_get(port, "/_stcore/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        status, _, body = http_get(port, "/_stcore/health")
        self.assertEqual((status, body), (200, "ok"))


class OpsCommandsTest(unittest.TestCase):
    """small link / open / logs / destroy для уже задеплоенного приложения."""

    STATE = {"name": "demo", "projectId": "proj-1", "environmentId": "env-1", "serviceId": "svc-1",
             "domain": "demo.up.railway.app"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = self.tmp.name
        write(self.app, "small.json", json.dumps({"name": "demo", "env": {"ACCESS_TOKEN": "tok-config, tok-2"}}))
        write(self.app, ".small.json", json.dumps(self.STATE))

    def railway(self, **kwargs):
        kwargs.setdefault("project", {"name": "demo", "services": [{"id": "svc-1", "name": "demo"}]})
        server = MockRailway(**kwargs)
        self.addCleanup(server.close)
        return server

    def cli(self, server, *args, token=TOKEN):
        env = {"SMALL_API_URL": server.url} if server else {}
        if token:
            env["RAILWAY_API_TOKEN"] = token
        return run([*args, self.app], env)

    # --- link / open ---

    def test_link_uses_first_token_from_config(self):
        with mock.patch("webbrowser.open") as browser:
            code, out, err = self.cli(None, "link", token=None)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "https://demo.up.railway.app/?token=tok-config")
        self.assertIn("ACCESS_TOKEN из small.json", err)
        browser.assert_not_called()

    def test_open_prints_and_opens_link(self):
        with mock.patch("webbrowser.open") as browser:
            code, out, _ = self.cli(None, "open", token=None)
        self.assertEqual(code, 0)
        browser.assert_called_once_with(out.strip())

    def test_link_falls_back_to_railway_variables(self):
        write(self.app, "small.json", '{"name": "demo"}')
        server = self.railway(service_vars={"ACCESS_TOKEN": "tok railway&x", "PORT": "8080"})
        code, out, err = self.cli(server, "link")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "https://demo.up.railway.app/?token=tok%20railway%26x")  # экранирование
        self.assertIn("ACCESS_TOKEN из Railway", err)
        self.assertEqual(server.variables["variables"],
                         {"projectId": "proj-1", "environmentId": "env-1", "serviceId": "svc-1"})

    def test_link_without_any_token_prints_plain_url(self):
        write(self.app, "small.json", '{"name": "demo"}')
        code, out, err = self.cli(None, "link", token=None)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "https://demo.up.railway.app/")
        self.assertIn("ACCESS_TOKEN не найден", err)

    def test_commands_need_a_deployed_app(self):
        os.remove(os.path.join(self.app, ".small.json"))
        for command in ("link", "open", "logs", "destroy"):
            code, _, err = self.cli(None, command)
            self.assertEqual(code, 1, command)
            self.assertIn("Сначала: small deploy", err)

    # --- logs ---

    def test_logs_of_latest_deployment(self):
        server = self.railway(live={"svc-1": "SUCCESS"}, logs={"deploymentLogs": [
            ("2026-10-10T12:00:01.123456789Z", "Listening on 0.0.0.0:8080"),
            ("2026-10-10T12:00:05Z", "[access-request] contact='me@example.com'")]})
        code, out, err = self.cli(server, "logs")
        self.assertEqual(code, 0, err)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^\d\d:\d\d:\d\d  Listening on 0\.0\.0\.0:8080$")
        self.assertTrue(lines[1].endswith("[access-request] contact='me@example.com'"))
        self.assertIn("деплой dep-live · SUCCESS", err)
        self.assertEqual(server.variables["deploymentLogs"]["id"], "dep-live")

    def test_build_logs_and_line_limit(self):
        server = self.railway(live={"svc-1": "FAILED"}, logs={"buildLogs": [
            ("2026-10-10T12:00:0%dZ" % i, "step %d" % i) for i in range(5)]})
        code, out, _ = run(["logs", self.app, "--build", "-n", "2"],
                           {"SMALL_API_URL": server.url, "RAILWAY_API_TOKEN": TOKEN})
        self.assertEqual(code, 0)
        self.assertEqual([l.split("  ", 1)[1] for l in out.strip().splitlines()], ["step 3", "step 4"])
        self.assertEqual(server.variables["buildLogs"]["limit"], 2)

    def test_logs_without_deployments(self):
        server = self.railway(live={"svc-1": ""})
        code, _, err = self.cli(server, "logs")
        self.assertEqual(code, 1)
        self.assertIn("ещё нет деплоев", err)

    def test_logs_follow_prints_only_new_lines(self):
        first = [("2026-10-10T12:00:01Z", "old line")]
        server = self.railway(live={"svc-1": "SUCCESS"}, logs={"deploymentLogs": list(first)})
        sleeps = []

        def fake_sleep(seconds):  # между опросами в «Railway» появляется новая строка; на 2-м опросе — Ctrl+C
            if seconds != 2.0:  # time.sleep общий для процесса: чужие вызовы не трогаем
                return
            sleeps.append(seconds)
            if len(sleeps) == 1:
                server.logs["deploymentLogs"].append(("2026-10-10T12:00:09Z", "new line"))
            else:
                raise KeyboardInterrupt

        with mock.patch("time.sleep", side_effect=fake_sleep):
            code, out, err = run(["logs", self.app, "--follow"],
                                 {"SMALL_API_URL": server.url, "RAILWAY_API_TOKEN": TOKEN})
        self.assertEqual(code, 0, err)
        self.assertEqual([l.split("  ", 1)[1] for l in out.strip().splitlines()], ["old line", "new line"])
        self.assertEqual(server.variables["deploymentLogs"]["startDate"], "2026-10-10T12:00:01Z")

    # --- destroy ---

    def test_destroy_whole_project(self):
        server = self.railway()
        code, _, err = self.cli(server, "destroy", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("projectDelete", server.calls)
        self.assertNotIn("serviceDelete", server.calls)
        self.assertEqual(server.variables["projectDelete"], {"id": "proj-1"})
        self.assertFalse(Path(self.app, ".small.json").exists())
        self.assertTrue(Path(self.app, "small.json").exists())   # настройки остаются

    def test_destroy_only_own_service_in_shared_project(self):
        server = self.railway(project={"name": "shared", "services": [
            {"id": "svc-1", "name": "demo"}, {"id": "svc-db", "name": "postgres"}]})
        code, _, err = self.cli(server, "destroy", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(server.variables["serviceDelete"], {"id": "svc-1"})
        self.assertNotIn("projectDelete", server.calls)
        self.assertIn("Другие сервисы проекта останутся: postgres", err)

    def test_destroy_requires_confirmation(self):
        server = self.railway()
        code, _, err = self.cli(server, "destroy")  # без терминала и без --yes
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        for answer, deleted in (("wrong-name", False), ("demo", True)):
            with mock.patch.object(sys, "stdin") as stdin, mock.patch("builtins.input", return_value=answer):
                stdin.isatty.return_value = True
                code, _, err = self.cli(server, "destroy")
            self.assertIn("Введите имя проекта (demo)", err)
            self.assertEqual(code, 0 if deleted else 1, answer)
            self.assertEqual(Path(self.app, ".small.json").exists(), not deleted)
        self.assertEqual(server.calls.count("projectDelete"), 1)

    def test_destroy_missing_project_keeps_state(self):
        server = self.railway(project=None)
        code, _, err = self.cli(server, "destroy", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("не найден в Railway", err)
        self.assertTrue(Path(self.app, ".small.json").exists())

    def test_railway_commands_need_token(self):
        for command in ("logs", "destroy"):
            code, _, err = self.cli(None, command, token=None)
            self.assertEqual(code, 1)
            self.assertIn("RAILWAY_API_TOKEN", err)


class EnvAndAccessTest(unittest.TestCase):
    """small env и small access: Railway и small.json меняются вместе."""

    STATE = {"name": "demo", "projectId": "proj-1", "environmentId": "env-1", "serviceId": "svc-1",
             "domain": "demo.up.railway.app"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = self.tmp.name
        self.config({"name": "demo", "env": {"ACCESS_TOKEN": "tok-owner", "SAME": "1", "DIFF": "local"}})
        write(self.app, ".small.json", json.dumps(self.STATE))

    def config(self, data=None):
        path = Path(self.app, "small.json")
        if data is not None:
            path.write_text(json.dumps(data), encoding="utf-8")
        return json.loads(path.read_text(encoding="utf-8"))

    def railway(self, **vars_):
        server = MockRailway(service_vars=dict({"ACCESS_TOKEN": "tok-owner", "SAME": "1", "DIFF": "remote",
                                                "ONLY_RAILWAY": "zzz", "RAILWAY_PUBLIC_DOMAIN": "x"}, **vars_))
        self.addCleanup(server.close)
        return server

    def cli(self, server, *args, token=TOKEN):
        env = {"SMALL_API_URL": server.url} if server else {}
        if token:
            env["RAILWAY_API_TOKEN"] = token
        where = [self.app] if args[0] in ("link", "open") else ["-C", self.app]  # у link папка — позиционно
        return run([args[0], *where, *args[1:]], env)

    # --- env ---

    def test_env_list_shows_sources_and_hides_values(self):
        code, out, err = self.cli(self.railway(), "env")
        self.assertEqual(code, 0, err)
        rows = {line.split()[0]: line for line in out.strip().splitlines()}
        self.assertEqual(sorted(rows), ["ACCESS_TOKEN", "DIFF", "ONLY_RAILWAY", "SAME"])  # RAILWAY_* скрыты
        self.assertIn("small.json + Railway", rows["SAME"])
        self.assertIn("Railway ≠ small.json", rows["DIFF"])
        self.assertIn("только Railway", rows["ONLY_RAILWAY"])
        for secret in ("tok-owner", "zzz", "remote"):
            self.assertNotIn(secret, out)
        code, out, _ = self.cli(self.railway(), "env", "list", "--values")
        self.assertIn("zzz", out)

    def test_env_set_updates_railway_and_config_then_redeploys(self):
        server = self.railway()
        code, _, err = self.cli(server, "env", "set", "NEW=hello world", "DIFF=fixed")
        self.assertEqual(code, 0, err)
        self.assertEqual(server.variables["variableCollectionUpsert"]["input"]["variables"],
                         {"NEW": "hello world", "DIFF": "fixed"})
        self.assertTrue(server.variables["variableCollectionUpsert"]["input"]["skipDeploys"])
        self.assertEqual(server.calls[-1], "serviceInstanceRedeploy")  # один перезапуск в конце
        self.assertEqual(self.config(), {"name": "demo", "env": {
            "ACCESS_TOKEN": "tok-owner", "SAME": "1", "DIFF": "fixed", "NEW": "hello world"}})
        self.assertIn("перезапускает", err)

    def test_env_unset_and_no_deploy(self):
        server = self.railway()
        code, _, err = self.cli(server, "env", "unset", "DIFF", "ONLY_RAILWAY", "MISSING", "--no-deploy")
        self.assertEqual(code, 0, err)
        deleted = [v for c, v in zip(server.calls, server.history) if c == "variableDelete"]
        self.assertEqual(sorted(d["input"]["name"] for d in deleted), ["DIFF", "ONLY_RAILWAY"])
        self.assertNotIn("serviceInstanceRedeploy", server.calls)
        self.assertNotIn("DIFF", self.config()["env"])
        self.assertIn("при следующем деплое", err)

    def test_env_before_first_deploy_changes_only_config(self):
        os.remove(os.path.join(self.app, ".small.json"))
        code, _, err = self.cli(None, "env", "set", "A=1", token=None)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.config()["env"]["A"], "1")
        self.assertIn("ещё не задеплоено", err)

    def test_env_set_rejects_bad_input_and_railway_errors_keep_config(self):
        before = self.config()
        for bad in ("1BAD=x", "NOEQUALS"):
            code, _, err = self.cli(None, "env", "set", bad)
            self.assertEqual(code, 1, bad)
            self.assertIn("KEY=VALUE", err)
        code, _, err = self.cli(self.railway(), "env", "set", "A=1", token="wrong-token")
        self.assertEqual(code, 1)
        self.assertIn("Not Authorized", err)
        self.assertEqual(self.config(), before)  # Railway отказал — small.json не тронут

    # --- access ---

    def test_access_add_list_link_revoke(self):
        server = self.railway()
        code, out, err = self.cli(server, "access", "add", "bob")
        self.assertEqual(code, 0, err)
        stored = self.config()["env"]["ACCESS_TOKEN"]
        self.assertRegex(stored, r"^tok-owner,bob:[A-Za-z0-9_-]{40,}$")
        bob_token = stored.split("bob:")[1]
        self.assertEqual(out.strip(), "https://demo.up.railway.app/?token=" + bob_token)
        self.assertEqual(server.variables["variableCollectionUpsert"]["input"]["variables"], {"ACCESS_TOKEN": stored})
        self.assertEqual(server.calls[-1], "serviceInstanceRedeploy")

        code, out, _ = self.cli(None, "link", "--as", "bob", token=None)
        self.assertEqual(out.strip(), "https://demo.up.railway.app/?token=" + bob_token)
        code, out, _ = self.cli(None, "link", token=None)  # без --as — ссылка владельца
        self.assertEqual(out.strip(), "https://demo.up.railway.app/?token=tok-owner")
        code, out, _ = self.cli(None, "access", token=None)
        self.assertEqual([line.split()[0] for line in out.strip().splitlines()], ["(общая", "bob"])
        self.assertNotIn(bob_token, out)

        self.assertEqual(self.cli(server, "access", "add", "bob")[0], 1)       # уже есть
        self.assertEqual(self.cli(server, "access", "add", "bad name")[0], 1)  # недопустимое имя
        code, _, err = self.cli(server, "access", "revoke", "bob")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.config()["env"]["ACCESS_TOKEN"], "tok-owner")
        code, _, err = self.cli(None, "link", "--as", "bob", token=None)
        self.assertEqual(code, 1)
        self.assertIn("small access add bob", err)

    def test_revoking_last_token_closes_the_app(self):
        self.config({"name": "demo", "env": {"ACCESS_TOKEN": "alice:tok-a"}})
        server = self.railway(ACCESS_TOKEN="alice:tok-a")
        code, _, err = self.cli(server, "access", "revoke", "alice")
        self.assertEqual(code, 0, err)
        self.assertIn("закрыто для всех", err)
        self.assertNotIn("ACCESS_TOKEN", self.config()["env"])
        self.assertIn("variableDelete", server.calls)

    def test_access_uses_railway_token_when_config_has_none(self):
        self.config({"name": "demo"})
        server = self.railway(ACCESS_TOKEN="from-railway")
        code, _, err = self.cli(server, "access", "add", "carol", "--no-deploy")
        self.assertEqual(code, 0, err)
        self.assertRegex(self.config()["env"]["ACCESS_TOKEN"], r"^from-railway,carol:")
        self.assertNotIn("serviceInstanceRedeploy", server.calls)


class DashboardActionsTest(unittest.TestCase):
    """Кнопки «Открыть по ссылке» и «Логи»."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        write(self.root, "shop/small.json", json.dumps({"name": "shop", "env": {"ACCESS_TOKEN": "tok-a b,bob:tok-b"}}))
        write(self.root, "shop/.small.json", json.dumps({"name": "shop", "projectId": "proj-1", "environmentId": "env-1",
                                                          "serviceId": "svc-1", "domain": "shop.up.railway.app"}))
        write(self.root, "draft/small.json", '{"name": "draft"}')

    def serve(self, token=None, api_url=None):
        if api_url:
            patcher = mock.patch.dict(os.environ, {"SMALL_API_URL": api_url})
            patcher.start()
            self.addCleanup(patcher.stop)
        server = start_server(self.root, port=0, token=token)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_page_has_buttons(self):
        body = http_get(self.serve(), "/")[2]
        for text in ('"Открыть по ссылке"', '"Логи"', 'id="logs"', ">Сборка<"):
            self.assertIn(text, body)
        # Tailwind-класс display (flex, grid, block) перебивает атрибут hidden: окно было бы открыто сразу.
        modal_classes = re.search(r'<div id="logs" hidden class="([^"]*)"', body).group(1).split()
        self.assertFalse({"flex", "grid", "block", "inline-flex"} & set(modal_classes), modal_classes)

    def test_open_redirects_with_owner_token(self):
        port = self.serve()
        status, res, _ = http_get(port, "/open?app=shop")
        self.assertEqual(status, 302)
        self.assertEqual(res.getheader("Location"), "https://shop.up.railway.app/?token=tok-a%20b")
        self.assertEqual(res.getheader("Referrer-Policy"), "no-referrer")
        self.assertEqual(http_get(port, "/open?app=draft")[0], 404)       # не задеплоено
        self.assertEqual(http_get(port, "/open?app=../etc")[0], 404)      # только приложения из списка
        apps = json.loads(http_get(port, "/api/apps")[2])
        self.assertNotIn("tok-b", json.dumps(apps))                       # в JSON токенов по-прежнему нет

    def test_logs_need_token(self):
        status, _, body = http_get(self.serve(), "/api/logs?app=shop")
        self.assertEqual(status, 400)
        self.assertIn("RAILWAY_API_TOKEN", json.loads(body)["error"])

    def test_logs_from_railway(self):
        railway = MockRailway(live={"svc-1": "SUCCESS"}, logs={
            "deploymentLogs": [("2026-10-10T12:00:01Z", "Listening on 0.0.0.0:8080")],
            "buildLogs": [("2026-10-10T11:59:00Z", "pip install fastapi")]})
        self.addCleanup(railway.close)
        port = self.serve(token=TOKEN, api_url=railway.url)
        data = json.loads(http_get(port, "/api/logs?app=shop")[2])
        self.assertEqual(data["deployment"], {"id": "dep-live", "status": "SUCCESS"})
        self.assertEqual(data["lines"], [{"time": "2026-10-10T12:00:01Z", "message": "Listening on 0.0.0.0:8080"}])
        build = json.loads(http_get(port, "/api/logs?app=shop&build=1")[2])
        self.assertEqual(build["lines"][0]["message"], "pip install fastapi")
        self.assertEqual(http_get(port, "/api/logs?app=draft")[0], 404)

    def test_logs_when_railway_is_down(self):
        port = self.serve(token=TOKEN, api_url="http://127.0.0.1:%d" % closed_port())
        status, _, body = http_get(port, "/api/logs?app=shop")
        self.assertEqual(status, 502)
        self.assertIn("Railway", json.loads(body)["error"])


HAS_FASTAPI = all(importlib.util.find_spec(m) is not None for m in ("fastapi", "uvicorn", "httpx"))


class FastAPITemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = os.path.join(self.tmp.name, "api")
        code, _, err = run(["init", self.app, "--template", "fastapi"], {})
        self.assertEqual(code, 0, err)
        self.token = load_config(Path(self.app, "small.json"))["env"]["ACCESS_TOKEN"]

    def test_files(self):
        self.assertEqual(sorted(os.listdir(self.app)),
                         [".gitignore", ".railwayignore", "magic_link.py", "main.py", "requirements.txt", "small.json"])
        requirements = Path(self.app, "requirements.txt").read_text(encoding="utf-8")
        self.assertRegex(requirements, r"(?m)^fastapi\b")
        self.assertRegex(requirements, r"(?m)^uvicorn\b")  # по ним Railpack сам запускает `uvicorn main:app`
        source = Path(self.app, "main.py").read_text(encoding="utf-8")
        compile(source, "main.py", "exec")
        self.assertIn("app.add_middleware(MagicLinkASGI)", source)

    @unittest.skipUnless(HAS_FASTAPI, "fastapi/uvicorn/httpx не установлены")
    def test_fastapi_app_behind_invite_link(self):
        from fastapi.testclient import TestClient
        sys.path.insert(0, self.app)
        self.addCleanup(sys.path.remove, self.app)
        spec = importlib.util.spec_from_file_location("small_fastapi_main", os.path.join(self.app, "main.py"))
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"ACCESS_TOKEN": "owner-tok,bob:" + self.token}), \
                contextlib.redirect_stdout(io.StringIO()) as log:
            spec.loader.exec_module(module)
            client = TestClient(module.app)
            self.assertEqual(client.get("/").status_code, 403)
            self.assertEqual(client.get("/docs").status_code, 403)
            res = client.get("/?token=" + self.token, follow_redirects=False)
            self.assertEqual((res.status_code, res.headers["location"]), (302, "/"))
            page = client.get("/")  # кука уже у клиента
            self.assertEqual(page.status_code, 200)
            self.assertIn("small + FastAPI", page.text)
            self.assertEqual(client.get("/api/hello", params={"name": "bob"}).json()["message"], "Привет, bob!")
            self.assertEqual(client.get("/docs").status_code, 200)
        self.assertIn("вход по ссылке (bob)", log.getvalue())

    @unittest.skipUnless(HAS_FASTAPI, "fastapi/uvicorn/httpx не установлены")
    def test_uvicorn_like_railway_and_token_hidden_in_access_log(self):
        port = closed_port()
        env = dict(os.environ, ACCESS_TOKEN=self.token, PYTHONUNBUFFERED="1")
        log_path = os.path.join(self.tmp.name, "uvicorn.log")
        with open(log_path, "wb") as log_file:
            proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                                     "--port", str(port)], cwd=self.app, env=env, stdout=log_file, stderr=log_file)
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            self.assertEqual(http_get(port, "/")[0], 403)
            status, res, _ = http_get(port, "/?token=" + self.token)
            self.assertEqual(status, 302)
            self.assertEqual(http_get(port, "/", cookie=res.getheader("Set-Cookie").split(";")[0])[0], 200)
            time.sleep(0.3)
        log = Path(log_path).read_text(encoding="utf-8", errors="replace")
        self.assertIn("token=***", log)
        self.assertNotIn(self.token, log)


class MockTelegram:
    """Bot API: getMe, getUpdates, sendMessage. Принимает только токен BOT."""

    BOT = "123456:test-bot-token"

    def __init__(self, updates=None):
        self.updates = list(updates or [])
        self.sent = []
        mock_server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                m = re.fullmatch(r"/bot([^/]+)/(\w+)", self.path)
                if not m or m.group(1) != MockTelegram.BOT:
                    return self._send(401, {"ok": False, "description": "Unauthorized"})
                method = m.group(2)
                if method == "getMe":
                    return self._send(200, {"ok": True, "result": {"id": 1, "username": "small_test_bot"}})
                if method == "getUpdates":
                    return self._send(200, {"ok": True, "result": mock_server.updates})
                if method == "sendMessage":
                    mock_server.sent.append(body)
                    return self._send(200, {"ok": True, "result": {"message_id": len(mock_server.sent)}})
                self._send(404, {"ok": False, "description": "Not Found"})

            def _send(self, code, payload):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class PlatformCommandsTest(unittest.TestCase):
    """small sleep, db, domain, notify на моках Railway и Telegram."""

    STATE = {"name": "demo", "projectId": "proj-1", "environmentId": "env-1", "serviceId": "svc-1",
             "domain": "demo.up.railway.app"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = self.tmp.name
        write(self.app, "small.json", json.dumps({"name": "demo", "env": {"ACCESS_TOKEN": "tok-owner"}}))
        write(self.app, ".small.json", json.dumps(self.STATE))

    def railway(self, **kwargs):
        kwargs.setdefault("project", {"name": "demo", "services": [{"id": "svc-1", "name": "demo"}]})
        kwargs.setdefault("live", {"svc-1": "SUCCESS", "svc-pg": "SUCCESS"})
        server = MockRailway(**kwargs)
        self.addCleanup(server.close)
        return server

    def cli(self, server, *args, env=None):
        full = {"RAILWAY_API_TOKEN": TOKEN, "SMALL_API_URL": server.url} if server else {}
        full.update(env or {})
        where = [self.app] if args[0] == "destroy" else ["-C", self.app]
        return run([args[0], *where, *args[1:]], full)

    def state(self):
        return json.loads(Path(self.app, ".small.json").read_text(encoding="utf-8"))

    def config(self):
        return json.loads(Path(self.app, "small.json").read_text(encoding="utf-8"))

    # --- sleep ---

    def test_sleep_on_off_and_status(self):
        server = self.railway()
        code, out, err = self.cli(server, "sleep")
        self.assertEqual((code, out.strip()), (0, "off"), err)
        code, _, err = self.cli(server, "sleep", "on")
        self.assertEqual(code, 0, err)
        self.assertEqual(server.variables["serviceInstanceUpdate"],
                         {"s": "svc-1", "e": "env-1", "input": {"sleepApplication": True}})
        self.assertEqual(server.calls[-1], "serviceInstanceRedeploy")
        self.assertIn("просыпается", err)
        self.assertEqual(self.cli(server, "sleep")[1].strip(), "on")
        server.calls.clear()
        code, _, err = self.cli(server, "sleep", "on")  # уже включён — ничего не делаем
        self.assertIn("уже включён", err)
        self.assertNotIn("serviceInstanceUpdate", server.calls)
        code, _, err = self.cli(server, "sleep", "off", "--no-deploy")
        self.assertEqual(code, 0, err)
        self.assertNotIn("serviceInstanceRedeploy", server.calls)
        self.assertFalse(server.sleep["svc-1"])

    # --- db ---

    def test_db_add_list_remove(self):
        server = self.railway()
        code, _, err = self.cli(server, "db", "add", "postgres")
        self.assertEqual(code, 0, err)
        deployed = server.variables["templateDeployV2"]["input"]
        self.assertEqual((deployed["projectId"], deployed["environmentId"], deployed["templateId"]),
                         ("proj-1", "env-1", "tpl-pg"))
        variables = deployed["serializedConfig"]["services"]["tpl-svc"]["variables"]
        self.assertEqual(variables["DATABASE_URL"]["value"], variables["DATABASE_URL"]["defaultValue"])
        self.assertNotIn("value", variables["SSL_CERT_DAYS"])  # пустое значение по умолчанию не подставляем
        self.assertEqual(self.state()["databases"], [{"type": "postgres", "serviceId": "svc-pg", "name": "Postgres"}])
        self.assertEqual(self.config()["env"]["DATABASE_URL"], "${{Postgres.DATABASE_URL}}")
        self.assertEqual(server.service_vars["DATABASE_URL"], "${{Postgres.DATABASE_URL}}")
        self.assertEqual(server.calls[-1], "serviceInstanceRedeploy")

        code, out, _ = self.cli(server, "db")
        self.assertEqual(out.split(), ["postgres", "Postgres", "SUCCESS"])
        self.assertEqual(self.cli(server, "db", "add", "postgres")[0], 1)  # уже есть

        self.assertEqual(self.cli(server, "db", "remove", "postgres")[0], 1)  # без --yes и терминала — нельзя
        code, _, err = self.cli(server, "db", "remove", "postgres", "--yes")
        self.assertEqual(code, 0, err)
        self.assertEqual(server.variables["serviceDelete"], {"id": "svc-pg"})
        self.assertEqual(self.state()["databases"], [])
        self.assertNotIn("DATABASE_URL", self.config()["env"])

    def test_db_add_reports_failed_template(self):
        server = self.railway(workflow="Error")
        code, _, err = self.cli(server, "db", "add", "postgres")
        self.assertEqual(code, 1)
        self.assertIn("boom", err)
        self.assertNotIn("databases", self.state())

    def test_destroy_removes_app_together_with_its_database(self):
        server = self.railway(project={"name": "demo", "services": [{"id": "svc-1", "name": "demo"},
                                                                    {"id": "svc-pg", "name": "Postgres"}]})
        write(self.app, ".small.json", json.dumps(dict(self.STATE, databases=[
            {"type": "postgres", "serviceId": "svc-pg", "name": "Postgres"}])))
        code, _, err = self.cli(server, "destroy", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIn("projectDelete", server.calls)  # база — часть приложения, проект удаляется целиком
        self.assertNotIn("serviceDelete", server.calls)

    # --- domain ---

    def test_domain_add_list_remove(self):
        server = self.railway()
        code, out, err = self.cli(server, "domain", "add", "https://App.Example.com/")
        self.assertEqual(code, 0, err)
        self.assertEqual(server.variables["customDomainCreate"]["input"]["domain"], "app.example.com")
        rows = [line.split() for line in out.strip().splitlines()]
        self.assertEqual(rows, [["CNAME", "app.example.com", "abc123.up.railway.app"],
                                ["TXT", "_railway-verify.app", "railway-verify=xyz"]])
        code, out, _ = self.cli(server, "domain")
        self.assertIn("app.example.com", out)
        self.assertIn("ждёт DNS-записей", out)
        self.assertEqual(self.cli(server, "domain", "add", "app.example.com")[0], 1)  # уже есть
        self.assertEqual(self.cli(server, "domain", "add", "not a domain")[0], 1)
        code, _, err = self.cli(server, "domain", "remove", "app.example.com")
        self.assertEqual(code, 0, err)
        self.assertEqual(server.custom_domains, [])
        self.assertEqual(self.cli(server, "domain", "remove", "app.example.com")[0], 1)

    # --- notify ---

    def test_notify_telegram_finds_chat_and_sets_variables(self):
        server = self.railway()
        telegram = MockTelegram(updates=[{"update_id": 7, "message": {"chat": {"id": 4242, "first_name": "Ruslan"}}}])
        self.addCleanup(telegram.close)
        tg_env = {"TELEGRAM_BOT_TOKEN": MockTelegram.BOT, "TELEGRAM_API_URL": telegram.url}
        code, _, err = self.cli(server, "notify", "telegram", env=tg_env)
        self.assertEqual(code, 0, err)
        self.assertEqual(telegram.sent[0]["chat_id"], 4242)
        self.assertIn("demo", telegram.sent[0]["text"])
        self.assertIn("@small_test_bot → Ruslan", err)
        self.assertEqual(self.config()["env"]["TELEGRAM_CHAT_ID"], "4242")
        self.assertEqual(server.service_vars["TELEGRAM_BOT_TOKEN"], MockTelegram.BOT)
        self.assertNotIn(MockTelegram.BOT, err)  # токен бота не печатается
        self.assertEqual(self.cli(None, "notify")[1].strip(), "telegram")

        code, _, err = self.cli(server, "notify", "off")
        self.assertEqual(code, 0, err)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", self.config()["env"])
        self.assertNotIn("TELEGRAM_CHAT_ID", server.service_vars)

    def test_notify_telegram_errors(self):
        telegram = MockTelegram()
        self.addCleanup(telegram.close)
        code, _, err = self.cli(None, "notify", "telegram", env={"TELEGRAM_BOT_TOKEN": "wrong",
                                                                 "TELEGRAM_API_URL": telegram.url})
        self.assertEqual(code, 1)
        self.assertIn("Unauthorized", err)
        code, _, err = self.cli(None, "notify", "telegram", "--wait", "0",
                                env={"TELEGRAM_BOT_TOKEN": MockTelegram.BOT, "TELEGRAM_API_URL": telegram.url})
        self.assertEqual(code, 1)
        self.assertIn("@small_test_bot", err)  # никто не написал боту
        with mock.patch.object(sys, "stdin") as stdin:  # нет токена и нет терминала (иначе getpass ждал бы ввода)
            stdin.isatty.return_value = False
            code, _, err = run(["notify", "-C", self.app, "telegram"], {})
        self.assertEqual(code, 1)
        self.assertIn("TELEGRAM_BOT_TOKEN", err)


class DashboardUsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        write(self.root, "shop/.small.json", json.dumps({"name": "shop", "projectId": "proj-1",
                                                          "environmentId": "env-1", "serviceId": "svc-1"}))
        write(self.root, "draft/small.json", '{"name": "draft"}')

    def test_monthly_cost_from_average_load(self):
        from small_cli.dashboard import estimate_cost
        self.assertAlmostEqual(estimate_cost({"CPU_USAGE": 0.5, "MEMORY_USAGE_GB": 1.0}), 20.0)  # $10 + $10
        self.assertAlmostEqual(estimate_cost({"DISK_USAGE_GB": 2.0, "UNKNOWN": 99}), 0.3)

    def test_api_has_sleep_and_cost(self):
        # CPU: приложение создано 23 ч назад, всё время 0.1 ядра → среднее 0.1 (не занижаем новые приложения).
        # Память: первый замер 9 ч назад, потом 5 замеров по 0.2 ГБ, в остальные часы спало → среднее 0.1.
        railway = MockRailway(live={"svc-1": "SLEEPING"}, metrics={"proj-1": [
            ("CPU_USAGE", [(h, 0.1) for h in range(24)]),
            ("MEMORY_USAGE_GB", [(9, 0.2), (8, 0.2), (3, 0.2), (2, 0.2), (0, 0.2)])]})
        railway.sleep["svc-1"] = True
        self.addCleanup(railway.close)
        with mock.patch.dict(os.environ, {"SMALL_API_URL": railway.url}):
            apps = scan_apps(self.root)
            add_live_status(apps, TOKEN)
            add_usage(apps, TOKEN)
        shop = next(a for a in apps if a["name"] == "shop")
        self.assertEqual((shop["sleep"], shop["deployment"]["status"]), (True, "SLEEPING"))
        self.assertAlmostEqual(shop["costUsd"], 3.0)  # 0.1 ядра × $20 + 0.1 ГБ × $10
        draft = next(a for a in apps if a["name"] == "draft")
        self.assertEqual((draft["sleep"], draft["costUsd"]), (None, None))
        start = datetime.fromisoformat(railway.variables["metrics"]["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(railway.variables["metrics"]["end"].replace("Z", "+00:00"))
        self.assertEqual(((end - start).days, railway.variables["metrics"]["step"]), (7, 3600))

    def test_usage_errors_leave_cost_empty(self):
        with mock.patch.dict(os.environ, {"SMALL_API_URL": "http://127.0.0.1:%d" % closed_port()}):
            apps = scan_apps(self.root)
            add_usage(apps, TOKEN, timeout=1)
        self.assertEqual({a["costUsd"] for a in apps}, {None})


class LauncherTest(unittest.TestCase):
    def test_small_cmd_is_ascii_with_crlf(self):
        # cmd.exe разбирает UTF-8 и LF-переводы строк в .cmd с ошибками: обрывки строк исполняются как команды.
        data = (Path(__file__).resolve().parents[1] / "small.cmd").read_bytes()
        self.assertTrue(all(b < 128 for b in data), "small.cmd должен быть только ASCII")
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "small.cmd должен быть с переводами строк CRLF")


class IgnoreRulesTest(unittest.TestCase):
    def check(self, lines, path, is_dir=False):
        return IgnoreRules(lines).ignored(path, is_dir)

    def test_patterns(self):
        self.assertTrue(self.check(["*.log"], "a/b/debug.log"))
        self.assertTrue(self.check(["/build"], "build", is_dir=True))
        self.assertFalse(self.check(["/build"], "src/build", is_dir=True))
        self.assertTrue(self.check(["build/"], "src/build", is_dir=True))
        self.assertFalse(self.check(["build/"], "build"))
        self.assertTrue(self.check(["docs/*.md"], "docs/a.md"))
        self.assertFalse(self.check(["docs/*.md"], "docs/sub/a.md"))
        self.assertTrue(self.check(["docs/**/*.md"], "docs/sub/a.md"))
        self.assertTrue(self.check(["**/tmp"], "x/y/tmp", is_dir=True))
        self.assertFalse(self.check(["*.log", "!keep.log"], "keep.log"))
        self.assertTrue(self.check(["# comment", "", ".env"], ".env"))
        self.assertTrue(self.check(["file[0-9].txt"], "file7.txt"))


if __name__ == "__main__":
    unittest.main()
