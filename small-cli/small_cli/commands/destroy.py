"""small destroy: удалить приложение из Railway — проект целиком, если в нём только сервисы этого приложения
(сам сервис и его базы данных), иначе только их. Затем удаляется .small.json; small.json остаётся."""

from ..common import CliError, app_dir, ask, linked_state, require_token, say
from ..config import CONFIG_FILE, STATE_FILE
from ..i18n import tr
from ..railway import Railway, RailwayError


def register(sub):
    x = sub.add_parser("destroy", help=tr("delete the app from Railway", "удалить приложение из Railway"),
                       description=tr("Delete the app from Railway: the whole project if it only has this app's "
                                      "services (the service itself and its databases), otherwise just those. "
                                      "Then %s is deleted; %s stays.",
                                      "Удалить приложение из Railway: проект целиком, если в нём только сервисы этого "
                                      "приложения (сам сервис и его базы данных), иначе только их. Затем удаляется %s; "
                                      "%s остаётся.") % (STATE_FILE, CONFIG_FILE))
    x.add_argument("path", nargs="?", default=".", help=tr("app folder (default: current folder)",
                                                           "папка приложения (по умолчанию текущая)"))
    x.add_argument("--yes", action="store_true", help=tr("don't ask for confirmation (for scripts)",
                                                         "не спрашивать подтверждение (для скриптов)"))
    x.set_defaults(func=cmd_destroy)


def cmd_destroy(args):
    root = app_dir(args.path)
    state = linked_state(root)
    api = Railway(require_token())
    try:
        project_name, services = api.project_services(state["projectId"])
    except RailwayError as e:
        raise CliError(tr("project %s not found in Railway (%s). If it is already deleted, delete %s manually",
                          "проект %s не найден в Railway (%s). Если он уже удалён, удалите %s вручную")
                       % (state["projectId"], e, root / STATE_FILE)) from None
    own_ids = {state["serviceId"]} | {db["serviceId"] for db in state.get("databases", [])}
    own = [s for s in services if s["id"] in own_ids]
    others = [s["name"] for s in services if s["id"] not in own_ids]
    if others:
        say(tr("This will delete services %s from project %s. Other services in the project stay: %s",
               "Будут удалены сервисы %s из проекта %s. Другие сервисы проекта останутся: %s")
            % (", ".join(s["name"] for s in own) or state.get("name", ""), project_name, ", ".join(others)))
    else:
        say(tr("This will delete the whole project %s: %s, domain %s, variables and all deploys.",
               "Будет удалён проект %s целиком: %s, домен %s, переменные и все деплои.")
            % (project_name, ", ".join(s["name"] for s in own) or tr("service", "сервис"), state.get("domain") or "—"))
    if not args.yes:
        answer = ask(tr("Type the project name (%s) to confirm: ", "Введите имя проекта (%s) для подтверждения: ")
                     % project_name)
        if answer is None:
            raise CliError(tr("confirm the deletion with --yes. Nothing was deleted",
                              "подтвердите удаление флагом --yes. Ничего не удалено"))
        if answer != project_name:
            raise CliError(tr("the name did not match. Nothing was deleted", "имя не совпало. Ничего не удалено"))

    if others:
        for service in own:
            api.delete_service(service["id"])
    else:
        api.delete_project(state["projectId"])
    (root / STATE_FILE).unlink()
    say(tr("Deleted. %s removed, %s kept: the next small deploy creates a new project.",
           "Удалено. %s удалён, %s оставлен: следующий small deploy создаст новый проект.") % (STATE_FILE, CONFIG_FILE))
    return 0
