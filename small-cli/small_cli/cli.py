"""small — деплой папки с кодом в Railway одной командой: `small deploy`.

`small init` создаёт заготовку приложения из шаблона: сервер с защитой по ссылке-приглашению,
small.json с уникальным именем и ACCESS_TOKEN, .gitignore и .railwayignore.

`small dashboard` поднимает локальную страницу со списком приложений в папке (см. dashboard.py).

`small link` / `small open` — ссылка-приглашение задеплоенного приложения, `small logs` — его логи,
`small destroy` — удалить его из Railway. `small env` — переменные приложения, `small access` — личные
ссылки-приглашения (токен на каждого человека с отзывом).

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

from . import __version__, access, dashboard, scaffold
from .archive import collect_files, make_tarball
from .config import CONFIG_FILE, ENV_NAME_RE, STATE_FILE, ConfigError, load_config, load_state, save_state
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
        k.add_argument("--as", dest="person", metavar="ИМЯ", help="личная ссылка человека из small access")
        k.set_defaults(func=cmd_link, open=opens)

    e = sub.add_parser("env", help="переменные приложения: список, задать, удалить",
                       description="Переменные приложения. set/unset меняют их сразу и в Railway, и в %s "
                                   "(иначе следующий small deploy вернул бы старые значения), затем Railway "
                                   "перезапускает приложение. Без подкоманды — список." % CONFIG_FILE)
    e.add_argument("-C", "--path", default=".", help="папка приложения (по умолчанию текущая)")
    e.set_defaults(func=cmd_env_list, values=False)
    env_sub = e.add_subparsers(dest="env_command", metavar="<list|set|unset>")
    el = env_sub.add_parser("list", help="показать переменные (значения скрыты)")
    el.add_argument("--values", action="store_true", help="показать значения")
    el.set_defaults(func=cmd_env_list)
    es = env_sub.add_parser("set", help="задать переменные: small env set KEY=VALUE ...")
    es.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    es.set_defaults(func=cmd_env_set)
    eu = env_sub.add_parser("unset", help="удалить переменные: small env unset KEY ...")
    eu.add_argument("names", nargs="+", metavar="KEY")
    eu.set_defaults(func=cmd_env_unset)
    for parser_ in (el, es, eu):
        parser_.add_argument("-C", "--path", default=argparse.SUPPRESS, help="папка приложения")
    for parser_ in (es, eu):
        parser_.add_argument("--no-deploy", action="store_true",
                             help="не перезапускать приложение (изменения вступят в силу при следующем деплое)")

    a = sub.add_parser("access", help="личные ссылки-приглашения: список, выдать, отозвать",
                       description="Токен на каждого человека. add выдаёт личную ссылку, revoke отзывает её: "
                                   "ссылка и куки этого человека перестают работать, у остальных всё остаётся. "
                                   "Токены хранятся в ACCESS_TOKEN (в %s и в Railway)." % CONFIG_FILE)
    a.add_argument("-C", "--path", default=".", help="папка приложения (по умолчанию текущая)")
    a.set_defaults(func=cmd_access_list)
    access_sub = a.add_subparsers(dest="access_command", metavar="<list|add|revoke>")
    al = access_sub.add_parser("list", help="у кого есть доступ")
    al.set_defaults(func=cmd_access_list)
    aa = access_sub.add_parser("add", help="выдать личную ссылку: small access add bob")
    aa.add_argument("name", metavar="ИМЯ")
    aa.set_defaults(func=cmd_access_add)
    ar = access_sub.add_parser("revoke", help="отозвать доступ: small access revoke bob")
    ar.add_argument("name", metavar="ИМЯ")
    ar.set_defaults(func=cmd_access_revoke)
    for parser_ in (al, aa, ar):
        parser_.add_argument("-C", "--path", default=argparse.SUPPRESS, help="папка приложения")
    for parser_ in (aa, ar):
        parser_.add_argument("--no-deploy", action="store_true", help="не перезапускать приложение")

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
    entries, source = access.current(root, state, warn=say)
    token = access.pick(entries, args.person)
    if args.person is not None and token is None:
        raise CliError("у %s нет личной ссылки. Есть: %s. Выдать: small access add %s"
                       % (args.person, ", ".join(n or "(общая)" for n, _ in entries) or "—", args.person))
    link = access.invite_link(state["domain"], token)
    if token:
        say("Ссылка-приглашение%s (ACCESS_TOKEN из %s). Кто её получит, тот войдёт:"
            % (" для " + args.person if args.person else "", source))
    else:
        say("ACCESS_TOKEN не найден ни в %s, ни в Railway — адрес без токена:" % CONFIG_FILE)
    print(link, flush=True)
    if args.open:
        webbrowser.open(link)
    return 0


def is_linked(state):
    return all(state.get(k) for k in ("projectId", "environmentId", "serviceId"))


def change_env(root, set_vars=None, unset=(), deploy=True):
    """Меняет переменные в Railway (если приложение задеплоено) и в small.json — чтобы они не расходились.

    Сначала Railway: если он откажет, small.json остаётся прежним. Возвращает True, если трогали Railway.
    """
    set_vars, unset = dict(set_vars or {}), list(unset)
    config_path, state = root / CONFIG_FILE, load_state(root / STATE_FILE)
    load_config(config_path)  # битый small.json — ошибка до любых изменений
    raw = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.is_file() else {}

    if is_linked(state):
        api = Railway(require_token())
        ids = (state["projectId"], state["environmentId"], state["serviceId"])
        existing = api.service_variables(*ids)
        if set_vars:
            api.upsert_variables(*ids, set_vars)
        for name in unset:
            if name in existing:
                api.delete_variable(*ids, name)
        if deploy:
            api.redeploy(state["serviceId"], state["environmentId"])

    env = raw.setdefault("env", {})
    env.update(set_vars)
    for name in unset:
        env.pop(name, None)
    config_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return is_linked(state)


def report_env_change(linked, deploy, what):
    if not linked:
        say("%s. Записано в %s; приложение ещё не задеплоено — в Railway попадёт при small deploy."
            % (what, CONFIG_FILE))
    elif deploy:
        say("%s. Обновлено в Railway и %s, Railway перезапускает приложение (small logs -f — следить)."
            % (what, CONFIG_FILE))
    else:
        say("%s. Обновлено в Railway и %s, вступит в силу при следующем деплое." % (what, CONFIG_FILE))


def cmd_env_list(args):
    root = app_dir(args.path)
    config_env = load_config(root / CONFIG_FILE)["env"]
    state = load_state(root / STATE_FILE)
    railway_env = None
    if is_linked(state):
        if token_from_env():
            railway_env = {k: v for k, v in Railway(token_from_env()).service_variables(
                state["projectId"], state["environmentId"], state["serviceId"]).items() if not k.startswith("RAILWAY_")}
        else:
            say("Нет RAILWAY_API_TOKEN — показываю только %s" % CONFIG_FILE)
    names = sorted(set(config_env) | set(railway_env or {}))
    if not names:
        say("Переменных нет. Задать: small env set KEY=VALUE")
        return 0
    width = max(len(n) for n in names) + 2
    for name in names:
        in_config, in_railway = name in config_env, railway_env is not None and name in railway_env
        value = railway_env[name] if in_railway else config_env[name]
        if in_config and in_railway:
            where = CONFIG_FILE + " + Railway" if config_env[name] == railway_env[name] else "Railway ≠ " + CONFIG_FILE
        elif in_railway:
            where = "только Railway"
        else:
            where = CONFIG_FILE + (" (в Railway — после small deploy)" if railway_env is not None else "")
        shown = value if getattr(args, "values", False) else "•" * min(len(value), 8) + " (%d)" % len(value)
        print("%-*s%-24s %s" % (width, name, shown, where))
    if not getattr(args, "values", False):
        say("Значения скрыты: small env list --values")
    return 0


def cmd_env_set(args):
    root = app_dir(args.path)
    pairs = {}
    for pair in args.pairs:
        name, sep, value = pair.partition("=")
        if not sep or not ENV_NAME_RE.match(name):
            raise CliError("ожидается KEY=VALUE (KEY — латиница, цифры, _; не с цифры), получено: %s" % pair)
        pairs[name] = value
    linked = change_env(root, set_vars=pairs, deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "Заданы: " + ", ".join(sorted(pairs)))
    return 0


def cmd_env_unset(args):
    root = app_dir(args.path)
    linked = change_env(root, unset=args.names, deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "Удалены: " + ", ".join(args.names))
    return 0


def cmd_access_list(args):
    root = app_dir(args.path)
    entries, source = access.current(root, load_state(root / STATE_FILE), warn=say)
    if not entries:
        say("ACCESS_TOKEN не задан — защищённое приложение закрыто для всех. Выдать доступ: small access add ИМЯ")
        return 0
    say("Доступ (ACCESS_TOKEN из %s):" % source)
    for name, token in entries:
        print("  %-24s %s…" % (name or "(общая ссылка)", token[:4]))
    say("Личная ссылка: small link --as ИМЯ · отозвать: small access revoke ИМЯ")
    return 0


def cmd_access_add(args):
    root = app_dir(args.path)
    if not access.NAME_RE.match(args.name):
        raise CliError("имя — до 40 символов: латиница, цифры, _ . @ -")
    state = load_state(root / STATE_FILE)
    entries, _ = access.current(root, state, warn=say)
    if any(name == args.name for name, _ in entries):
        raise CliError("у %s уже есть ссылка: small link --as %s" % (args.name, args.name))
    token = access.new_token()
    linked = change_env(root, {"ACCESS_TOKEN": access.serialize(entries + [(args.name, token)])},
                        deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "Доступ для %s добавлен" % args.name)
    if state.get("domain"):
        say("Личная ссылка для %s:" % args.name)
        print(access.invite_link(state["domain"], token), flush=True)
    else:
        say("Ссылка появится после small deploy: small link --as %s" % args.name)
    return 0


def cmd_access_revoke(args):
    root = app_dir(args.path)
    entries, _ = access.current(root, load_state(root / STATE_FILE), warn=say)
    remaining = [(name, token) for name, token in entries if name != args.name]
    if len(remaining) == len(entries):
        raise CliError("у %s нет личной ссылки. Есть: %s"
                       % (args.name, ", ".join(n or "(общая)" for n, _ in entries) or "—"))
    if not remaining:
        say("Это последний токен: после отзыва приложение будет закрыто для всех.")
    linked = change_env(root, {"ACCESS_TOKEN": access.serialize(remaining)} if remaining else None,
                        unset=() if remaining else ["ACCESS_TOKEN"], deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "Доступ %s отозван — его ссылка и куки перестанут действовать после перезапуска" % args.name)
    return 0


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
    say("  small open        # открыть по ссылке-приглашению")
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
