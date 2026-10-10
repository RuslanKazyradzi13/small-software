"""small — деплой папки с кодом в Railway одной командой: `small deploy`.

`small init` создаёт заготовку приложения из шаблона: сервер с защитой по ссылке-приглашению,
small.json с уникальным именем и ACCESS_TOKEN, .gitignore и .railwayignore.

`small dashboard` поднимает локальную страницу со списком приложений в папке (см. dashboard.py).

`small link` / `small open` — ссылка-приглашение задеплоенного приложения, `small logs` — его логи,
`small destroy` — удалить его из Railway.

Ход `small deploy`: прочитать small.json -> упаковать код -> найти проект по имени или создать его
(projectCreate + serviceCreate) -> записать переменные из small.json (variableCollectionUpsert)
-> загрузить архив (POST .../up) -> serviceDomainCreate -> опрашивать deployment(id).status
-> напечатать https://<домен>.

Прогресс пишется в stderr, итоговый URL — в stdout, поэтому `URL=$(small deploy)` работает.
"""

import argparse
import json
import os
import re
import secrets
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import __version__, dashboard, scaffold
from .archive import collect_files, make_tarball
from .config import CONFIG_FILE, STATE_FILE, ConfigError, load_config, load_state, save_state
from .railway import Railway, RailwayError, token_from_env

OK_STATUSES = {"SUCCESS", "SLEEPING"}
FAILED_STATUSES = {"FAILED", "CRASHED", "REMOVED", "REMOVING", "SKIPPED", "NEEDS_APPROVAL"}
MAX_POLL_ERRORS = 5


class CliError(Exception):
    pass


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CliError, ConfigError, RailwayError) as e:
        print("Ошибка: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nПрервано. Уже запущенная сборка продолжится в Railway.", file=sys.stderr)
        return 130


def build_parser():
    parser = argparse.ArgumentParser(prog="small", description="Деплой микросервисов в Railway.")
    parser.add_argument("--version", action="version", version="small " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="<команда>")
    sub.required = True

    d = sub.add_parser("deploy", help="задеплоить папку с кодом и вывести публичный URL",
                       description="Задеплоить папку с кодом в Railway и вывести публичный URL. "
                                   "Токен берётся из RAILWAY_API_TOKEN.")
    d.add_argument("path", nargs="?", default=".", help="папка с кодом (по умолчанию текущая)")
    d.add_argument("--name", help="имя проекта и сервиса (по умолчанию — name из small.json, иначе имя папки)")
    d.add_argument("--workspace", help="ID workspace, если их несколько (или переменная RAILWAY_WORKSPACE_ID)")
    d.add_argument("--port", type=int, help="порт, который слушает приложение (по умолчанию Railway берёт $PORT)")
    d.add_argument("-m", "--message", help="подпись деплоя")
    d.add_argument("--new", action="store_true", help="создать новый проект, даже если есть " + STATE_FILE)
    d.add_argument("--no-wait", action="store_true", help="не ждать окончания сборки")
    d.add_argument("--timeout", type=int, default=900, help="сколько секунд ждать сборку (по умолчанию 900)")
    d.add_argument("--interval", type=float, default=3.0, help=argparse.SUPPRESS)
    d.set_defaults(func=cmd_deploy)

    templates = "\n".join("  %-12s %s" % (k, v["description"]) for k, v in scaffold.TEMPLATES.items())
    i = sub.add_parser("init", help="создать заготовку микро-приложения",
                       description="Создать заготовку микро-приложения: сервер с защитой по ссылке-приглашению, "
                                   "%s, .gitignore и .railwayignore. Существующие файлы не перезаписываются "
                                   "без подтверждения." % CONFIG_FILE,
                       epilog="Шаблоны:\n" + templates, formatter_class=argparse.RawDescriptionHelpFormatter)
    i.add_argument("path", nargs="?", default=".", help="папка приложения (по умолчанию текущая)")
    i.add_argument("-t", "--template", choices=list(scaffold.TEMPLATES), default=scaffold.DEFAULT_TEMPLATE,
                   help="шаблон (по умолчанию %s)" % scaffold.DEFAULT_TEMPLATE)
    i.add_argument("--name", help="имя проекта (по умолчанию — имя папки со случайным суффиксом)")
    i.add_argument("--force", action="store_true", help="перезаписать существующие файлы без вопросов")
    i.set_defaults(func=cmd_init)

    b = sub.add_parser("dashboard", help="локальная страница со списком приложений",
                       description="Найти в папке приложения (small.json / .small.json, до %d уровней вглубь) "
                                   "и показать их в браузере: имя, статус, ссылки, путь." % dashboard.MAX_DEPTH)
    b.add_argument("path", nargs="?", default=".", help="где искать (по умолчанию текущая папка; например, ..)")
    b.add_argument("--port", type=int, default=dashboard.DEFAULT_PORT,
                   help="порт (по умолчанию %d; если занят — любой свободный)" % dashboard.DEFAULT_PORT)
    b.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    b.set_defaults(func=cmd_dashboard)

    for name, opens, text in (("link", False, "напечатать ссылку-приглашение задеплоенного приложения"),
                              ("open", True, "открыть задеплоенное приложение по ссылке-приглашению")):
        k = sub.add_parser(name, help=text, description=text.capitalize() + ". ACCESS_TOKEN берётся из %s, "
                           "а если его там нет — из переменных сервиса в Railway (нужен RAILWAY_API_TOKEN)."
                           % CONFIG_FILE)
        k.add_argument("path", nargs="?", default=".", help="папка приложения (по умолчанию текущая)")
        k.set_defaults(func=cmd_link, open=opens)

    g = sub.add_parser("logs", help="логи приложения из Railway",
                       description="Показать лог последнего деплоя: приложения (по умолчанию) или сборки.")
    g.add_argument("path", nargs="?", default=".", help="папка приложения (по умолчанию текущая)")
    g.add_argument("--build", action="store_true", help="лог сборки вместо лога приложения")
    g.add_argument("-n", "--lines", type=int, default=100, help="сколько строк (по умолчанию 100)")
    g.add_argument("-f", "--follow", action="store_true", help="следить за новыми строками (Ctrl+C — выход)")
    g.add_argument("--interval", type=float, default=2.0, help=argparse.SUPPRESS)
    g.set_defaults(func=cmd_logs)

    x = sub.add_parser("destroy", help="удалить приложение из Railway",
                       description="Удалить приложение из Railway: проект целиком, если в нём только этот сервис, "
                                   "иначе только сервис. Затем удаляется %s; %s остаётся." % (STATE_FILE, CONFIG_FILE))
    x.add_argument("path", nargs="?", default=".", help="папка приложения (по умолчанию текущая)")
    x.add_argument("--yes", action="store_true", help="не спрашивать подтверждение (для скриптов)")
    x.set_defaults(func=cmd_destroy)
    return parser


def app_dir(path):
    root = Path(path).resolve()
    if not root.is_dir():
        raise CliError("папка не найдена: %s" % root)
    return root


def linked_state(root):
    state = load_state(root / STATE_FILE)
    if not all(state.get(k) for k in ("projectId", "environmentId", "serviceId")):
        raise CliError("%s ещё не задеплоено (нет %s). Сначала: small deploy" % (root, STATE_FILE))
    return state


def require_token():
    token = token_from_env()
    if not token:
        raise CliError("не задан токен Railway. Создайте его в Railway → Account Settings → Tokens "
                       "и положите в переменную окружения RAILWAY_API_TOKEN.")
    return token


def cmd_link(args):
    root = app_dir(args.path)
    state = linked_state(root)
    if not state.get("domain"):
        raise CliError("у сервиса нет публичного домена — выполните small deploy")
    url = "https://%s/" % state["domain"]
    token, source = access_token(root, state)
    if token:
        link = url + "?token=" + quote(token, safe="")
        say("Ссылка-приглашение (ACCESS_TOKEN из %s). Кто её получит, тот войдёт:" % source)
    else:
        link = url
        say("ACCESS_TOKEN не найден ни в %s, ни в Railway — адрес без токена:" % CONFIG_FILE)
    print(link, flush=True)
    if args.open:
        webbrowser.open(link)
    return 0


def access_token(root, state):
    """(первый ACCESS_TOKEN, откуда он) — из small.json, иначе из переменных сервиса в Railway."""
    raw = load_config(root / CONFIG_FILE)["env"].get("ACCESS_TOKEN")
    source = CONFIG_FILE
    if not raw and token_from_env():
        try:
            raw = Railway(token_from_env()).service_variables(
                state["projectId"], state["environmentId"], state["serviceId"]).get("ACCESS_TOKEN")
            source = "Railway"
        except RailwayError as e:
            say("Не удалось прочитать переменные сервиса в Railway: %s" % e)
    first = (raw or "").split(",")[0].strip()
    return (first, source) if first else (None, None)


def cmd_logs(args):
    root = app_dir(args.path)
    state = linked_state(root)
    api = Railway(require_token())
    deployment = api.latest_deployment(state["serviceId"], state["environmentId"])
    if not deployment:
        raise CliError("у сервиса %s ещё нет деплоев" % state.get("name", ""))
    say("Лог %s · деплой %s · %s" % ("сборки" if args.build else "приложения", deployment["id"], deployment["status"]))

    seen, last = set(), {"ts": None}

    def show(entries):
        for entry in entries:
            key = (entry["timestamp"], entry["message"])
            if key not in seen:
                seen.add(key)
                print("%s  %s" % (log_time(entry["timestamp"]), entry["message"]), flush=True)
                last["ts"] = max(last["ts"] or "", entry["timestamp"])

    show(api.log_entries(deployment["id"], build=args.build, limit=args.lines))
    if not args.follow:
        return 0
    say("Слежу за новыми строками, Ctrl+C — выход")
    try:
        while True:
            time.sleep(args.interval)
            try:
                show(api.log_entries(deployment["id"], build=args.build, limit=args.lines, start_date=last["ts"]))
            except RailwayError as e:
                say("  повторяю запрос: %s" % e)
    except KeyboardInterrupt:
        return 0


def log_time(timestamp):
    """ISO-время Railway (UTC, с наносекундами) -> локальное ЧЧ:ММ:СС."""
    try:
        moment = datetime.strptime(timestamp[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return str(timestamp)


def cmd_destroy(args):
    root = app_dir(args.path)
    state = linked_state(root)
    api = Railway(require_token())
    try:
        project_name, services = api.project_services(state["projectId"])
    except RailwayError as e:
        raise CliError("проект %s не найден в Railway (%s). Если он уже удалён, удалите %s вручную"
                       % (state["projectId"], e, root / STATE_FILE)) from None
    others = [s["name"] for s in services if s["id"] != state["serviceId"]]
    if others:
        say("Будет удалён сервис %s из проекта %s. Другие сервисы проекта останутся: %s"
            % (state.get("name", state["serviceId"]), project_name, ", ".join(others)))
    else:
        say("Будет удалён проект %s целиком: сервис, домен %s, переменные и все деплои."
            % (project_name, state.get("domain") or "—"))
    if not args.yes:
        answer = ask("Введите имя проекта (%s) для подтверждения: " % project_name)
        if answer is None:
            raise CliError("подтвердите удаление флагом --yes. Ничего не удалено")
        if answer != project_name:
            raise CliError("имя не совпало. Ничего не удалено")

    if others:
        api.delete_service(state["serviceId"])
    else:
        api.delete_project(state["projectId"])
    (root / STATE_FILE).unlink()
    say("Удалено. %s удалён, %s оставлен: следующий small deploy создаст новый проект." % (STATE_FILE, CONFIG_FILE))
    return 0


def cmd_dashboard(args):
    root = Path(args.path).resolve()
    if not root.is_dir():
        raise CliError("папка не найдена: %s" % root)
    token = token_from_env()
    server = dashboard.start_server(root, args.port, token)
    port = server.server_address[1]
    if args.port and port != args.port:
        say("Порт %d занят, использую %d" % (args.port, port))
    found = len(dashboard.scan_apps(root))
    say("small dashboard: %s, приложений найдено: %d" % (root, found))
    say("Статусы деплоя из Railway: %s" % ("включены" if token else "выключены (нет RAILWAY_API_TOKEN), офлайн-режим"))
    say("Ctrl+C — остановить")
    print(server.url, flush=True)
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("Дашборд остановлен")
    finally:
        server.server_close()
    return 0


def cmd_init(args):
    root = Path(args.path).resolve()
    if root.exists() and not root.is_dir():
        raise CliError("%s — это файл, а не папка" % root)
    name = slugify(args.name) if args.name else unique_name(root.name)
    config = {"name": name, "env": {"ACCESS_TOKEN": secrets.token_urlsafe(32)}}
    files = scaffold.template_files(args.template)
    files[CONFIG_FILE] = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    # Ignore-файлы не перезаписываются, а дополняются, поэтому конфликтом не считаются.
    conflicts = [rel for rel in files if (root / rel).exists()]
    if conflicts and not args.force:
        listing = ", ".join(conflicts)
        if not confirm("В %s уже есть: %s. Перезаписать? [y/N] " % (root, listing)):
            raise CliError("в %s уже есть: %s. Ничего не изменено. Чтобы перезаписать, добавьте --force"
                           % (root, listing))

    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        (root / rel).write_bytes(content)
    merged = {rel: scaffold.merge_ignore(root / rel, lines)
              for rel, lines in scaffold.ignore_rules(args.template).items()}

    labels = {"created": "создан", "updated": "дополнен", "unchanged": "без изменений"}
    say("Шаблон %s в %s:" % (args.template, root))
    width = max(len(rel) for rel in list(files) + list(merged)) + 2
    for rel in files:
        say("  %-*s%s" % (width, rel, "перезаписан" if rel in conflicts else "создан"))
    for rel, result in merged.items():
        say("  %-*s%s" % (width, rel, labels[result]))
    say("")
    say("Имя проекта: %s. В %s сгенерирован ACCESS_TOKEN; файл добавлен в .gitignore, не публикуйте его."
        % (name, CONFIG_FILE))
    say("Дальше:")
    if root != Path.cwd().resolve():
        say("  cd \"%s\"" % root)
    say("  small deploy")
    say("  затем откройте https://<домен>/?token=<ACCESS_TOKEN из %s>" % CONFIG_FILE)
    return 0


def unique_name(folder_name):
    """Имя папки + случайный суффикс: явное имя из small.json привязывает деплой к одноимённому проекту,
    поэтому оно не должно совпасть с чужим проектом, а поддомен <name>.up.railway.app — с занятым."""
    return "%s-%s" % (slugify(folder_name)[:35].strip("-") or "app", secrets.token_hex(2))


def ask(prompt):
    """Ответ пользователя или None, если терминала нет (скрипт, CI) или ввод закрыт."""
    if not sys.stdin or not sys.stdin.isatty():
        return None
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        return input().strip()
    except EOFError:
        sys.stderr.write("\n")
        return None


def confirm(prompt):
    answer = ask(prompt)
    return answer is not None and answer.lower() in ("y", "yes", "д", "да")


def cmd_deploy(args):
    token = require_token()
    root = Path(args.path).resolve()
    if not root.is_dir():
        raise CliError("папка не найдена: %s" % root)

    config = load_config(root / CONFIG_FILE)
    state_path = root / STATE_FILE
    state = {} if args.new else load_state(state_path)
    reused = bool(state.get("serviceId"))
    explicit_name = args.name or config["name"]
    name = slugify(explicit_name or root.name)

    files = collect_files(str(root), skip_root_files=(CONFIG_FILE, STATE_FILE))
    if not files:
        raise CliError("в %s нечего деплоить (всё отфильтровано .gitignore/.railwayignore?)" % root)
    tarball = make_tarball(str(root), files)
    say("Упаковано файлов: %d (%s)" % (len(files), human_size(len(tarball))))

    api = Railway(token)
    if reused:
        say("Проект %s из %s" % (state["name"], STATE_FILE))
        if explicit_name and name != state["name"]:
            say("  Имя %s не применено: папка уже привязана к %s. Чтобы привязать её к %s, удалите %s."
                % (name, state["name"], name, STATE_FILE))
    else:
        workspace_id = args.workspace or os.environ.get("RAILWAY_WORKSPACE_ID") or pick_workspace(api)
        # Явно заданное имя (small.json или --name) идентифицирует сервис: если проект уже есть
        # в Railway (например, деплой с другого компьютера), подключаемся к нему, а не создаём дубликат.
        state = bind_existing(api, name, workspace_id) if explicit_name and not args.new else None
        if state:
            say("Найден проект %s в Railway, папка привязана к нему" % name)
        else:
            project_id, environment_id = api.create_project(name, workspace_id)
            say("Создан проект %s" % name)
            service_id = api.create_service(project_id, environment_id, name)
            say("Создан сервис %s" % name)
            state = {"name": name, "projectId": project_id,
                     "environmentId": environment_id, "serviceId": service_id}
        # Сохраняем сразу: если сборка упадёт, повторный запуск не наплодит новых проектов.
        save_state(state_path, state)

    if config["env"]:
        # До загрузки кода, чтобы новая сборка сразу получила переменные. Значения не печатаем.
        api.upsert_variables(state["projectId"], state["environmentId"], state["serviceId"], config["env"])
        say("Переменные из %s: %s" % (CONFIG_FILE, ", ".join(sorted(config["env"]))))

    try:
        up = api.upload(state["projectId"], state["environmentId"], state["serviceId"], tarball, args.message)
    except RailwayError as e:
        if reused:
            raise RailwayError("%s\nЕсли проект удалён в Railway, запустите `small deploy --new`." % e) from None
        raise
    deployment_id = up["deploymentId"]
    say("Код загружен, деплой %s" % deployment_id)
    if up.get("logsUrl"):
        say("Логи: %s" % up["logsUrl"].rstrip("&"))

    if not state.get("domain"):
        state["domain"] = ensure_domain(api, state, args.port)
        save_state(state_path, state)
        say("Публичный домен: %s" % state["domain"])
    url = "https://" + state["domain"]

    if args.no_wait:
        print(url)
        return 0

    status = wait_for_deployment(api, deployment_id, args.timeout, args.interval)
    if status not in OK_STATUSES:
        show_logs(api, deployment_id, build=(status == "FAILED"))
        raise CliError("деплой завершился со статусом %s" % status)
    say("Готово!")
    print(url)
    return 0


def pick_workspace(api):
    try:
        workspaces = api.workspaces()
    except RailwayError:
        return None  # токен workspace: `me` ему недоступен, проект создастся в его же workspace
    if len(workspaces) <= 1:
        return workspaces[0]["id"] if workspaces else None
    listing = "\n".join("  %s  %s" % (w["id"], w["name"]) for w in workspaces)
    raise CliError("у аккаунта несколько workspace — укажите нужный: small deploy --workspace <id>\n" + listing)


def bind_existing(api, name, workspace_id):
    """Ищет в Railway проект с именем name и возвращает state для привязки к нему, либо None."""
    try:
        projects = api.find_projects(name, workspace_id)
    except RailwayError as e:
        say("Не удалось проверить, есть ли проект %s в Railway (%s), создаю новый" % (name, e))
        return None
    if not projects:
        return None
    if len(projects) > 1:
        raise CliError("в Railway несколько проектов с именем %s (%s). Удалите лишние или задайте другое имя."
                       % (name, ", ".join(p["id"] for p in projects)))
    project = projects[0]
    if not project["environments"]:
        raise CliError("у проекта %s в Railway нет окружений" % name)
    env = next((e for e in project["environments"] if e["name"] == "production"), project["environments"][0])
    services = project["services"]
    service = next((s for s in services if s["name"] == name), services[0] if len(services) == 1 else None)
    if service is None and services:
        raise CliError("в проекте %s несколько сервисов (%s), ни один не называется %s"
                       % (name, ", ".join(s["name"] for s in services), name))
    state = {"name": name, "projectId": project["id"], "environmentId": env["id"]}
    if service is None:
        state["serviceId"] = api.create_service(project["id"], env["id"], name)
        say("Создан сервис %s" % name)
        return state
    state["serviceId"] = service["id"]
    domains = api.service_domains(project["id"], env["id"], service["id"])
    if domains:
        state["domain"] = domains[0]["domain"]
    return state


def ensure_domain(api, state, port):
    """Создаёт домен *.up.railway.app и пытается переименовать его в <name>.up.railway.app."""
    sid, eid = state["serviceId"], state["environmentId"]
    domain_id, domain = api.create_domain(sid, eid, port)
    wanted = state["name"] + ".up.railway.app"
    if domain == wanted:
        return domain
    try:
        if not api.domain_available(wanted):
            return domain
        api.rename_domain(domain_id, sid, eid, wanted, port)
    except RailwayError as e:
        say("Не удалось занять %s (%s), оставляю %s" % (wanted, e, domain))
        return domain
    try:
        return api.service_domain(state["projectId"], eid, sid, domain_id) or wanted
    except RailwayError:
        return wanted


def wait_for_deployment(api, deployment_id, timeout, interval):
    started = time.monotonic()
    last, errors = None, 0
    while True:
        try:
            status = api.deployment_status(deployment_id)
            errors = 0
        except RailwayError as e:
            errors += 1
            if errors >= MAX_POLL_ERRORS:
                raise
            say("  повторяю запрос статуса: %s" % e)
            status = last
        elapsed = time.monotonic() - started
        if status != last:
            say("[%s] %s" % (format_elapsed(elapsed), status))
            last = status
        if status in OK_STATUSES or status in FAILED_STATUSES:
            return status
        if elapsed > timeout:
            raise CliError("сборка не завершилась за %d с (статус %s); она продолжается в Railway"
                           % (timeout, status))
        time.sleep(interval)


def show_logs(api, deployment_id, build):
    try:
        lines = api.logs(deployment_id, build=build)
    except RailwayError as e:
        say("Не удалось получить логи: %s" % e)
        return
    if lines:
        say("Последние строки %s:" % ("лога сборки" if build else "лога приложения"))
        for line in lines:
            say("  " + line)


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:40].strip("-") or "app"


def human_size(size):
    for unit in ("Б", "КБ", "МБ"):
        if size < 1024 or unit == "МБ":
            return ("%d %s" if unit == "Б" else "%.1f %s") % (size, unit)
        size /= 1024.0


def format_elapsed(seconds):
    return "%02d:%02d" % divmod(int(seconds), 60)


def say(message):
    print(message, file=sys.stderr, flush=True)
