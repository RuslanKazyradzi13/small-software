"""small db: a database for the app in one step.

add deploys a Railway template (like `railway add --database`) into the app's project and passes the database URL
to the app through a Railway reference variable (${{Postgres.DATABASE_URL}}), so the password is never stored
on our side. The database is recorded in .small.json, and small destroy deletes it together with the app.
"""

import argparse
import time

from ..common import CliError, app_dir, ask, linked_state, require_token, say
from ..config import STATE_FILE, save_state
from ..i18n import tr
from ..railway import Railway, RailwayError
from .env import add_no_deploy, change_env, report_env_change

# Type -> (Railway template code, app variable, database variable).
DATABASES = {
    "postgres": ("postgres", "DATABASE_URL", "DATABASE_URL"),
}
WORKFLOW_TIMEOUT = 300


def register(sub):
    d = sub.add_parser("db", help=tr("databases: add, list, remove", "база данных: добавить, список, удалить"),
                       description=tr("App databases. add deploys a database in the app's project and passes the "
                                      "app a variable that points to it (for postgres, DATABASE_URL). "
                                      "small destroy deletes databases together with the app.",
                                      "Базы данных приложения. add разворачивает базу в проекте приложения и передаёт "
                                      "приложению переменную со ссылкой на неё (для postgres — DATABASE_URL). "
                                      "small destroy удаляет базы вместе с приложением."))
    d.add_argument("-C", "--path", default=".", help=tr("app folder (default: current folder)",
                                                        "папка приложения (по умолчанию текущая)"))
    d.set_defaults(func=cmd_db_list)
    db_sub = d.add_subparsers(dest="db_command", metavar="<list|add|remove>")
    dl = db_sub.add_parser("list", help=tr("app databases", "базы приложения"))
    dl.set_defaults(func=cmd_db_list)
    da = db_sub.add_parser("add", help=tr("add a database: small db add postgres",
                                          "добавить базу: small db add postgres"))
    da.add_argument("kind", choices=sorted(DATABASES))
    add_no_deploy(da)
    da.set_defaults(func=cmd_db_add)
    dr = db_sub.add_parser("remove", help=tr("delete a database with all its data: small db remove postgres",
                                             "удалить базу вместе с данными: small db remove postgres"))
    dr.add_argument("kind", choices=sorted(DATABASES))
    dr.add_argument("--yes", action="store_true", help=tr("don't ask for confirmation (for scripts)",
                                                          "не спрашивать подтверждение (для скриптов)"))
    add_no_deploy(dr)
    dr.set_defaults(func=cmd_db_remove)
    for parser in (dl, da, dr):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help=tr("app folder", "папка приложения"))


def cmd_db_list(args):
    root = app_dir(args.path)
    state = linked_state(root)
    databases = state.get("databases", [])
    if not databases:
        say(tr("No databases. Add one: small db add postgres", "Баз данных нет. Добавить: small db add postgres"))
        return 0
    api = Railway(require_token())
    for db in databases:
        try:
            deployment = api.instance_status(db["serviceId"], state["environmentId"])["deployment"]
            status = deployment["status"] if deployment else tr("no deploys", "нет деплоев")
        except (RailwayError, KeyError, TypeError) as e:  # the database may have been deleted in the Railway dashboard
            status = tr("unavailable: %s", "недоступна: %s") % e
        print("%-10s %-20s %s" % (db["type"], db["name"], status))
    return 0


def cmd_db_add(args):
    root = app_dir(args.path)
    state = linked_state(root)
    if any(db["type"] == args.kind for db in state.get("databases", [])):
        raise CliError(tr("%s is already added. List: small db list", "%s уже подключена. Список: small db list")
                       % args.kind)
    template_code, app_var, db_var = DATABASES[args.kind]
    api = Railway(require_token())
    project, env = state["projectId"], state["environmentId"]

    before = {s["id"] for s in api.project_services(project)[1]}
    template = api.template(template_code)
    config = template["serializedConfig"]
    for service in config.get("services", {}).values():  # like `railway add`: the template's default values
        for variable in (service.get("variables") or {}).values():
            if variable and variable.get("defaultValue"):
                variable["value"] = variable["defaultValue"]
    say(tr("Deploying %s in project %s…", "Разворачиваю %s в проекте %s…")
        % (template["name"], state.get("name", project)))
    workflow = api.deploy_template(project, env, template["id"], config)
    if workflow:
        wait_workflow(api, workflow)

    created = [s for s in api.project_services(project)[1] if s["id"] not in before]
    if len(created) != 1:
        raise CliError(tr("expected one new database service, got: %s",
                          "ожидался один новый сервис базы, появилось: %s")
                       % (", ".join(s["name"] for s in created) or "0"))
    service = created[0]
    state.setdefault("databases", []).append({"type": args.kind, "serviceId": service["id"], "name": service["name"]})
    save_state(root / STATE_FILE, state)  # right away: even if something fails later, destroy deletes the database too

    reference = "${{%s.%s}}" % (service["name"], db_var)
    linked = change_env(root, {app_var: reference}, deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, tr("Database %s (%s) added, %s = %s",
                                                     "База %s (%s) добавлена, %s = %s")
                      % (args.kind, service["name"], app_var, reference))
    say(tr("The database takes about a minute to start; if the app connects to it on startup, "
           "the first start may happen before it is ready.",
           "База запускается около минуты; если приложение подключается к ней при старте, "
           "первый запуск может не застать её."))
    return 0


def wait_workflow(api, workflow_id):
    deadline = time.monotonic() + WORKFLOW_TIMEOUT
    while True:
        result = api.workflow_status(workflow_id)
        if result["status"] == "Complete":
            return
        if result["status"] in ("Error", "NotFound"):
            raise CliError(tr("Railway could not deploy the database: %s", "Railway не смог развернуть базу: %s")
                           % (result.get("error") or result["status"]))
        if time.monotonic() > deadline:
            raise CliError(tr("the database was not deployed within %d s; check the project in the Railway dashboard",
                              "база не развернулась за %d с; проверьте проект в дашборде Railway") % WORKFLOW_TIMEOUT)
        time.sleep(2)


def cmd_db_remove(args):
    root = app_dir(args.path)
    state = linked_state(root)
    db = next((d for d in state.get("databases", []) if d["type"] == args.kind), None)
    if db is None:
        raise CliError(tr("%s is not added. List: small db list", "%s не подключена. Список: small db list")
                       % args.kind)
    say(tr("This will delete the %s database (%s) with all its data.",
           "Будет удалена база %s (%s) вместе со всеми данными.") % (args.kind, db["name"]))
    if not args.yes:
        answer = ask(tr("Type %s to confirm: ", "Введите %s для подтверждения: ") % db["name"])
        if answer is None:
            raise CliError(tr("confirm the deletion with --yes. Nothing was deleted",
                              "подтвердите удаление флагом --yes. Ничего не удалено"))
        if answer != db["name"]:
            raise CliError(tr("the name did not match. Nothing was deleted", "имя не совпало. Ничего не удалено"))
    api = Railway(require_token())
    api.delete_service(db["serviceId"])
    state["databases"] = [d for d in state["databases"] if d is not db]
    save_state(root / STATE_FILE, state)
    linked = change_env(root, unset=[DATABASES[args.kind][1]], deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, tr("Database %s deleted, %s removed", "База %s удалена, %s убрана")
                      % (args.kind, DATABASES[args.kind][1]))
    return 0
