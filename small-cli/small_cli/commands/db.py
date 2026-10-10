"""small db: база данных для приложения в один шаг.

add — разворачивает шаблон Railway (как `railway add --database`) в проекте приложения и передаёт приложению
ссылку на базу через переменную-ссылку Railway (${{Postgres.DATABASE_URL}}), поэтому пароль нигде не хранится
у нас. База записывается в .small.json, и small destroy удаляет её вместе с приложением.
"""

import argparse
import time

from ..common import CliError, app_dir, ask, linked_state, require_token, say
from ..config import STATE_FILE, save_state
from ..railway import Railway, RailwayError
from .env import add_no_deploy, change_env, report_env_change

# Тип -> (код шаблона Railway, переменная приложения, переменная базы).
DATABASES = {
    "postgres": ("postgres", "DATABASE_URL", "DATABASE_URL"),
}
WORKFLOW_TIMEOUT = 300


def register(sub):
    d = sub.add_parser("db", help="база данных: добавить, список, удалить",
                       description="Базы данных приложения. add разворачивает базу в проекте приложения и передаёт "
                                   "приложению переменную со ссылкой на неё (для postgres — DATABASE_URL). "
                                   "small destroy удаляет базы вместе с приложением.")
    d.add_argument("-C", "--path", default=".", help="папка приложения (по умолчанию текущая)")
    d.set_defaults(func=cmd_db_list)
    db_sub = d.add_subparsers(dest="db_command", metavar="<list|add|remove>")
    dl = db_sub.add_parser("list", help="базы приложения")
    dl.set_defaults(func=cmd_db_list)
    da = db_sub.add_parser("add", help="добавить базу: small db add postgres")
    da.add_argument("kind", choices=sorted(DATABASES))
    add_no_deploy(da)
    da.set_defaults(func=cmd_db_add)
    dr = db_sub.add_parser("remove", help="удалить базу вместе с данными: small db remove postgres")
    dr.add_argument("kind", choices=sorted(DATABASES))
    dr.add_argument("--yes", action="store_true", help="не спрашивать подтверждение (для скриптов)")
    add_no_deploy(dr)
    dr.set_defaults(func=cmd_db_remove)
    for parser in (dl, da, dr):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help="папка приложения")


def cmd_db_list(args):
    root = app_dir(args.path)
    state = linked_state(root)
    databases = state.get("databases", [])
    if not databases:
        say("Баз данных нет. Добавить: small db add postgres")
        return 0
    api = Railway(require_token())
    for db in databases:
        try:
            deployment = api.instance_status(db["serviceId"], state["environmentId"])["deployment"]
            status = deployment["status"] if deployment else "нет деплоев"
        except (RailwayError, KeyError, TypeError) as e:  # базу могли удалить в дашборде Railway
            status = "недоступна: %s" % e
        print("%-10s %-20s %s" % (db["type"], db["name"], status))
    return 0


def cmd_db_add(args):
    root = app_dir(args.path)
    state = linked_state(root)
    if any(db["type"] == args.kind for db in state.get("databases", [])):
        raise CliError("%s уже подключена. Список: small db list" % args.kind)
    template_code, app_var, db_var = DATABASES[args.kind]
    api = Railway(require_token())
    project, env = state["projectId"], state["environmentId"]

    before = {s["id"] for s in api.project_services(project)[1]}
    template = api.template(template_code)
    config = template["serializedConfig"]
    for service in config.get("services", {}).values():  # как `railway add`: значения по умолчанию шаблона
        for variable in (service.get("variables") or {}).values():
            if variable and variable.get("defaultValue"):
                variable["value"] = variable["defaultValue"]
    say("Разворачиваю %s в проекте %s…" % (template["name"], state.get("name", project)))
    workflow = api.deploy_template(project, env, template["id"], config)
    if workflow:
        wait_workflow(api, workflow)

    created = [s for s in api.project_services(project)[1] if s["id"] not in before]
    if len(created) != 1:
        raise CliError("ожидался один новый сервис базы, появилось: %s" % (", ".join(s["name"] for s in created) or "0"))
    service = created[0]
    state.setdefault("databases", []).append({"type": args.kind, "serviceId": service["id"], "name": service["name"]})
    save_state(root / STATE_FILE, state)  # сразу: даже если дальше что-то упадёт, destroy удалит и базу

    reference = "${{%s.%s}}" % (service["name"], db_var)
    linked = change_env(root, {app_var: reference}, deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "База %s (%s) добавлена, %s = %s"
                      % (args.kind, service["name"], app_var, reference))
    say("База запускается около минуты; если приложение подключается к ней при старте, первый запуск может не застать её.")
    return 0


def wait_workflow(api, workflow_id):
    deadline = time.monotonic() + WORKFLOW_TIMEOUT
    while True:
        result = api.workflow_status(workflow_id)
        if result["status"] == "Complete":
            return
        if result["status"] in ("Error", "NotFound"):
            raise CliError("Railway не смог развернуть базу: %s" % (result.get("error") or result["status"]))
        if time.monotonic() > deadline:
            raise CliError("база не развернулась за %d с; проверьте проект в дашборде Railway" % WORKFLOW_TIMEOUT)
        time.sleep(2)


def cmd_db_remove(args):
    root = app_dir(args.path)
    state = linked_state(root)
    db = next((d for d in state.get("databases", []) if d["type"] == args.kind), None)
    if db is None:
        raise CliError("%s не подключена. Список: small db list" % args.kind)
    say("Будет удалена база %s (%s) вместе со всеми данными." % (args.kind, db["name"]))
    if not args.yes:
        answer = ask("Введите %s для подтверждения: " % db["name"])
        if answer is None:
            raise CliError("подтвердите удаление флагом --yes. Ничего не удалено")
        if answer != db["name"]:
            raise CliError("имя не совпало. Ничего не удалено")
    api = Railway(require_token())
    api.delete_service(db["serviceId"])
    state["databases"] = [d for d in state["databases"] if d is not db]
    save_state(root / STATE_FILE, state)
    linked = change_env(root, unset=[DATABASES[args.kind][1]], deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "База %s удалена, %s убрана" % (args.kind, DATABASES[args.kind][1]))
    return 0
