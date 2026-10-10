"""`small dashboard`: локальная страница со списком микро-приложений в папке.

Приложением считается папка, где есть small.json и/или .small.json. Привязку и ссылки берём из локальных
файлов. Если задан RAILWAY_API_TOKEN, для привязанных приложений дополнительно спрашиваем у Railway статус
последнего деплоя; любая ошибка (сеть, таймаут, токен) просто оставляет локальные данные.
Значения переменных из small.json и сам токен Railway никогда не отдаются в браузер.
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import access
from .archive import ALWAYS_IGNORED
from .config import CONFIG_FILE, STATE_FILE, ConfigError, load_config, load_state
from .railway import Railway, RailwayError

DEFAULT_PORT = 3000
MAX_DEPTH = 6  # глубже не ищем: `small dashboard` из корня диска не должен обходить весь диск
DOMAIN_RE = re.compile(r"[A-Za-z0-9.-]+\Z")
RAILWAY_ID_RE = re.compile(r"[A-Za-z0-9-]+\Z")
DEPLOY_STATUS_RE = re.compile(r"[A-Z_]{1,32}\Z")
LIVE_TIMEOUT = 5      # секунд на запрос к Railway: кнопка «Обновить» не должна зависать
LIVE_WORKERS = 8
LOG_LINES = 200
LOAD_DAYS = 7
# Тарифы Railway за месяц (docs.railway.com/reference/pricing/plans) на единицу средней нагрузки из metrics.
MONTHLY_PRICES_USD = {
    "CPU_USAGE": 20.0,         # $20 за vCPU, загруженный весь месяц
    "MEMORY_USAGE_GB": 10.0,   # $10 за ГБ памяти весь месяц
    "DISK_USAGE_GB": 0.15,     # $0.15 за ГБ тома весь месяц
}


def scan_apps(root, max_depth=MAX_DEPTH):
    """Находит приложения под root. Внутрь найденного приложения не спускается."""
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
    app["envNames"] = sorted(config["env"])  # только имена: значения — секреты

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
                app["errors"].append("%s: некорректный домен %r" % (STATE_FILE, domain))
    return app


def _is_id(value):
    return isinstance(value, str) and bool(RAILWAY_ID_RE.match(value))


def add_live_status(apps, token, timeout=LIVE_TIMEOUT):
    """Дописывает app["deployment"] = {"status", "createdAt"} из Railway для привязанных приложений.

    Возвращает режим для страницы: "off" (нет токена), "on" (Railway ответил) или "error" (не ответил ни разу).
    Никогда не бросает исключений: живой статус — украшение, локальные данные важнее.
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
        except Exception:  # сеть, таймаут, неверный токен, удалённый сервис, неожиданный ответ
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
    """$ в месяц при средней нагрузке load (ядра, ГБ), по тарифам Railway; без исходящего трафика
    и бесплатного лимита тарифа ($5 Hobby / $20 Pro)."""
    return sum(MONTHLY_PRICES_USD[m] * float(v) for m, v in load.items() if m in MONTHLY_PRICES_USD)


def add_usage(apps, token, timeout=LIVE_TIMEOUT, days=LOAD_DAYS):
    """Дописывает app["costUsd"] — оценку $/мес для проекта приложения (с его базами) по средней нагрузке
    за последние days дней. Ошибки молча оставляют None: это оценка, а не биллинг."""
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
    # В Windows SO_REUSEADDR разрешает занять порт, который уже слушает другой процесс.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address, root, token=None):
        super().__init__(address, DashboardHandler)
        self.root = Path(root).resolve()
        self.token = token  # токен Railway для живых статусов; в браузер не уходит
        port = self.server_address[1]
        # Защита от DNS rebinding: чужой сайт, указавший свой домен на 127.0.0.1, получит 403.
        self.allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}

    @property
    def url(self):
        return "http://127.0.0.1:%d/" % self.server_address[1]


def start_server(root, port=DEFAULT_PORT, token=None):
    """Сервер на 127.0.0.1:port; если порт занят — на свободном случайном."""
    try:
        return DashboardServer(("127.0.0.1", port), root, token)
    except OSError:
        if port == 0:
            raise
        return DashboardServer(("127.0.0.1", 0), root, token)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "small-dashboard"

    def do_GET(self):
        if self.headers.get("Host") not in self.server.allowed_hosts:
            return self._send(403, "text/plain; charset=utf-8", b"Forbidden")
        url = urlparse(self.path)
        path, params = url.path, {k: v[-1] for k, v in parse_qs(url.query).items()}
        if path == "/":
            return self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        if path == "/open":
            return self._open(params.get("app"))
        if path == "/api/logs":
            return self._logs(params.get("app"), params.get("build") == "1")
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

    def _find(self, rel_path):
        """Приложение из списка по relPath — произвольные пути с диска не принимаются."""
        return next((app for app in scan_apps(self.server.root) if app["relPath"] == rel_path), None)

    def _open(self, rel_path):
        """Редирект на приложение по ссылке-приглашению. Токен уходит только в адресную строку браузера."""
        app = self._find(rel_path)
        if not app or not app["url"]:
            return self._send(404, "text/plain; charset=utf-8", "Приложение не найдено или не задеплоено".encode("utf-8"))
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
            return self._json(404, {"error": "приложение не найдено или не задеплоено"})
        if not self.server.token:
            return self._json(400, {"error": "нужен RAILWAY_API_TOKEN: перезапустите small dashboard с ним"})
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

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


PAGE = r"""<!doctype html>
<html lang="ru">
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
        Микро-приложения в <span id="root" class="font-mono"></span>
      </p>
    </div>
    <button id="refresh" type="button"
      class="inline-flex items-center gap-2 rounded-lg bg-stone-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-stone-700 disabled:cursor-wait disabled:opacity-60 dark:bg-stone-100 dark:text-stone-900 dark:hover:bg-white">
      <svg id="spinner" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/>
      </svg>
      Обновить
    </button>
  </header>

  <p id="summary" class="mb-4 text-sm text-stone-500 dark:text-stone-400"></p>
  <ul id="apps" class="grid gap-3"></ul>

  <div id="empty" hidden class="rounded-xl border border-dashed border-stone-300 p-10 text-center dark:border-stone-700">
    <p class="font-medium">Приложений не найдено</p>
    <p class="mt-1 text-sm text-stone-500 dark:text-stone-400">
      Здесь нет папок с <code class="font-mono">small.json</code> или <code class="font-mono">.small.json</code>.
      Создайте приложение: <code class="font-mono">small init my-app</code>
    </p>
  </div>
  <div id="error" hidden class="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300"></div>
</main>

<!-- У #logs нет display-класса: Tailwind-класс вроде "flex" перебил бы атрибут hidden, и окно было бы видно сразу. -->
<div id="logs" hidden class="fixed inset-0 z-10 bg-black/50">
 <div id="logs-backdrop" class="flex h-full items-center justify-center p-4">
  <div class="flex max-h-[85vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl bg-white shadow-xl dark:bg-stone-900">
    <div class="flex flex-wrap items-center gap-2 border-b border-stone-200 px-4 py-3 dark:border-stone-800">
      <h2 id="logs-title" class="mr-auto min-w-0 truncate font-semibold"></h2>
      <div class="inline-flex rounded-lg bg-stone-100 p-0.5 text-sm dark:bg-stone-800" role="tablist">
        <button type="button" data-build="0" class="logs-tab rounded-md px-3 py-1">Приложение</button>
        <button type="button" data-build="1" class="logs-tab rounded-md px-3 py-1">Сборка</button>
      </div>
      <button id="logs-refresh" type="button" class="rounded-lg px-3 py-1 text-sm hover:bg-stone-100 dark:hover:bg-stone-800">Обновить</button>
      <button id="logs-close" type="button" aria-label="Закрыть" class="rounded-lg px-2 py-1 text-lg leading-none hover:bg-stone-100 dark:hover:bg-stone-800">×</button>
    </div>
    <p id="logs-meta" class="px-4 pt-2 text-xs text-stone-500 dark:text-stone-400"></p>
    <pre id="logs-body" class="m-0 flex-1 overflow-auto whitespace-pre-wrap break-all px-4 py-3 font-mono text-xs leading-relaxed"></pre>
  </div>
 </div>
</div>

<script>
const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

const STATUS = {
  linked: ["Привязан к Railway", "bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-400/30", "bg-emerald-500"],
  not_deployed: ["Не задеплоен", "bg-stone-100 text-stone-600 ring-stone-500/20 dark:bg-stone-800 dark:text-stone-300 dark:ring-stone-400/20", "bg-stone-400"],
};

// Живой статус последнего деплоя из Railway: [подпись, классы бейджа, классы точки].
const DEPLOY_TONES = {
  ok: ["bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-400/30", "bg-emerald-500"],
  busy: ["bg-amber-50 text-amber-800 ring-amber-600/20 dark:bg-amber-950 dark:text-amber-300 dark:ring-amber-400/30", "bg-amber-500 animate-pulse"],
  bad: ["bg-red-50 text-red-700 ring-red-600/20 dark:bg-red-950 dark:text-red-300 dark:ring-red-400/30", "bg-red-500"],
  idle: ["bg-sky-50 text-sky-700 ring-sky-600/20 dark:bg-sky-950 dark:text-sky-300 dark:ring-sky-400/30", "bg-sky-500"],
  other: ["bg-stone-100 text-stone-600 ring-stone-500/20 dark:bg-stone-800 dark:text-stone-300 dark:ring-stone-400/20", "bg-stone-400"],
};
const DEPLOY_STATUS = {
  SUCCESS: ["ok", "работает"], SLEEPING: ["idle", "спит"],
  QUEUED: ["busy", "в очереди"], WAITING: ["busy", "ожидает"], INITIALIZING: ["busy", "запускается"],
  BUILDING: ["busy", "сборка"], DEPLOYING: ["busy", "выкатывается"],
  FAILED: ["bad", "сборка упала"], CRASHED: ["bad", "приложение упало"],
  REMOVED: ["other", "удалён"], REMOVING: ["other", "удаляется"], SKIPPED: ["other", "пропущен"],
  NEEDS_APPROVAL: ["other", "ждёт подтверждения"],
};

function deployBadge(deployment) {
  const [tone, hint] = DEPLOY_STATUS[deployment.status] || ["other", ""];
  const [badgeClass, dotClass] = DEPLOY_TONES[tone];
  const badge = el("span", "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 font-mono text-xs font-medium ring-1 ring-inset " + badgeClass);
  badge.append(el("span", "h-1.5 w-1.5 rounded-full " + dotClass), document.createTextNode(deployment.status));
  const when = deployment.createdAt ? new Date(deployment.createdAt).toLocaleString("ru-RU") : "";
  badge.title = "Последний деплой в Railway" + (hint ? ": " + hint : "") + (when ? ", " + when : "");
  return badge;
}

const ARROW ='<svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17 17 7"/><path d="M8 7h9v9"/></svg>';

function link(href, text) {
  const a = el("a", "inline-flex items-center gap-1 font-medium text-blue-600 hover:underline dark:text-blue-400", text);
  a.insertAdjacentHTML("beforeend", ARROW);  // константа, не данные пользователя
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
      "ring-1 ring-inset ring-sky-600/20 dark:bg-sky-950 dark:text-sky-300 dark:ring-sky-400/30", "засыпает");
    sleep.title = "Режим сна: без запросов приложение засыпает и не тратит ресурсы (small sleep)";
    badges.append(sleep);
  }
  if (app.deployment) badges.append(deployBadge(app.deployment));
  badges.append(badge);
  head.append(badges);
  li.append(head);

  const links = el("div", "mt-3 flex flex-wrap gap-x-5 gap-y-1 text-sm");
  if (app.url) links.append(link(app.url, app.url.replace("https://", "")));
  if (app.railwayUrl) links.append(link(app.railwayUrl, "Railway"));
  if (!app.url && !app.railwayUrl) links.append(el("span", "text-stone-500 dark:text-stone-400", "Ещё не деплоилось: small deploy"));
  li.append(links);

  if (app.url || app.serviceId) {
    const actions = el("div", "mt-3 flex flex-wrap gap-2");
    if (app.url) {
      const open = el("a", BUTTON, "Открыть по ссылке");
      open.href = "/open?app=" + encodeURIComponent(app.relPath);
      open.target = "_blank";
      open.rel = "noopener";
      open.title = "Откроется сразу с токеном доступа — без экрана «Доступ по приглашению»";
      actions.append(open);
    }
    if (app.serviceId) {
      const logs = el("button", BUTTON, "Логи");
      logs.type = "button";
      if (liveMode === "off") {
        logs.disabled = true;
        logs.title = "Нужен RAILWAY_API_TOKEN: перезапустите small dashboard с ним";
      }
      logs.addEventListener("click", () => openLogs(app));
      actions.append(logs);
    }
    li.append(actions);
  }

  if (app.costUsd !== null && app.costUsd !== undefined) {
    const cost = el("p", "mt-3 text-xs text-stone-500 dark:text-stone-400", "≈ " + usd(app.costUsd) + "/мес при текущей нагрузке");
    cost.title = "Средняя нагрузка за неделю (CPU, память, тома; вместе с базами) × тарифы Railway. " +
      "Без трафика и бесплатного лимита тарифа — точная сумма в Railway → Usage";
    li.append(cost);
  }
  li.append(el("p", "mt-3 break-all font-mono text-xs text-stone-500 dark:text-stone-400", app.path));

  if (app.envNames.length) {
    const env = el("div", "mt-3 flex flex-wrap items-center gap-1.5");
    env.append(el("span", "text-xs text-stone-500 dark:text-stone-400", "Переменные:"));
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
let liveMode = "off";
const logsState = { app: null, build: false };

function openLogs(app) {
  logsState.app = app;
  logsState.build = false;
  $("logs-title").textContent = "Логи · " + app.name;
  $("logs").hidden = false;
  loadLogs();
}

async function loadLogs() {
  const { app, build } = logsState;
  for (const tab of document.querySelectorAll(".logs-tab")) {
    const active = (tab.dataset.build === "1") === build;
    tab.className = "logs-tab rounded-md px-3 py-1 " + (active ? "bg-white shadow-sm dark:bg-stone-700" : "text-stone-500 dark:text-stone-400");
  }
  $("logs-meta").textContent = "Загрузка…";
  $("logs-body").textContent = "";
  try {
    const res = await fetch("/api/logs?app=" + encodeURIComponent(app.relPath) + "&build=" + (build ? 1 : 0), { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
    if (!data.deployment) {
      $("logs-meta").textContent = "У сервиса ещё нет деплоев";
      return;
    }
    $("logs-meta").textContent = `Деплой ${data.deployment.id} · ${data.deployment.status} · строк: ${data.lines.length}`;
    $("logs-body").textContent = data.lines.length
      ? data.lines.map((l) => new Date(l.time).toLocaleTimeString("ru-RU") + "  " + l.message).join("\n")
      : "Лог пуст";
    $("logs-body").scrollTop = $("logs-body").scrollHeight;
  } catch (e) {
    $("logs-meta").textContent = "Не удалось получить лог: " + e.message;
  }
}

for (const tab of document.querySelectorAll(".logs-tab")) {
  tab.addEventListener("click", () => { logsState.build = tab.dataset.build === "1"; loadLogs(); });
}
$("logs-refresh").addEventListener("click", loadLogs);
$("logs-close").addEventListener("click", () => { $("logs").hidden = true; });
$("logs").addEventListener("click", (e) => {  // клик по затемнённому фону вокруг окна
  if (e.target === $("logs") || e.target === $("logs-backdrop")) $("logs").hidden = true;
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("logs").hidden = true; });

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
    const time = new Date(data.scannedAt).toLocaleTimeString("ru-RU");
    const live = {
      on: "статусы деплоя из Railway",
      off: "офлайн-режим: задайте RAILWAY_API_TOKEN для живых статусов",
      error: "Railway не ответил, показаны локальные данные",
    }[data.live] || "";
    const costs = [...new Map(data.apps.filter((a) => a.costUsd != null).map((a) => [a.projectId, a.costUsd])).values()];
    const total = costs.length ? " · ≈ " + usd(costs.reduce((x, y) => x + y, 0)) + "/мес" : "";
    $("summary").textContent = (n
      ? `${n} ${plural(n, "приложение", "приложения", "приложений")} · ${linked} привязано к Railway${total} · обновлено в ${time}`
      : `Обновлено в ${time}`) + (linked && live ? " · " + live : "");
  } catch (e) {
    $("error").textContent = "Не удалось получить список: " + e.message + ". Запущен ли small dashboard?";
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
