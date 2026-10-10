"""small gateway: sign-in in front of the app, for any app in any language, without changes to its code.

on: a <app>-gateway service (Caddy and magic_link) takes the app's address and the sign-in variables; the app keeps
running without a public address, reachable only through the gateway over Railway's private network. Every request,
websockets included, is checked by the gateway; the app learns who is signed in from the X-Small-User header.
off: the address and the variables go back to the app, and the gateway is deleted.
"""

import argparse
import time

from .. import access as tokens
from .. import gateway
from ..common import CliError, app_dir, confirm, linked_state, require_token, say
from ..config import STATE_FILE, save_state
from ..i18n import tr
from ..railway import Railway, RailwayError
from .deploy import FAILED_STATUSES, show_logs, wait_for_deployment



def register(sub):
    g = sub.add_parser("gateway", help=tr("sign-in in front of any app, no code changes: on / off / status",
                                          "вход перед любым приложением, без правок кода: on / off / статус"),
                       description=tr("The gateway: a separate service with the app's address that checks every "
                                      "request (sign-in with Google, Microsoft, email links, invite links) and passes "
                                      "the ones it lets in to the app over Railway's private network. The app keeps "
                                      "running without a public address and needs no sign-in code of its own; "
                                      "websockets are checked too. on turns it on, off turns it off, with no "
                                      "subcommand you get the status.",
                                      "Шлюз: отдельный сервис с адресом приложения, который проверяет каждый запрос "
                                      "(вход через Google, Microsoft, по ссылке на почту и личной ссылке) и пропускает "
                                      "к приложению по внутренней сети Railway. Приложение работает без публичного "
                                      "адреса, и своего кода входа ему не нужно; websocket тоже проверяются. on — "
                                      "включить, off — выключить, без подкоманды — статус."))
    g.add_argument("-C", "--path", default=".", help=tr("app folder (default: current folder)",
                                                        "папка приложения (по умолчанию текущая)"))
    g.set_defaults(func=cmd_gateway_status)
    gateway_sub = g.add_subparsers(dest="gateway_command", metavar="<on|off>")
    gon = gateway_sub.add_parser("on", help=tr("put the gateway in front of the app", "поставить шлюз перед приложением"))
    gon.add_argument("--port", type=int, help=tr("the port the app listens on (default: $PORT, which small sets to "
                                                 "%s)", "порт, который слушает приложение (по умолчанию $PORT, small "
                                                        "задаёт %s)") % gateway.DEFAULT_PORT)
    gon.add_argument("--timeout", type=int, default=900, help=argparse.SUPPRESS)
    gon.add_argument("--interval", type=float, default=3.0, help=argparse.SUPPRESS)
    gon.set_defaults(func=cmd_gateway_on)
    goff = gateway_sub.add_parser("off", help=tr("remove the gateway: the address goes back to the app",
                                                 "убрать шлюз: адрес возвращается приложению"))
    goff.add_argument("--yes", action="store_true", help=tr("don't ask, even if the app has no sign-in of its own",
                                                            "не спрашивать, даже если у приложения нет своего входа"))
    goff.set_defaults(func=cmd_gateway_off)
    for parser in (gon, goff):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help=tr("app folder", "папка приложения"))


def code_problem(root):
    """None if the app can sit behind the gateway; otherwise why not. An app without magic_link always can. One with
    magic_link needs a version that trusts the gateway (small 0.9), or it would keep asking people to sign in."""
    if tokens.is_streamlit(root) and (root / "magic_link.py").is_file():
        if "behind_gateway" not in (root / "app.py").read_text(encoding="utf-8", errors="replace"):
            return (tr("app.py is from a streamlit template older than small 0.9, which doesn't know the gateway. "
                       "Take require_access() from the new app.py, and magic_link.py next to it: %s",
                       "app.py — из шаблона streamlit старше small 0.9, он не знает шлюза. Возьмите require_access() "
                       "из нового app.py и magic_link.py рядом с ним: %s") % (tokens.TEMPLATES_DIR / "streamlit"))
    for name, template in (("magic_link.py", "streamlit" if tokens.is_streamlit(root) else "python-http"),
                           ("magic-link.js", "node-http")):
        path = root / name
        if path.is_file() and "SMALL_GATEWAY_KEY" not in path.read_text(encoding="utf-8", errors="replace"):
            return (tr("%s in the app is older than small 0.9 and doesn't know the gateway: behind it, people would "
                       "still be asked to sign in. Replace it with the new one: %s",
                       "%s в приложении старше small 0.9 и не знает шлюза: за ним людей всё равно просили бы войти. "
                       "Замените его новым: %s") % (name, tokens.TEMPLATES_DIR / template / name))
    return None


def create(api, root, state, env, port=None):
    """Creates <app>-gateway for the app (state gets "gateway"), with its variables: the sign-in ones from env, where
    the app is (UPSTREAM), its key and port. The app gets the key as a reference and, if needed, a known PORT. Code is
    not uploaded and addresses don't move here. Returns (the gateway's service ID, the names of the variables given
    to the app)."""
    pid, eid, sid = state["projectId"], state["environmentId"], state["serviceId"]
    _, services = api.project_services(pid)
    app_name = next((s["name"] for s in services if s["id"] == sid), state["name"])
    name = app_name + "-gateway"
    if any(s["name"] == name for s in services):
        raise CliError(tr("the project already has a service named %s: delete or rename it in Railway",
                          "в проекте уже есть сервис %s: удалите или переименуйте его в Railway") % name)
    app_port = str(port or env.get("PORT") or gateway.DEFAULT_PORT)
    app_vars = {"SMALL_GATEWAY_KEY": gateway.key_reference(name)}
    if not port and not env.get("PORT"):
        app_vars["PORT"] = app_port  # the app must listen on a known port: the gateway reaches it there
    gateway_vars = {k: v for k, v in env.items() if k in gateway.GATEWAY_ONLY + gateway.SHARED and v}
    gateway_vars.update(UPSTREAM=gateway.upstream(app_name, app_port), SMALL_GATEWAY_KEY=tokens.new_token(),
                        PORT=str(gateway.GATEWAY_PORT))
    if state.get("domain"):
        gateway_vars["PUBLIC_URL"] = tokens.app_url(root, state)
    if not gateway_vars.get("SESSION_SECRET"):
        gateway_vars["SESSION_SECRET"] = tokens.new_token()
    say(tr("Creating the gateway %s…", "Создаю шлюз %s…") % name)
    gateway_id = api.create_service(pid, eid, name)
    state["gateway"] = {"serviceId": gateway_id, "name": name}
    api.upsert_variables(pid, eid, gateway_id, gateway_vars)
    api.upsert_variables(pid, eid, sid, app_vars)
    return gateway_id, list(app_vars)


def move_domain(api, state, from_service, to_service, port):
    """Moves the app's *.up.railway.app address to another service, keeping its name when Railway lets it go in
    time. Returns the address the service has now."""
    project_id, environment_id, wanted = state["projectId"], state["environmentId"], state["domain"]
    for domain in api.service_domains(project_id, environment_id, from_service):
        api.delete_domain(domain["id"])
    domain_id, domain = api.create_domain(to_service, environment_id, port)
    for _ in range(10):  # the freed name may take a few seconds to become available again
        if domain == wanted:
            break
        try:
            if api.domain_available(wanted):
                api.rename_domain(domain_id, to_service, environment_id, wanted, port)
                domain = api.service_domain(project_id, environment_id, to_service, domain_id) or wanted
                continue
        except RailwayError:
            pass
        time.sleep(3)
    return domain


def cmd_gateway_status(args):
    root = app_dir(args.path)
    state = linked_state(root)
    if not gateway.is_on(state):
        say(tr("No gateway: the app has the public address itself%s. Put one in front: small gateway on",
               "Шлюза нет: публичный адрес у самого приложения%s. Поставить: small gateway on")
            % ("" if gateway.has_own_sign_in(root) else tr(", and it has no sign-in of its own",
                                                    ", и своего входа у него нет")))
        print("off")
        return 0
    api = Railway(require_token())
    deployment = api.latest_deployment(state["gateway"]["serviceId"], state["environmentId"])
    say(tr("Gateway %s in front of the app: https://%s (deploy: %s, small %s)",
           "Шлюз %s перед приложением: https://%s (деплой: %s, small %s)")
        % (state["gateway"]["name"], state["domain"], deployment["status"] if deployment else "—",
           state["gateway"].get("version", "?")))
    print("on")
    return 0


def cmd_gateway_on(args):
    root = app_dir(args.path)
    state = linked_state(root)
    if gateway.is_on(state):
        say(tr("The gateway is already on: https://%s", "Шлюз уже включён: https://%s") % state.get("domain", "—"))
        print("on")
        return 0
    if not state.get("domain"):
        raise CliError(tr("the app has no address yet: run small deploy first", "у приложения ещё нет адреса: сначала small deploy"))
    problem = code_problem(root)
    if problem:
        raise CliError(problem)
    api = Railway(require_token())
    pid, eid, sid = state["projectId"], state["environmentId"], state["serviceId"]
    if api.custom_domains(pid, eid, sid):
        raise CliError(tr("the app has a custom domain. Remove it first (small domain remove DOMAIN), turn the gateway "
                          "on, then add it again: small domain add DOMAIN — it will point to the gateway",
                          "у приложения есть свой домен. Сначала уберите его (small domain remove ДОМЕН), включите "
                          "шлюз и добавьте снова: small domain add ДОМЕН — он будет указывать на шлюз"))
    env, _ = tokens.app_env(root, state, warn=say)
    gateway_id, app_vars = create(api, root, state, env, args.port)
    try:
        if not gateway.wait_for_private_address(api, state, interval=args.interval or 0.1):
            raise CliError(tr("Railway hasn't given the app a private address: deploy it first (small deploy)",
                              "Railway не выдал приложению внутренний адрес: сначала задеплойте его (small deploy)"))
        deployment = gateway.upload(api, state)
        status = wait_for_deployment(api, deployment, args.timeout, args.interval)
        if status in FAILED_STATUSES:
            show_logs(api, deployment, build=(status == "FAILED"))
            raise CliError(tr("the gateway didn't start (%s); the app is unchanged",
                              "шлюз не запустился (%s); приложение не изменилось") % status)
    except (CliError, RailwayError, KeyboardInterrupt):
        try:
            api.delete_service(gateway_id)
            for variable in app_vars:  # the app goes back to how it was
                api.delete_variable(pid, eid, sid, variable)
        except RailwayError:
            say(tr("Could not delete the unfinished gateway %s: delete it in Railway", "Не удалось удалить "
                   "недостроенный шлюз %s: удалите его в Railway") % state["gateway"]["name"])
        raise
    save_state(root / STATE_FILE, state)  # from here on the gateway is part of the app

    # The sign-in variables now belong to the gateway.
    existing = api.service_variables(pid, eid, sid)
    for variable in gateway.GATEWAY_ONLY:
        if variable in existing:
            api.delete_variable(pid, eid, sid, variable)
    say(tr("Moving the address %s to the gateway…", "Переношу адрес %s на шлюз…") % state["domain"])
    domain = move_domain(api, state, sid, gateway_id, gateway.GATEWAY_PORT)
    if domain != state["domain"]:
        say(tr("Railway didn't let the old name go in time: the app's address is now https://%s",
               "Railway не успел освободить прежнее имя: теперь адрес приложения https://%s") % domain)
        state["domain"] = domain
        api.upsert_variables(pid, eid, gateway_id, {"PUBLIC_URL": "https://" + domain})
    save_state(root / STATE_FILE, state)
    api.redeploy(sid, eid)
    say(tr("The gateway is on. Every request to https://%s goes through sign-in first; the app itself has no public "
           "address now and learns who is signed in from the X-Small-User header. Railway is restarting the app.",
           "Шлюз включён. Каждый запрос к https://%s сначала проходит вход; у самого приложения больше нет публичного "
           "адреса, а кто вошёл, оно узнаёт из заголовка X-Small-User. Railway перезапускает приложение.")
        % state["domain"])
    print("https://" + state["domain"])
    return 0


def cmd_gateway_off(args):
    root = app_dir(args.path)
    state = linked_state(root)
    if not gateway.is_on(state):
        say(tr("The app has no gateway.", "Шлюза у приложения нет."))
        print("off")
        return 0
    if not gateway.has_own_sign_in(root) and not args.yes:
        say(tr("The app has no sign-in of its own: without the gateway anyone with its address gets in.",
               "Своего входа у приложения нет: без шлюза в него попадёт любой, у кого есть адрес."))
        if not confirm(tr("Remove the gateway anyway? [y/N] ", "Всё равно убрать шлюз? [y/N] ")):
            raise CliError(tr("nothing changed (to go ahead without asking: small gateway off --yes)",
                              "ничего не изменено (без вопроса: small gateway off --yes)"))
    api = Railway(require_token())
    pid, eid, sid, gateway_id = (state["projectId"], state["environmentId"], state["serviceId"],
                                 state["gateway"]["serviceId"])
    ours = api.service_variables(pid, eid, gateway_id)
    back = {k: v for k, v in ours.items() if k in gateway.GATEWAY_ONLY + gateway.SHARED and v}
    if back:
        api.upsert_variables(pid, eid, sid, back)
    if "SMALL_GATEWAY_KEY" in api.service_variables(pid, eid, sid):
        api.delete_variable(pid, eid, sid, "SMALL_GATEWAY_KEY")
    say(tr("Moving the address %s back to the app…", "Возвращаю адрес %s приложению…") % state["domain"])
    domain = move_domain(api, state, gateway_id, sid, None)
    api.delete_service(gateway_id)
    if domain != state["domain"]:
        say(tr("Railway didn't let the old name go in time: the app's address is now https://%s",
               "Railway не успел освободить прежнее имя: теперь адрес приложения https://%s") % domain)
        state["domain"] = domain
        api.upsert_variables(pid, eid, sid, {"PUBLIC_URL": "https://" + domain})
    state.pop("gateway")
    save_state(root / STATE_FILE, state)
    api.redeploy(sid, eid)
    say(tr("The gateway is removed: the app has its address back (https://%s), Railway is restarting it.",
           "Шлюз убран: адрес снова у приложения (https://%s), Railway его перезапускает.") % state["domain"])
    print("https://" + state["domain"])
    return 0


