"""`small dashboard`: a local page listing the micro-apps in a folder.

An app is a folder that has small.json and/or .small.json. The link to Railway and the URLs come from the local
files. If RAILWAY_API_TOKEN is set, for linked apps we also ask Railway for the status of the latest deploy;
any error (network, timeout, token) just leaves the local data.
Variable values from small.json and the Railway token itself are never sent to the browser.

The Share button (/api/access) is the only thing that changes data: it grants and revokes access with the same
logic as small access. Such requests are accepted only from this dashboard's page (see DashboardHandler._trusted).
"""

import hmac
import json
import os
import re
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import access
from .archive import ALWAYS_IGNORED
from .common import CliError
from .config import CONFIG_FILE, STATE_FILE, ConfigError, load_config, load_state
from .i18n import lang, tr
from .railway import Railway, RailwayError

DEFAULT_PORT = 3000
MAX_DEPTH = 6  # no deeper: `small dashboard` from the root of a drive must not walk the whole drive
DOMAIN_RE = re.compile(r"[A-Za-z0-9.-]+\Z")
RAILWAY_ID_RE = re.compile(r"[A-Za-z0-9-]+\Z")
DEPLOY_STATUS_RE = re.compile(r"[A-Z_]{1,32}\Z")
LIVE_TIMEOUT = 5      # seconds per request to Railway: the Refresh button must not hang
LIVE_WORKERS = 8
LOG_LINES = 200
LOAD_DAYS = 7
SECRET_HEADER = "X-Small-Dashboard"  # this server run's key in the page's requests to /api/access
MAX_BODY = 4096       # bytes in the POST /api/access body: it has only three short fields
MAX_WHO = 320         # characters in an email address, domain or name
# Railway monthly prices (docs.railway.com/reference/pricing/plans) per unit of average load from metrics.
MONTHLY_PRICES_USD = {
    "CPU_USAGE": 20.0,         # $20 per vCPU busy all month
    "MEMORY_USAGE_GB": 10.0,   # $10 per GB of memory all month
    "DISK_USAGE_GB": 0.15,     # $0.15 per GB of volume all month
}


def scan_apps(root, max_depth=MAX_DEPTH):
    """Finds apps under root. Does not descend into an app once found."""
    root = Path(root).resolve()
    apps = []
    for dirpath, dirnames, filenames in os.walk(root):
        folder = Path(dirpath)
        if CONFIG_FILE in filenames or STATE_FILE in filenames:
            apps.append(describe_app(folder, root))
            dirnames[:] = []
            continue
        depth = len(folder.relative_to(root).parts)
        dirnames[:] = sorted(d for d in dirnames if d not in ALWAYS_IGNORED) if depth < max_depth else []
    return sorted(apps, key=lambda a: a["relPath"].lower())


def describe_app(folder, root):
    rel = folder.relative_to(root).as_posix()
    app = {"name": folder.name, "path": str(folder), "relPath": "." if rel == "." else rel,
           "status": "not_deployed", "url": None, "railwayUrl": None, "envNames": [], "errors": [],
           "serviceId": None, "environmentId": None, "projectId": None, "deployment": None, "sleep": None,
           "costUsd": None}
    try:
        config = load_config(folder / CONFIG_FILE)
    except ConfigError as e:
        config = {"name": None, "env": {}}
        app["errors"].append(str(e))
    try:
        state = load_state(folder / STATE_FILE)
    except ConfigError as e:
        state = {}
        app["errors"].append(str(e).replace(str(folder / STATE_FILE), STATE_FILE))

    app["name"] = config["name"] or (state.get("name") if isinstance(state.get("name"), str) else None) or folder.name
    app["envNames"] = sorted(config["env"])  # names only: the values are secrets

    if state.get("serviceId"):
        app["status"] = "linked"
        project_id, service_id = state.get("projectId"), state.get("serviceId")
        if all(_is_id(v) for v in (project_id, service_id)):
            app["railwayUrl"] = "https://railway.com/project/%s/service/%s" % (project_id, service_id)
        if _is_id(service_id) and _is_id(state.get("environmentId")):
            app["serviceId"], app["environmentId"] = service_id, state["environmentId"]
        if _is_id(project_id):
            app["projectId"] = project_id
        domain = state.get("domain")
        if domain:
            if isinstance(domain, str) and DOMAIN_RE.match(domain):
                app["url"] = "https://" + domain
            else:
                app["errors"].append(tr("%s: invalid domain %r", "%s: некорректный домен %r") % (STATE_FILE, domain))
    return app


def _is_id(value):
    return isinstance(value, str) and bool(RAILWAY_ID_RE.match(value))


def add_live_status(apps, token, timeout=LIVE_TIMEOUT):
    """Adds app["deployment"] = {"status", "createdAt"} from Railway for linked apps.

    Returns the mode for the page: "off" (no token), "on" (Railway answered) or "error" (it never answered).
    Never raises: the live status is a nicety, the local data matters more.
    """
    linked = [app for app in apps if app["serviceId"] and app["environmentId"]]
    if not token:
        return "off"
    if not linked:
        return "on"
    api = Railway(token)

    def fetch(app):
        try:
            return app, api.instance_status(app["serviceId"], app["environmentId"], timeout=timeout), True
        except Exception:  # network, timeout, bad token, deleted service, unexpected response
            return app, None, False

    answered = False
    with ThreadPoolExecutor(max_workers=min(LIVE_WORKERS, len(linked))) as pool:
        for app, instance, ok in pool.map(fetch, linked):
            answered = answered or ok
            if not ok or not isinstance(instance, dict):
                continue
            app["sleep"] = bool(instance.get("sleep"))
            deployment = instance.get("deployment")
            if isinstance(deployment, dict):
                status = deployment.get("status")
                if isinstance(status, str) and DEPLOY_STATUS_RE.match(status):
                    created = deployment.get("createdAt")
                    app["deployment"] = {"status": status, "createdAt": created if isinstance(created, str) else None}
    return "on" if answered else "error"


def estimate_cost(load):
    """$ per month at average load `load` (cores, GB), at Railway prices; excluding egress
    and the plan's free allowance ($5 Hobby / $20 Pro)."""
    return sum(MONTHLY_PRICES_USD[m] * float(v) for m, v in load.items() if m in MONTHLY_PRICES_USD)


def add_usage(apps, token, timeout=LIVE_TIMEOUT, days=LOAD_DAYS):
    """Adds app["costUsd"]: an estimate in $/month for the app's project (with its databases) from the average load
    over the last `days` days. Errors silently leave None: this is an estimate, not billing."""
    projects = sorted({app["projectId"] for app in apps if app["projectId"]})
    if not token or not projects:
        return
    api = Railway(token)
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)

    def fetch(project):
        try:
            load = api.average_load(project, start.isoformat().replace("+00:00", "Z"),
                                    end.isoformat().replace("+00:00", "Z"), timeout=timeout)
            return project, round(estimate_cost(load), 2)
        except Exception:
            return project, None

    with ThreadPoolExecutor(max_workers=min(LIVE_WORKERS, len(projects))) as pool:
        costs = dict(pool.map(fetch, projects))
    for app in apps:
        if app["projectId"]:
            app["costUsd"] = costs.get(app["projectId"])


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, SO_REUSEADDR allows taking a port that another process is already listening on.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address, root, token=None):
        super().__init__(address, DashboardHandler)
        self.root = Path(root).resolve()
        self.token = token  # Railway token for live statuses; never sent to the browser
        port = self.server_address[1]
        # DNS rebinding protection: a foreign site that points its domain at 127.0.0.1 gets a 403.
        self.allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
        self.allowed_origins = {"http://" + host for host in self.allowed_hosts}
        # This run's key: the page sends it in the SECRET_HEADER header with every request to /api/access. A foreign
        # site does not know it, and a browser sends a request with a custom header only after a CORS preflight
        # (OPTIONS), which the server does not approve.
        self.secret = secrets.token_urlsafe(32)
        self.access_lock = threading.Lock()  # one request at a time changes small.json and the variables in Railway

    @property
    def url(self):
        return "http://127.0.0.1:%d/" % self.server_address[1]


def start_server(root, port=DEFAULT_PORT, token=None):
    """A server on 127.0.0.1:port; if the port is taken, on a random free one."""
    try:
        return DashboardServer(("127.0.0.1", port), root, token)
    except OSError:
        if port == 0:
            raise
        return DashboardServer(("127.0.0.1", 0), root, token)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "small-dashboard"
    timeout = 30  # seconds of silence from the browser: an idle connection does not hold a thread forever

    def do_GET(self):
        if self.headers.get("Host") not in self.server.allowed_hosts:
            return self._send(403, "text/plain; charset=utf-8", b"Forbidden")
        url = urlparse(self.path)
        path, params = url.path, {k: v[-1] for k, v in parse_qs(url.query).items()}
        if path == "/":
            # The page with the key must not be framed by a foreign site: it could put Revoke under a visible button.
            return self._send(200, "text/html; charset=utf-8", render_page(self.server.secret).encode("utf-8"),
                              {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'"})
        if path == "/open":
            return self._open(params.get("app"))
        if path == "/api/logs":
            return self._logs(params.get("app"), params.get("build") == "1")
        if path == "/api/access":
            return self._access_list(params.get("app"))
        if path == "/api/apps":
            apps = scan_apps(self.server.root)
            live = add_live_status(apps, self.server.token)
            if live == "on":
                add_usage(apps, self.server.token)
            payload = {"root": str(self.server.root),
                       "scannedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                       "live": live, "apps": apps}
            return self._send(200, "application/json; charset=utf-8",
                              json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def do_POST(self):
        """POST /api/access {"app": relPath, "action": "add" | "revoke", "who": email, @domain or name}."""
        length = self._content_length()
        # Read the body (at most MAX_BODY) before the checks: Windows aborts a socket closed with unread data
        # with an RST packet, and the browser would not even see the 403 response.
        body = self.rfile.read(length) if length is not None and length <= MAX_BODY else None
        if not self._trusted():
            return self._forbidden()
        if urlparse(self.path).path != "/api/access":
            return self._send(404, "text/plain; charset=utf-8", b"Not found")
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"error": tr("expected Content-Type: application/json",
                                                "нужен Content-Type: application/json")})
        if length is None:
            return self._json(411, {"error": tr("Content-Length is required", "нужен Content-Length")})
        if body is None:
            return self._json(413, {"error": tr("request is too large", "слишком большой запрос")})
        try:
            request = json.loads(body.decode("utf-8"))
        except ValueError:  # and UnicodeDecodeError
            request = None
        if not isinstance(request, dict) or request.get("action") not in ("add", "revoke") \
                or not isinstance(request.get("app"), str) or not isinstance(request.get("who"), str):
            return self._json(400, {"error": tr('expected JSON {"app": ..., "action": "add" or "revoke", "who": ...}',
                                                'ожидается JSON {"app": ..., "action": "add" или "revoke", '
                                                '"who": ...}')})
        self._access_change(request["app"], request["action"], request["who"].strip())

    def _content_length(self):
        """Body length from Content-Length; None if the header is missing or invalid, or the body is chunked."""
        if self.headers.get("Transfer-Encoding"):
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        return length if length >= 0 else None

    def _trusted(self):
        """The request comes from this dashboard's page: Host is ours (DNS rebinding), Origin is ours if the browser
        sent one (foreign site), the browser did not mark the request as cross-site, and SECRET_HEADER holds this
        run's key."""
        server = self.server
        if self.headers.get("Host") not in server.allowed_hosts:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in server.allowed_origins:
            return False
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
            return False
        given = self.headers.get(SECRET_HEADER, "").encode("utf-8", "replace")
        return hmac.compare_digest(given, server.secret.encode("ascii"))

    def _forbidden(self):
        # The server rejects its own page only if it was restarted: the page still has the old key.
        self._json(403, {"error": tr("forbidden: reload the page (the dashboard may have been restarted)",
                                     "запрещено: обновите страницу (дашборд мог быть перезапущен)")})

    def _access_app(self, rel_path):
        """(app, None) or (None, (code, response)). Only an app from the list (arbitrary paths on disk are not
        accepted) that is linked to Railway; a token is required, otherwise small.json and Railway would diverge."""
        app = self._find(rel_path)
        if not app or not app["serviceId"]:
            return None, (404, {"error": _not_deployed()})
        if not self.server.token:
            return None, (400, {"error": _need_token()})
        return app, None

    def _access_list(self, rel_path):
        """GET /api/access?app=relPath: who has access (addresses, domains and personal-link names), without tokens."""
        from .commands.access import list_access  # not at module top: commands imports this module

        if not self._trusted():
            return self._forbidden()
        app, error = self._access_app(rel_path)
        if error:
            return self._json(*error)
        warnings = []
        try:
            info = list_access(Path(app["path"]), warn=warnings.append)
        except ConfigError as e:
            return self._json(400, {"error": str(e)})
        names = []
        for name, _ in info["entries"]:
            if name not in names:
                names.append(name)  # repeated names and unnamed shared links are revoked together, as one row
        self._json(200, {
            "app": app["relPath"], "url": _app_url(app),
            "emails": [{"who": email, "domain": email.startswith("@")} for email in info["emails"]],
            "links": [{"who": name, "shared": not name} for name in names],
            "mailConfigured": info["mail"], "warnings": warnings,
        })

    def _access_change(self, rel_path, action, who):
        """Grants or revokes access like small access add / revoke: small.json, Railway, restart.
        A personal link reaches the browser only here, in the response to the add that created it."""
        from .commands.access import grant_access, revoke_access, who_error

        if not _valid_who(who, action):
            return self._json(400, {"error": str(who_error())})
        app, error = self._access_app(rel_path)
        if error:
            return self._json(*error)
        folder, warnings = Path(app["path"]), []
        try:
            with self.server.access_lock:
                change = grant_access if action == "add" else revoke_access
                result = change(folder, who, deploy=True, warn=warnings.append)
        except (CliError, ConfigError) as e:
            return self._json(400, {"error": str(e)})
        except RailwayError as e:
            return self._json(502, {"error": "Railway: %s" % e})
        except OSError as e:  # small.json was not written: no permission, the file is locked by another program
            return self._json(500, {"error": str(e)})
        payload = {"ok": True, "action": action, "kind": result["kind"], "who": result["who"],
                   "url": _app_url(app), "warnings": warnings}
        if action == "add" and result["kind"] == "link":
            # The domain is the one validated by describe_app, not taken as is from .small.json.
            payload["link"] = (access.invite_link(urlparse(app["url"]).netloc, result["token"])
                               if app["url"] else None)
        elif action == "add":
            payload["mailConfigured"] = result["mail"]
        self._json(200, payload)

    def _find(self, rel_path):
        """The app from the list by relPath; arbitrary paths on disk are not accepted."""
        return next((app for app in scan_apps(self.server.root) if app["relPath"] == rel_path), None)

    def _open(self, rel_path):
        """Redirect to the app via the invite link. The token goes only into the browser's address bar."""
        app = self._find(rel_path)
        if not app or not app["url"]:
            return self._send(404, "text/plain; charset=utf-8",
                              tr("App not found or not deployed", "Приложение не найдено или не задеплоено").encode("utf-8"))
        folder = Path(app["path"])
        try:
            entries, _ = access.current(folder, load_state(folder / STATE_FILE))
        except ConfigError:
            entries = []
        self.send_response(302)
        self.send_header("Location", access.invite_link(urlparse(app["url"]).netloc, access.pick(entries)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _logs(self, rel_path, build):
        app = self._find(rel_path)
        if not app or not app["serviceId"]:
            return self._json(404, {"error": _not_deployed()})
        if not self.server.token:
            return self._json(400, {"error": _need_token()})
        try:
            api = Railway(self.server.token)
            deployment = api.latest_deployment(app["serviceId"], app["environmentId"], timeout=LIVE_TIMEOUT)
            if not deployment:
                return self._json(200, {"deployment": None, "lines": []})
            entries = api.log_entries(deployment["id"], build=build, limit=LOG_LINES)
        except (RailwayError, KeyError, TypeError) as e:
            return self._json(502, {"error": "Railway: %s" % e})
        return self._json(200, {
            "deployment": {"id": deployment["id"], "status": deployment["status"]},
            "lines": [{"time": e["timestamp"], "message": e["message"]} for e in entries],
        })

    def _json(self, code, payload):
        self._send(code, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _send(self, code, content_type, body, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def _not_deployed():
    return tr("app not found or not deployed", "приложение не найдено или не задеплоено")


def _need_token():
    return tr("RAILWAY_API_TOKEN is required: restart small dashboard with it set",
              "нужен RAILWAY_API_TOKEN: перезапустите small dashboard с ним")


def _app_url(app):
    return app["url"] + "/" if app["url"] else None


def _valid_who(who, action):
    """An email, @domain or personal-link name, by the same rules as small access; "" is the unnamed shared link
    (it can only be revoked)."""
    if action == "revoke" and who == "":
        return True
    return len(who) <= MAX_WHO and (access.is_email(who.lower()) or bool(access.NAME_RE.match(who)))


def _page_texts():
    """Texts that the page's JS assembles, in the current language: they go into the page as the T object.

    {name} are substitutions for fmt() in JS; apps are the forms for plural(): three in Russian, two in English.
    """
    return {
        "locale": tr("en-US", "ru-RU"),  # for toLocaleString / toLocaleTimeString
        "linked": tr("Linked to Railway", "Привязан к Railway"),
        "notDeployed": tr("Not deployed", "Не задеплоен"),
        "deploy": {  # hint for the latest deploy's status; the keys are Railway API statuses
            "SUCCESS": tr("running", "работает"),
            "SLEEPING": tr("sleeping", "спит"),
            "QUEUED": tr("queued", "в очереди"),
            "WAITING": tr("waiting", "ожидает"),
            "INITIALIZING": tr("starting", "запускается"),
            "BUILDING": tr("building", "сборка"),
            "DEPLOYING": tr("deploying", "выкатывается"),
            "FAILED": tr("build failed", "сборка упала"),
            "CRASHED": tr("app crashed", "приложение упало"),
            "REMOVED": tr("removed", "удалён"),
            "REMOVING": tr("removing", "удаляется"),
            "SKIPPED": tr("skipped", "пропущен"),
            "NEEDS_APPROVAL": tr("awaiting approval", "ждёт подтверждения"),
        },
        "lastDeploy": tr("Latest deploy on Railway", "Последний деплой в Railway"),
        "sleepBadge": tr("auto-sleep", "засыпает"),
        "sleepHint": tr("Sleep mode: with no requests the app goes to sleep and uses no resources (small sleep)",
                        "Режим сна: без запросов приложение засыпает и не тратит ресурсы (small sleep)"),
        "notDeployedYet": tr("Not deployed yet: small deploy", "Ещё не деплоилось: small deploy"),
        "open": tr("Open via invite link", "Открыть по ссылке"),
        "openHint": tr("Opens right away with the access token, skipping the invite-only screen",
                       "Откроется сразу с токеном доступа — без экрана «Доступ по приглашению»"),
        "logs": tr("Logs", "Логи"),
        "needToken": tr("RAILWAY_API_TOKEN is required: restart small dashboard with it set",
                        "Нужен RAILWAY_API_TOKEN: перезапустите small dashboard с ним"),
        "costAtLoad": tr("≈ {cost}/mo at current load", "≈ {cost}/мес при текущей нагрузке"),
        "costHint": tr("Average load over the past week (CPU, memory, volumes; databases included) × Railway prices. "
                       "Excludes egress and the plan's free allowance — see the exact amount in Railway → Usage",
                       "Средняя нагрузка за неделю (CPU, память, тома; вместе с базами) × тарифы Railway. "
                       "Без трафика и бесплатного лимита тарифа — точная сумма в Railway → Usage"),
        "variables": tr("Variables:", "Переменные:"),
        "logsTitle": tr("Logs · {app}", "Логи · {app}"),
        "loading": tr("Loading…", "Загрузка…"),
        "noDeploys": tr("This service has no deploys yet", "У сервиса ещё нет деплоев"),
        "logsMeta": tr("Deploy {id} · {status} · lines: {count}", "Деплой {id} · {status} · строк: {count}"),
        "logEmpty": tr("Log is empty", "Лог пуст"),
        "logsFailed": tr("Couldn't load logs: {error}", "Не удалось получить лог: {error}"),
        "live": {
            "on": tr("deploy statuses from Railway", "статусы деплоя из Railway"),
            "off": tr("offline mode: set RAILWAY_API_TOKEN for live statuses",
                      "офлайн-режим: задайте RAILWAY_API_TOKEN для живых статусов"),
            "error": tr("Railway didn't respond, showing local data", "Railway не ответил, показаны локальные данные"),
        },
        "perMonth": tr("/mo", "/мес"),
        "apps": tr(["app", "apps"], ["приложение", "приложения", "приложений"]),
        "summary": tr("{count} {apps} · {linked} linked to Railway{total} · updated at {time}",
                      "{count} {apps} · {linked} привязано к Railway{total} · обновлено в {time}"),
        "summaryEmpty": tr("Updated at {time}", "Обновлено в {time}"),
        "listFailed": tr("Couldn't load the app list: {error}. Is small dashboard running?",
                         "Не удалось получить список: {error}. Запущен ли small dashboard?"),
        # Share dialog
        "share": tr("Share", "Поделиться"),
        "shareHint": tr("Give access by work email or with a personal link",
                        "Выдать доступ по рабочей почте или по личной ссылке"),
        "shareTitle": tr("Share · {app}", "Поделиться · {app}"),
        "accessFailed": tr("Couldn't load the access list: {error}", "Не удалось получить список доступа: {error}"),
        "nobody": tr("Nobody has access yet: the app is closed to everyone.",
                     "Доступа пока нет ни у кого — приложение закрыто для всех."),
        "kindEmail": tr("signs in with this email", "входит по этой почте"),
        "kindDomain": tr("everyone with an address in this domain", "все, у кого адрес в этом домене"),
        "kindLink": tr("personal link", "личная ссылка"),
        "sharedLink": tr("Shared link", "Общая ссылка"),
        "kindShared": tr("anyone who has it can open the app", "открыть приложение может любой, у кого она есть"),
        "revoke": tr("Revoke", "Отозвать"),
        "revokeLabel": tr("Revoke access for {who}", "Отозвать доступ у {who}"),
        "revokeShared": tr("Revoke the shared link", "Отозвать общую ссылку"),
        "confirmRevoke": tr("Revoke access for {who}? It stops working once the app restarts.",
                            "Отозвать доступ у {who}? Он закроется после перезапуска приложения."),
        "confirmRevokeShared": tr("Revoke the shared link? Everyone who has it loses access once the app restarts.",
                                  "Отозвать общую ссылку? Все, у кого она есть, потеряют доступ после перезапуска "
                                  "приложения."),
        "confirmClosed": tr("Nobody else has access: the app will be closed to everyone.",
                            "Больше доступа ни у кого нет — приложение закроется для всех."),
        "adding": tr("Adding…", "Добавляю…"),
        "revoking": tr("Revoking…", "Отзываю…"),
        "granted": tr("Access granted: {who}", "Доступ выдан: {who}"),
        "domainWho": tr("all {domain} addresses", "все адреса {domain}"),
        "linkFor": tr("Personal link for {who}", "Личная ссылка для {who}"),
        "linkPassword": tr("The link works like a password: send it to this person only. "
                           "It is shown only once, so copy it now.",
                           "Ссылка работает как пароль — пересылайте её только этому человеку. "
                           "Она показывается один раз: скопируйте её сейчас."),
        "linkLater": tr("The link will appear after small deploy: small link --as {who}",
                        "Ссылка появится после small deploy: small link --as {who}"),
        "copy": tr("Copy", "Копировать"),
        "copied": tr("Copied", "Скопировано"),
        "emailHow": tr("How to sign in: open {url} and sign in with this work address (with Google or Microsoft, "
                       "or with a link sent by email).",
                       "Как войти: открыть {url} и войти с этим рабочим адресом (через Google или Microsoft либо "
                       "по ссылке из письма)."),
        "noUrl": tr("the app URL (after small deploy)", "адрес приложения (после small deploy)"),
        "mailOff": tr("Nobody on the list can sign in yet: neither sign-in emails nor Google or Microsoft are set "
                      "up. Set up one in the terminal: small sso google, small sso microsoft or small mail resend",
                      "Войти из списка пока никто не может: не настроены ни письма для входа, ни Google или "
                      "Microsoft. Настройте что-то одно в терминале: small sso google, small sso microsoft или "
                      "small mail resend"),
        "revoked": tr("Access revoked: {who}", "Доступ отозван: {who}"),
        "restarting": tr("Railway is restarting the app: the change takes effect in a minute or two.",
                         "Railway перезапускает приложение — изменение вступит в силу через минуту-другую."),
    }


def render_page(secret=""):
    """The dashboard HTML in the current language: markup texts go into {{name}}, texts for JS as the T object.
    secret is the server run's key for requests to /api/access."""
    code = '<code class="font-mono">%s</code>'
    values = {
        "lang": lang(),
        "apps_in": tr("Micro-apps in", "Микро-приложения в"),
        "refresh": tr("Refresh", "Обновить"),
        "no_apps": tr("No apps found", "Приложений не найдено"),
        "no_app_folders": tr("There are no folders with %s or %s here.", "Здесь нет папок с %s или %s.")
                          % (code % "small.json", code % ".small.json"),
        "create_app": tr("Create an app: %s", "Создайте приложение: %s") % (code % "small init my-app"),
        "tab_app": tr("App", "Приложение"),
        "tab_build": tr("Build", "Сборка"),
        "close": tr("Close", "Закрыть"),
        "share_label": tr("Email, @domain or name", "Почта, @домен или имя"),
        "share_add": tr("Add", "Добавить"),
        "share_hint": tr("ivan@company.com: signs in with this email; @company.com: the whole domain; "
                         "bob: a personal link.",
                         "ivan@company.com — вход по этой почте, @company.com — весь домен, bob — личная ссылка."),
        "share_who_has": tr("Who has access", "У кого есть доступ"),
        # JSON inside <script>: escape "</" so that a "</script>" in a string does not close the tag too early.
        "texts": json.dumps(_page_texts(), ensure_ascii=False).replace("</", "<\\/"),
        "secret": json.dumps(secret),  # token_urlsafe: only Latin letters, digits, - and _
    }
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], PAGE_TEMPLATE)


# The page itself has no comments: it goes to the browser and is in English, so its notes live here.
# - #logs has no display class: a Tailwind class like "flex" would override the hidden attribute and the dialog
#   would show right away.
# - DEPLOY_STATUS is the badge tone for the latest deploy's live status from Railway; the status label is in T.deploy.
# - ARROW is inserted via insertAdjacentHTML: it is a constant, not user data.
# - A click on #logs or #logs-backdrop is a click on the dimmed background around the dialog: the dialog closes.
# - fmt() substitutes {name} in a single pass: substituted values (error messages) are not parsed again.
# - SECRET is this server run's key: api() sends it in the X-Small-Dashboard header. After small dashboard restarts
#   the key is different, and the server answers 403 "reload the page".
# - #share, like #logs, has no display class. The Share dialog closes only with × and Escape, not a click outside: a
#   stray click must not lose the personal link, which is shown only once. On close it is wiped from the page.
# - The access list (/api/access) has no tokens; a link arrives only in the response to the add that created it.
# - A response that arrives after the dialog was closed or reopened for another app is not shown (shareState.app).
PAGE_TEMPLATE = r"""<!doctype html>
<html lang="{{lang}}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>small dashboard</title>
<script src="https://cdn.tailwindcss.com/3.4.17"></script>
</head>
<body class="min-h-screen bg-stone-50 text-stone-900 antialiased dark:bg-stone-950 dark:text-stone-100">
<main class="mx-auto max-w-4xl px-4 py-10">
  <header class="mb-6 flex flex-wrap items-end justify-between gap-4">
    <div class="min-w-0">
      <h1 class="text-2xl font-semibold tracking-tight">small dashboard</h1>
      <p class="mt-1 truncate text-sm text-stone-500 dark:text-stone-400">
        {{apps_in}} <span id="root" class="font-mono"></span>
      </p>
    </div>
    <button id="refresh" type="button"
      class="inline-flex items-center gap-2 rounded-lg bg-stone-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-stone-700 disabled:cursor-wait disabled:opacity-60 dark:bg-stone-100 dark:text-stone-900 dark:hover:bg-white">
      <svg id="spinner" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/>
      </svg>
      {{refresh}}
    </button>
  </header>

  <p id="summary" class="mb-4 text-sm text-stone-500 dark:text-stone-400"></p>
  <ul id="apps" class="grid gap-3"></ul>

  <div id="empty" hidden class="rounded-xl border border-dashed border-stone-300 p-10 text-center dark:border-stone-700">
    <p class="font-medium">{{no_apps}}</p>
    <p class="mt-1 text-sm text-stone-500 dark:text-stone-400">
      {{no_app_folders}}
      {{create_app}}
    </p>
  </div>
  <div id="error" hidden class="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"></div>
</main>

<div id="logs" hidden class="fixed inset-0 z-10 bg-black/50">
 <div id="logs-backdrop" class="flex h-full items-center justify-center p-4">
  <div class="flex max-h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl bg-white shadow-xl dark:bg-stone-900">
    <div class="flex flex-wrap items-center gap-2 border-b border-stone-200 px-4 py-3 dark:border-stone-800">
      <h2 id="logs-title" class="mr-auto min-w-0 truncate font-semibold"></h2>
      <div class="inline-flex rounded-lg bg-stone-100 p-0.5 text-sm dark:bg-stone-800" role="tablist">
        <button type="button" data-build="0" class="logs-tab rounded-md px-3 py-1">{{tab_app}}</button>
        <button type="button" data-build="1" class="logs-tab rounded-md px-3 py-1">{{tab_build}}</button>
      </div>
      <button id="logs-refresh" type="button" class="rounded-lg px-3 py-1 text-sm hover:bg-stone-100 dark:hover:bg-stone-800">{{refresh}}</button>
      <button id="logs-close" type="button" aria-label="{{close}}" class="rounded-lg px-2 py-1 text-lg leading-none hover:bg-stone-100 dark:hover:bg-stone-800">×</button>
    </div>
    <p id="logs-meta" class="px-4 pt-2 text-xs text-stone-500 dark:text-stone-400"></p>
    <pre id="logs-body" class="m-0 flex-1 overflow-auto whitespace-pre-wrap break-all px-4 py-3 font-mono text-xs leading-relaxed"></pre>
  </div>
 </div>
</div>

<div id="share" hidden class="fixed inset-0 z-10 bg-black/50">
 <div class="flex h-full items-center justify-center p-4">
  <div role="dialog" aria-modal="true" aria-labelledby="share-title"
    class="flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden rounded-xl bg-white shadow-xl dark:bg-stone-900">
    <div class="flex items-center gap-2 border-b border-stone-200 px-4 py-3 dark:border-stone-800">
      <h2 id="share-title" class="mr-auto min-w-0 truncate font-semibold"></h2>
      <button id="share-close" type="button" aria-label="{{close}}" class="rounded-lg px-2 py-1 text-lg leading-none hover:bg-stone-100 dark:hover:bg-stone-800">×</button>
    </div>
    <div class="overflow-auto px-4 py-4">
      <form id="share-form">
        <label for="share-who" class="text-sm font-medium">{{share_label}}</label>
        <div class="mt-1.5 flex gap-2">
          <input id="share-who" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="320"
            placeholder="ivan@company.com" aria-describedby="share-hint"
            class="min-w-0 flex-1 rounded-lg border border-stone-300 bg-white px-3 py-1.5 text-sm outline-none focus:border-stone-500 focus:ring-2 focus:ring-stone-300 disabled:opacity-60 dark:border-stone-700 dark:bg-stone-950 dark:focus:ring-stone-700">
          <button id="share-add" type="submit" class="inline-flex items-center rounded-lg bg-stone-900 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-stone-700 disabled:cursor-wait disabled:opacity-60 dark:bg-stone-100 dark:text-stone-900 dark:hover:bg-white">{{share_add}}</button>
        </div>
        <p id="share-hint" class="mt-1.5 text-xs text-stone-500 dark:text-stone-400">{{share_hint}}</p>
      </form>
      <div id="share-error" hidden role="alert" class="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"></div>
      <div id="share-result" hidden role="status" class="mt-3 rounded-lg border border-stone-200 bg-stone-50 px-3 py-3 text-sm dark:border-stone-800 dark:bg-stone-950"></div>
      <h3 class="mt-5 text-sm font-semibold">{{share_who_has}}</h3>
      <p id="share-status" class="mt-1 text-xs text-stone-500 dark:text-stone-400"></p>
      <ul id="share-list" class="mt-1 divide-y divide-stone-200 dark:divide-stone-800"></ul>
      <div id="share-notes"></div>
    </div>
  </div>
 </div>
</div>

<script>
const T = {{texts}};
const SECRET = {{secret}};
const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function fmt(template, values) {
  return template.replace(/\{(\w+)\}/g, (match, key) => (key in values ? String(values[key]) : match));
}

function plural(n, forms) {
  if (forms.length < 3) return n === 1 ? forms[0] : forms[1];
  const [one, few, many] = forms;
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

const STATUS = {
  linked: [T.linked, "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-400/30", "bg-emerald-500"],
  not_deployed: [T.notDeployed, "bg-stone-100 text-stone-600 ring-stone-500/20 dark:bg-stone-800 dark:text-stone-300 dark:ring-stone-400/20", "bg-stone-400"],
};

const DEPLOY_TONES = {
  ok: ["bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-400/30", "bg-emerald-500"],
  busy: ["bg-amber-50 text-amber-800 ring-amber-600/20 dark:bg-amber-950 dark:text-amber-300 dark:ring-amber-400/30", "bg-amber-500 animate-pulse"],
  bad: ["bg-red-50 text-red-700 ring-red-600/20 dark:bg-red-950 dark:text-red-300 dark:ring-red-400/30", "bg-red-500"],
  idle: ["bg-sky-50 text-sky-700 ring-sky-600/20 dark:bg-sky-950 dark:text-sky-300 dark:ring-sky-400/30", "bg-sky-500"],
  other: ["bg-stone-100 text-stone-600 ring-stone-500/20 dark:bg-stone-800 dark:text-stone-300 dark:ring-stone-400/20", "bg-stone-400"],
};
const DEPLOY_STATUS = {
  SUCCESS: "ok", SLEEPING: "idle",
  QUEUED: "busy", WAITING: "busy", INITIALIZING: "busy", BUILDING: "busy", DEPLOYING: "busy",
  FAILED: "bad", CRASHED: "bad",
  REMOVED: "other", REMOVING: "other", SKIPPED: "other", NEEDS_APPROVAL: "other",
};

function deployBadge(deployment) {
  const tone = DEPLOY_STATUS[deployment.status] || "other";
  const hint = T.deploy[deployment.status] || "";
  const [badgeClass, dotClass] = DEPLOY_TONES[tone];
  const badge = el("span", "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 font-mono text-xs font-medium ring-1 ring-inset " + badgeClass);
  badge.append(el("span", "h-1.5 w-1.5 rounded-full " + dotClass), document.createTextNode(deployment.status));
  const when = deployment.createdAt ? new Date(deployment.createdAt).toLocaleString(T.locale) : "";
  badge.title = T.lastDeploy + (hint ? ": " + hint : "") + (when ? ", " + when : "");
  return badge;
}

const ARROW ='<svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17 17 7"/><path d="M8 7h9v9"/></svg>';

function link(href, text) {
  const a = el("a", "inline-flex items-center gap-1 font-medium text-blue-600 hover:underline dark:text-blue-400", text);
  a.insertAdjacentHTML("beforeend", ARROW);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  return a;
}

function card(app) {
  const li = el("li", "rounded-xl border border-stone-200 bg-white p-5 shadow-sm dark:border-stone-800 dark:bg-stone-900");
  const head = el("div", "flex flex-wrap items-center justify-between gap-2");
  head.append(el("h2", "text-base font-semibold", app.name));
  const [label, badgeClass, dotClass] = STATUS[app.status] || STATUS.not_deployed;
  const badge = el("span", "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset " + badgeClass);
  badge.append(el("span", "h-1.5 w-1.5 rounded-full " + dotClass), document.createTextNode(label));
  const badges = el("div", "flex flex-wrap items-center gap-2");
  if (app.sleep) {
    const sleep = el("span", "inline-flex items-center rounded-full bg-sky-50 px-2.5 py-0.5 text-xs font-medium text-sky-700 " +
      "ring-1 ring-inset ring-sky-600/20 dark:bg-sky-950 dark:text-sky-300 dark:ring-sky-400/30", T.sleepBadge);
    sleep.title = T.sleepHint;
    badges.append(sleep);
  }
  if (app.deployment) badges.append(deployBadge(app.deployment));
  badges.append(badge);
  head.append(badges);
  li.append(head);

  const links = el("div", "mt-3 flex flex-wrap gap-x-5 gap-y-1 text-sm");
  if (app.url) links.append(link(app.url, app.url.replace("https://", "")));
  if (app.railwayUrl) links.append(link(app.railwayUrl, "Railway"));
  if (!app.url && !app.railwayUrl) links.append(el("span", "text-stone-500 dark:text-stone-400", T.notDeployedYet));
  li.append(links);

  if (app.url || app.serviceId) {
    const actions = el("div", "mt-3 flex flex-wrap gap-2");
    if (app.serviceId) {
      const share = el("button", PRIMARY, T.share);
      share.type = "button";
      share.title = T.shareHint;
      if (liveMode === "off") {
        share.disabled = true;
        share.title = T.needToken;
      }
      share.addEventListener("click", () => openShare(app, share));
      actions.append(share);
    }
    if (app.url) {
      const open = el("a", BUTTON, T.open);
      open.href = "/open?app=" + encodeURIComponent(app.relPath);
      open.target = "_blank";
      open.rel = "noopener";
      open.title = T.openHint;
      actions.append(open);
    }
    if (app.serviceId) {
      const logs = el("button", BUTTON, T.logs);
      logs.type = "button";
      if (liveMode === "off") {
        logs.disabled = true;
        logs.title = T.needToken;
      }
      logs.addEventListener("click", () => openLogs(app));
      actions.append(logs);
    }
    li.append(actions);
  }

  if (app.costUsd !== null && app.costUsd !== undefined) {
    const cost = el("p", "mt-3 text-xs text-stone-500 dark:text-stone-400", fmt(T.costAtLoad, { cost: usd(app.costUsd) }));
    cost.title = T.costHint;
    li.append(cost);
  }
  li.append(el("p", "mt-3 break-all font-mono text-xs text-stone-500 dark:text-stone-400", app.path));

  if (app.envNames.length) {
    const env = el("div", "mt-3 flex flex-wrap items-center gap-1.5");
    env.append(el("span", "text-xs text-stone-500 dark:text-stone-400", T.variables));
    for (const name of app.envNames) {
      env.append(el("span", "rounded bg-stone-100 px-1.5 py-0.5 font-mono text-xs dark:bg-stone-800", name));
    }
    li.append(env);
  }
  for (const error of app.errors) {
    li.append(el("p", "mt-3 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-300", "⚠ " + error));
  }
  return li;
}

function usd(value) {
  return value < 0.01 ? "< $0.01" : "$" + value.toFixed(2);
}

const BUTTON = "inline-flex items-center rounded-lg border border-stone-300 px-3 py-1.5 text-sm font-medium transition " +
  "hover:bg-stone-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-stone-700 dark:hover:bg-stone-800";
const PRIMARY = "inline-flex items-center rounded-lg border border-stone-900 bg-stone-900 px-3 py-1.5 text-sm font-medium " +
  "text-white transition hover:bg-stone-700 disabled:cursor-not-allowed disabled:opacity-50 " +
  "dark:border-stone-100 dark:bg-stone-100 dark:text-stone-900 dark:hover:bg-white";
const REVOKE = "shrink-0 rounded-lg px-2.5 py-1 text-sm font-medium text-red-700 transition hover:bg-red-50 " +
  "disabled:cursor-not-allowed disabled:opacity-50 dark:text-red-400 dark:hover:bg-red-950";
const NOTE = "mt-2 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-300";
let liveMode = "off";
const logsState = { app: null, build: false };

function openLogs(app) {
  logsState.app = app;
  logsState.build = false;
  $("logs-title").textContent = fmt(T.logsTitle, { app: app.name });
  $("logs").hidden = false;
  loadLogs();
}

async function loadLogs() {
  const { app, build } = logsState;
  for (const tab of document.querySelectorAll(".logs-tab")) {
    const active = (tab.dataset.build === "1") === build;
    tab.className = "logs-tab rounded-md px-3 py-1 " + (active ? "bg-white shadow-sm dark:bg-stone-700" : "text-stone-500 dark:text-stone-400");
  }
  $("logs-meta").textContent = T.loading;
  $("logs-body").textContent = "";
  try {
    const res = await fetch("/api/logs?app=" + encodeURIComponent(app.relPath) + "&build=" + (build ? 1 : 0), { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
    if (!data.deployment) {
      $("logs-meta").textContent = T.noDeploys;
      return;
    }
    $("logs-meta").textContent = fmt(T.logsMeta,
      { id: data.deployment.id, status: data.deployment.status, count: data.lines.length });
    $("logs-body").textContent = data.lines.length
      ? data.lines.map((l) => new Date(l.time).toLocaleTimeString(T.locale) + "  " + l.message).join("\n")
      : T.logEmpty;
    $("logs-body").scrollTop = $("logs-body").scrollHeight;
  } catch (e) {
    $("logs-meta").textContent = fmt(T.logsFailed, { error: e.message });
  }
}

for (const tab of document.querySelectorAll(".logs-tab")) {
  tab.addEventListener("click", () => { logsState.build = tab.dataset.build === "1"; loadLogs(); });
}
$("logs-refresh").addEventListener("click", loadLogs);
$("logs-close").addEventListener("click", () => { $("logs").hidden = true; });
$("logs").addEventListener("click", (e) => {
  if (e.target === $("logs") || e.target === $("logs-backdrop")) $("logs").hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  $("logs").hidden = true;
  closeShare();
});

async function api(path, payload) {
  const options = { cache: "no-store", headers: { "X-Small-Dashboard": SECRET } };
  if (payload !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
  return data;
}

function fmtNodes(template, values) {
  return template.split(/(\{\w+\})/).filter(Boolean).map((part) => {
    const key = part.slice(1, -1);
    if (!/^\{\w+\}$/.test(part) || !(key in values)) return document.createTextNode(part);
    const value = values[key];
    return value instanceof Node ? value : document.createTextNode(String(value));
  });
}

const shareState = { app: null, opener: null, busy: false, mailNoted: false };

function openShare(app, opener) {
  shareState.app = app;
  shareState.opener = opener;
  shareState.mailNoted = false;
  $("share-title").textContent = fmt(T.shareTitle, { app: app.name });
  $("share-who").value = "";
  showShareError(null);
  $("share-result").replaceChildren();
  $("share-result").hidden = true;
  $("share").hidden = false;
  $("share-who").focus();
  loadAccess();
}

function closeShare() {
  if ($("share").hidden) return;
  $("share").hidden = true;
  $("share-result").replaceChildren();
  $("share-result").hidden = true;
  shareState.app = null;
  if (shareState.opener) shareState.opener.focus();
}

function showShareError(message) {
  $("share-error").textContent = message ? message.charAt(0).toUpperCase() + message.slice(1) : "";
  $("share-error").hidden = !message;
}

function setShareBusy(busy) {
  shareState.busy = busy;
  $("share-add").disabled = busy;
  for (const button of $("share-list").querySelectorAll("button")) button.disabled = busy;
}

async function loadAccess() {
  const app = shareState.app;
  $("share-status").textContent = T.loading;
  $("share-list").replaceChildren();
  $("share-notes").replaceChildren();
  try {
    const data = await api("/api/access?app=" + encodeURIComponent(app.relPath));
    if (shareState.app === app) renderAccess(data);
  } catch (e) {
    if (shareState.app === app) $("share-status").textContent = fmt(T.accessFailed, { error: e.message });
  }
}

function renderAccess(data) {
  const rows = [
    ...data.emails.map((e) => ({ who: e.who, label: e.who, kind: e.domain ? T.kindDomain : T.kindEmail })),
    ...data.links.map((l) => l.shared
      ? { who: "", label: T.sharedLink, kind: T.kindShared, shared: true }
      : { who: l.who, label: l.who, kind: T.kindLink }),
  ];
  $("share-status").textContent = rows.length ? "" : T.nobody;
  $("share-list").replaceChildren(...rows.map((row) => accessRow(row, rows.length)));
  const notes = [...data.warnings];
  if (data.emails.length && !data.mailConfigured && !shareState.mailNoted) notes.push(T.mailOff);
  $("share-notes").replaceChildren(...notes.map((text) => el("p", NOTE, "⚠ " + text)));
  setShareBusy(shareState.busy);
}

function accessRow(row, total) {
  const li = el("li", "flex items-center gap-3 py-2");
  const text = el("div", "min-w-0 flex-1");
  text.append(el("p", "truncate text-sm font-medium" + (row.shared ? " italic" : ""), row.label),
    el("p", "text-xs text-stone-500 dark:text-stone-400", row.kind));
  const revoke = el("button", REVOKE, T.revoke);
  revoke.type = "button";
  revoke.setAttribute("aria-label", row.shared ? T.revokeShared : fmt(T.revokeLabel, { who: row.label }));
  revoke.addEventListener("click", () => {
    let question = row.shared ? T.confirmRevokeShared : fmt(T.confirmRevoke, { who: row.label });
    if (total === 1) question += " " + T.confirmClosed;
    if (confirm(question)) changeAccess({ action: "revoke", who: row.who }, revoke, T.revoking);
  });
  li.append(text, revoke);
  return li;
}

async function changeAccess(request, button, busyText) {
  const app = shareState.app;
  if (!app || shareState.busy) return;
  const label = button.textContent;
  button.textContent = busyText;
  setShareBusy(true);
  showShareError(null);
  try {
    const result = await api("/api/access", { app: app.relPath, ...request });
    if (shareState.app !== app) return;
    showResult(result);
    if (request.action === "add") $("share-who").value = "";
    loadAccess();
  } catch (e) {
    if (shareState.app === app) showShareError(e.message);
  } finally {
    button.textContent = label;
    setShareBusy(false);
    if (shareState.app === app) ($("share-copy") || $("share-who")).focus();
  }
}

function showResult(result) {
  const parts = [];
  shareState.mailNoted = result.action === "add" && result.kind !== "link" && !result.mailConfigured;
  const done = (text) => el("p", "font-medium text-emerald-700 dark:text-emerald-400", "✓ " + text);
  if (result.action === "revoke") {
    parts.push(done(fmt(T.revoked, { who: result.kind === "link" && !result.who ? T.sharedLink : result.who })));
  } else if (result.kind === "link") {
    parts.push(done(fmt(T.granted, { who: result.who })));
    if (result.link) {
      const row = el("div", "mt-2 flex gap-2");
      const input = el("input", "min-w-0 flex-1 rounded-lg border border-stone-300 bg-white px-3 py-1.5 font-mono text-xs " +
        "dark:border-stone-700 dark:bg-stone-900");
      input.readOnly = true;
      input.value = result.link;
      input.setAttribute("aria-label", fmt(T.linkFor, { who: result.who }));
      input.addEventListener("focus", () => input.select());
      const copy = el("button", BUTTON, T.copy);
      copy.id = "share-copy";
      copy.type = "button";
      copy.addEventListener("click", () => copyLink(input, copy));
      row.append(input, copy);
      parts.push(row, el("p", NOTE, "⚠ " + T.linkPassword));
    } else {
      parts.push(el("p", "mt-1", fmt(T.linkLater, { who: result.who })));
    }
  } else {
    const who = result.kind === "domain" ? fmt(T.domainWho, { domain: result.who }) : result.who;
    parts.push(done(fmt(T.granted, { who })));
    const how = el("p", "mt-1");
    how.append(...fmtNodes(T.emailHow, { url: result.url ? link(result.url, result.url) : T.noUrl }));
    parts.push(how);
    if (shareState.mailNoted) parts.push(el("p", NOTE, "⚠ " + T.mailOff));
  }
  parts.push(el("p", "mt-2 text-xs text-stone-500 dark:text-stone-400", T.restarting));
  for (const warning of result.warnings) parts.push(el("p", NOTE, "⚠ " + warning));
  $("share-result").replaceChildren(...parts);
  $("share-result").hidden = false;
}

async function copyLink(input, button) {
  try {
    await navigator.clipboard.writeText(input.value);
  } catch (e) {
    input.select();
    document.execCommand("copy");
  }
  button.textContent = T.copied;
  setTimeout(() => { button.textContent = T.copy; }, 2000);
}

$("share-form").addEventListener("submit", (e) => {
  e.preventDefault();
  changeAccess({ action: "add", who: $("share-who").value.trim() }, $("share-add"), T.adding);
});
$("share-close").addEventListener("click", closeShare);

async function refresh() {
  const button = $("refresh");
  button.disabled = true;
  $("spinner").classList.add("animate-spin");
  try {
    const res = await fetch("/api/apps", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    liveMode = data.live;
    $("root").textContent = data.root;
    $("apps").replaceChildren(...data.apps.map(card));
    $("empty").hidden = data.apps.length > 0;
    $("error").hidden = true;
    const n = data.apps.length;
    const linked = data.apps.filter((a) => a.status === "linked").length;
    const time = new Date(data.scannedAt).toLocaleTimeString(T.locale);
    const live = T.live[data.live] || "";
    const costs = [...new Map(data.apps.filter((a) => a.costUsd != null).map((a) => [a.projectId, a.costUsd])).values()];
    const total = costs.length ? " · ≈ " + usd(costs.reduce((x, y) => x + y, 0)) + T.perMonth : "";
    $("summary").textContent = (n
      ? fmt(T.summary, { count: n, apps: plural(n, T.apps), linked, total, time })
      : fmt(T.summaryEmpty, { time })) + (linked && live ? " · " + live : "");
  } catch (e) {
    $("error").textContent = fmt(T.listFailed, { error: e.message });
    $("error").hidden = false;
  } finally {
    button.disabled = false;
    $("spinner").classList.remove("animate-spin");
  }
}

$("refresh").addEventListener("click", refresh);
refresh();
</script>
</body>
</html>
"""
