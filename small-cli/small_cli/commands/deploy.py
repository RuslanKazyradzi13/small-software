"""small deploy: pack the code -> find the project by name or create it (projectCreate + serviceCreate)
-> serviceDomainCreate -> write the variables from small.json (variableCollectionUpsert) -> upload the archive
(POST .../up) -> poll deployment(id).status -> print https://<domain>.

Progress goes to stderr and the final URL to stdout, so `URL=$(small deploy)` works.
"""

import argparse
import os
import time

from ..archive import collect_files, make_tarball
from ..common import CliError, app_dir, format_elapsed, human_size, require_token, say, slugify
from ..config import CONFIG_FILE, STATE_FILE, load_config, load_state, save_state
from ..i18n import lang, tr
from ..railway import Railway, RailwayError

OK_STATUSES = {"SUCCESS", "SLEEPING"}
FAILED_STATUSES = {"FAILED", "CRASHED", "REMOVED", "REMOVING", "SKIPPED", "NEEDS_APPROVAL"}
MAX_POLL_ERRORS = 5


def register(sub):
    d = sub.add_parser("deploy", help=tr("deploy a code folder and print its public URL",
                                         "задеплоить папку с кодом и вывести публичный URL"),
                       description=tr("Deploy a code folder to Railway and print its public URL. "
                                      "The token is read from RAILWAY_API_TOKEN.",
                                      "Задеплоить папку с кодом в Railway и вывести публичный URL. "
                                      "Токен берётся из RAILWAY_API_TOKEN."))
    d.add_argument("path", nargs="?", default=".",
                   help=tr("code folder (default: current folder)", "папка с кодом (по умолчанию текущая)"))
    d.add_argument("--name", help=tr("project and service name (default: name from small.json, "
                                     "otherwise the folder name)",
                                     "имя проекта и сервиса (по умолчанию — name из small.json, иначе имя папки)"))
    d.add_argument("--workspace", help=tr("workspace ID if you have several (or the RAILWAY_WORKSPACE_ID variable)",
                                          "ID workspace, если их несколько (или переменная RAILWAY_WORKSPACE_ID)"))
    d.add_argument("--port", type=int, help=tr("port the app listens on (by default Railway uses $PORT)",
                                               "порт, который слушает приложение (по умолчанию Railway берёт $PORT)"))
    d.add_argument("-m", "--message", help=tr("deploy message", "подпись деплоя"))
    d.add_argument("--new", action="store_true",
                   help=tr("create a new project even if %s exists", "создать новый проект, даже если есть %s")
                   % STATE_FILE)
    d.add_argument("--no-wait", action="store_true",
                   help=tr("do not wait for the build to finish", "не ждать окончания сборки"))
    d.add_argument("--timeout", type=int, default=900,
                   help=tr("how many seconds to wait for the build (default: 900)",
                           "сколько секунд ждать сборку (по умолчанию 900)"))
    d.add_argument("--interval", type=float, default=3.0, help=argparse.SUPPRESS)
    d.set_defaults(func=cmd_deploy)


def cmd_deploy(args):
    token = require_token()
    root = app_dir(args.path)

    config = load_config(root / CONFIG_FILE)
    state_path = root / STATE_FILE
    state = {} if args.new else load_state(state_path)
    reused = bool(state.get("serviceId"))
    explicit_name = args.name or config["name"]
    name = slugify(explicit_name or root.name)

    files = collect_files(str(root), skip_root_files=(CONFIG_FILE, STATE_FILE))
    if not files:
        raise CliError(tr("nothing to deploy in %s (everything filtered out by .gitignore/.railwayignore?)",
                          "в %s нечего деплоить (всё отфильтровано .gitignore/.railwayignore?)") % root)
    tarball = make_tarball(str(root), files)
    say(tr("Packed %d file (%s)" if len(files) == 1 else "Packed %d files (%s)", "Упаковано файлов: %d (%s)")
        % (len(files), human_size(len(tarball))))

    api = Railway(token)
    if reused:
        say(tr("Project %s from %s", "Проект %s из %s") % (state["name"], STATE_FILE))
        if explicit_name and name != state["name"]:
            say(tr("  Name %s not applied: the folder is already linked to %s. To link it to %s, delete %s.",
                   "  Имя %s не применено: папка уже привязана к %s. Чтобы привязать её к %s, удалите %s.")
                % (name, state["name"], name, STATE_FILE))
    else:
        workspace_id = args.workspace or os.environ.get("RAILWAY_WORKSPACE_ID") or pick_workspace(api)
        # An explicitly set name (small.json or --name) identifies the service: if the project already exists
        # in Railway (e.g. deployed from another computer), we connect to it instead of creating a duplicate.
        state = bind_existing(api, name, workspace_id) if explicit_name and not args.new else None
        if state:
            say(tr("Found project %s in Railway, linked the folder to it",
                   "Найден проект %s в Railway, папка привязана к нему") % name)
        else:
            project_id, environment_id = api.create_project(name, workspace_id)
            say(tr("Created project %s", "Создан проект %s") % name)
            service_id = api.create_service(project_id, environment_id, name)
            say(tr("Created service %s", "Создан сервис %s") % name)
            state = {"name": name, "projectId": project_id,
                     "environmentId": environment_id, "serviceId": service_id}
        # Save right away: if the build fails, a rerun won't spawn new projects.
        save_state(state_path, state)

    if not state.get("domain"):
        # Domain before the code upload: the very first build already needs its address (links in sign-in emails).
        state["domain"] = ensure_domain(api, state, args.port)
        save_state(state_path, state)
        say(tr("Public domain: %s", "Публичный домен: %s") % state["domain"])

    variables = dict(config["env"])
    signs_in = any(variables.get(name) for name in ("ACCESS_EMAILS", "GOOGLE_CLIENT_ID", "MICROSOFT_CLIENT_ID"))
    if signs_in and "PUBLIC_URL" not in variables:
        # After a domain rename Railway keeps the old address in RAILWAY_PUBLIC_DOMAIN, so sign-in links and sign-on
        # redirects would lead to a 404. That's why we pass the real address ourselves (only to Railway: the domain
        # can change).
        variables["PUBLIC_URL"] = "https://" + state["domain"]
    if variables and "OWNER_LANG" not in variables:
        variables["OWNER_LANG"] = lang()  # log lines and Telegram notifications in the owner's language
    if variables:
        # Before the code upload, so the new build gets the variables right away. Values are not printed.
        api.upsert_variables(state["projectId"], state["environmentId"], state["serviceId"], variables)
        say(tr("Variables from %s: %s%s", "Переменные из %s: %s%s")
            % (CONFIG_FILE, ", ".join(sorted(config["env"])),
               tr(" (+ PUBLIC_URL for sign-in emails)", " (+ PUBLIC_URL для писем входа)")
               if "PUBLIC_URL" not in config["env"] and "PUBLIC_URL" in variables else ""))

    try:
        up = api.upload(state["projectId"], state["environmentId"], state["serviceId"], tarball, args.message)
    except RailwayError as e:
        if reused:
            raise RailwayError(tr("%s\nIf the project was deleted in Railway, run `small deploy --new`.",
                                  "%s\nЕсли проект удалён в Railway, запустите `small deploy --new`.") % e) from None
        raise
    deployment_id = up["deploymentId"]
    say(tr("Code uploaded, deploy %s", "Код загружен, деплой %s") % deployment_id)
    if up.get("logsUrl"):
        say(tr("Logs: %s", "Логи: %s") % up["logsUrl"].rstrip("&"))

    url = "https://" + state["domain"]

    if args.no_wait:
        print(url)
        return 0

    status = wait_for_deployment(api, deployment_id, args.timeout, args.interval)
    if status not in OK_STATUSES:
        show_logs(api, deployment_id, build=(status == "FAILED"))
        raise CliError(tr("deploy finished with status %s", "деплой завершился со статусом %s") % status)
    say(tr("Done!", "Готово!"))
    print(url)
    return 0


def pick_workspace(api):
    try:
        workspaces = api.workspaces()
    except RailwayError:
        return None  # a workspace token: `me` isn't available to it, the project gets created in that same workspace
    if len(workspaces) <= 1:
        return workspaces[0]["id"] if workspaces else None
    listing = "\n".join("  %s  %s" % (w["id"], w["name"]) for w in workspaces)
    raise CliError(tr("the account has several workspaces — pick one: small deploy --workspace <id>\n",
                      "у аккаунта несколько workspace — укажите нужный: small deploy --workspace <id>\n") + listing)


def bind_existing(api, name, workspace_id):
    """Looks up a Railway project named name and returns the state to link to it, or None."""
    try:
        projects = api.find_projects(name, workspace_id)
    except RailwayError as e:
        say(tr("Could not check whether project %s exists in Railway (%s), creating a new one",
               "Не удалось проверить, есть ли проект %s в Railway (%s), создаю новый") % (name, e))
        return None
    if not projects:
        return None
    if len(projects) > 1:
        raise CliError(tr("Railway has several projects named %s (%s). Delete the extra ones or choose another name.",
                          "в Railway несколько проектов с именем %s (%s). Удалите лишние или задайте другое имя.")
                       % (name, ", ".join(p["id"] for p in projects)))
    project = projects[0]
    if not project["environments"]:
        raise CliError(tr("project %s in Railway has no environments", "у проекта %s в Railway нет окружений") % name)
    env = next((e for e in project["environments"] if e["name"] == "production"), project["environments"][0])
    services = project["services"]
    service = next((s for s in services if s["name"] == name), services[0] if len(services) == 1 else None)
    if service is None and services:
        raise CliError(tr("project %s has several services (%s), none of them is named %s",
                          "в проекте %s несколько сервисов (%s), ни один не называется %s")
                       % (name, ", ".join(s["name"] for s in services), name))
    state = {"name": name, "projectId": project["id"], "environmentId": env["id"]}
    if service is None:
        state["serviceId"] = api.create_service(project["id"], env["id"], name)
        say(tr("Created service %s", "Создан сервис %s") % name)
        return state
    state["serviceId"] = service["id"]
    domains = api.service_domains(project["id"], env["id"], service["id"])
    if domains:
        state["domain"] = domains[0]["domain"]
    return state


def ensure_domain(api, state, port):
    """Creates a *.up.railway.app domain and tries to rename it to <name>.up.railway.app."""
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
        say(tr("Could not claim %s (%s), keeping %s", "Не удалось занять %s (%s), оставляю %s") % (wanted, e, domain))
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
            say(tr("  retrying status request: %s", "  повторяю запрос статуса: %s") % e)
            status = last
        elapsed = time.monotonic() - started
        if status != last:
            say("[%s] %s" % (format_elapsed(elapsed), status))
            last = status
        if status in OK_STATUSES or status in FAILED_STATUSES:
            return status
        if elapsed > timeout:
            raise CliError(tr("build did not finish in %d s (status %s); it keeps running in Railway",
                              "сборка не завершилась за %d с (статус %s); она продолжается в Railway")
                           % (timeout, status))
        time.sleep(interval)


def show_logs(api, deployment_id, build):
    try:
        lines = api.logs(deployment_id, build=build)
    except RailwayError as e:
        say(tr("Could not fetch logs: %s", "Не удалось получить логи: %s") % e)
        return
    if lines:
        say(tr("Last lines of the %s:", "Последние строки %s:")
            % (tr("build log", "лога сборки") if build else tr("app log", "лога приложения")))
        for line in lines:
            say("  " + line)
