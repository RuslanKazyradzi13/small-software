"""small env: the app's variables. set/unset change them right away in both Railway and small.json; otherwise the
next small deploy would bring back the old values from small.json."""

import argparse
import json

from .. import gateway
from ..common import CliError, app_dir, is_linked, require_token, say
from ..config import CONFIG_FILE, ENV_NAME_RE, STATE_FILE, load_config, load_state
from ..i18n import tr
from ..railway import Railway, token_from_env


def register(sub):
    e = sub.add_parser("env", help=tr("app environment variables: list, set, unset",
                                      "переменные приложения: список, задать, удалить"),
                       description=tr("App environment variables. set/unset change them in both Railway and %s "
                                      "at once (otherwise the next small deploy would bring back the old values), "
                                      "then Railway restarts the app. With no subcommand, lists them.",
                                      "Переменные приложения. set/unset меняют их сразу и в Railway, и в %s "
                                      "(иначе следующий small deploy вернул бы старые значения), затем Railway "
                                      "перезапускает приложение. Без подкоманды — список.") % CONFIG_FILE)
    e.add_argument("-C", "--path", default=".", help=tr("app folder (default: current folder)",
                                                        "папка приложения (по умолчанию текущая)"))
    e.set_defaults(func=cmd_env_list, values=False)
    env_sub = e.add_subparsers(dest="env_command", metavar="<list|set|unset>")
    el = env_sub.add_parser("list", help=tr("list variables (values hidden)", "показать переменные (значения скрыты)"))
    el.add_argument("--values", action="store_true", help=tr("show values", "показать значения"))
    el.set_defaults(func=cmd_env_list)
    es = env_sub.add_parser("set", help=tr("set variables: small env set KEY=VALUE ...",
                                           "задать переменные: small env set KEY=VALUE ..."))
    es.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    es.set_defaults(func=cmd_env_set)
    eu = env_sub.add_parser("unset", help=tr("remove variables: small env unset KEY ...",
                                             "удалить переменные: small env unset KEY ..."))
    eu.add_argument("names", nargs="+", metavar="KEY")
    eu.set_defaults(func=cmd_env_unset)
    for parser in (el, es, eu):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help=tr("app folder", "папка приложения"))
    for parser in (es, eu):
        add_no_deploy(parser)


def add_no_deploy(parser):
    parser.add_argument("--no-deploy", action="store_true",
                        help=tr("don't restart the app (changes take effect on the next deploy)",
                                "не перезапускать приложение (изменения вступят в силу при следующем деплое)"))


def change_env(root, set_vars=None, unset=(), deploy=True, railway_only=None):
    """Changes variables in Railway (if the app is deployed) and in small.json, so that they don't diverge.

    Railway first: if it refuses, small.json stays as it was. railway_only are variables that small manages
    itself and doesn't write to small.json (e.g. PUBLIC_URL). Behind the gateway, each variable goes to its service
    (sign-in variables to the gateway, see gateway.services_for), and only the services that changed restart.
    Returns True if Railway was touched.
    """
    set_vars, unset = dict(set_vars or {}), list(unset)
    config_path, state = root / CONFIG_FILE, load_state(root / STATE_FILE)
    load_config(config_path)  # a broken small.json is an error before any changes
    raw = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.is_file() else {}

    if is_linked(state):
        api = Railway(require_token())
        services = [state["serviceId"]] + ([state["gateway"]["serviceId"]] if gateway.is_on(state) else [])
        changed = []
        for service_id in services:
            mine = {k: v for k, v in set_vars.items() if service_id in gateway.services_for(k, state)}
            if service_id == gateway.public_service(state):
                mine.update(railway_only or {})  # small's own (the public address): for the service that has it
            gone = [name for name in unset if service_id in gateway.services_for(name, state)]
            if len(services) > 1 and not (mine or gone):
                continue
            ids = (state["projectId"], state["environmentId"], service_id)
            existing = api.service_variables(*ids)
            if mine:
                api.upsert_variables(*ids, mine)
            for name in gone:
                if name in existing:
                    api.delete_variable(*ids, name)
            changed.append(service_id)
        if deploy:
            for service_id in changed or [state["serviceId"]]:
                api.redeploy(service_id, state["environmentId"])

    env = raw.setdefault("env", {})
    env.update(set_vars)
    for name in unset:
        env.pop(name, None)
    config_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return is_linked(state)


def report_env_change(linked, deploy, what):
    if not linked:
        say(tr("%s. Saved to %s; the app is not deployed yet, Railway gets it on small deploy.",
               "%s. Записано в %s; приложение ещё не задеплоено — в Railway попадёт при small deploy.")
            % (what, CONFIG_FILE))
    elif deploy:
        say(tr("%s. Updated in Railway and %s; Railway is restarting the app (follow with small logs -f).",
               "%s. Обновлено в Railway и %s, Railway перезапускает приложение (small logs -f — следить).")
            % (what, CONFIG_FILE))
    else:
        say(tr("%s. Updated in Railway and %s; takes effect on the next deploy.",
               "%s. Обновлено в Railway и %s, вступит в силу при следующем деплое.") % (what, CONFIG_FILE))


def cmd_env_list(args):
    root = app_dir(args.path)
    config_env = load_config(root / CONFIG_FILE)["env"]
    state = load_state(root / STATE_FILE)
    railway_env = None
    if is_linked(state):
        if token_from_env():
            # unrendered: the ${{Postgres.DATABASE_URL}} reference from small.json matches the one stored in Railway,
            # and the database password doesn't reach the terminal even with --values.
            railway_env = {k: v for k, v in Railway(token_from_env()).service_variables(
                state["projectId"], state["environmentId"], state["serviceId"], unrendered=True).items()
                if not k.startswith("RAILWAY_")}
        else:
            say(tr("No RAILWAY_API_TOKEN, showing %s only", "Нет RAILWAY_API_TOKEN — показываю только %s")
                % CONFIG_FILE)
    names = sorted(set(config_env) | set(railway_env or {}))
    if not names:
        say(tr("No variables. Set one: small env set KEY=VALUE", "Переменных нет. Задать: small env set KEY=VALUE"))
        return 0
    width = max(len(n) for n in names) + 2
    for name in names:
        in_config, in_railway = name in config_env, railway_env is not None and name in railway_env
        value = railway_env[name] if in_railway else config_env[name]
        if in_config and in_railway:
            where = CONFIG_FILE + " + Railway" if config_env[name] == railway_env[name] else "Railway ≠ " + CONFIG_FILE
        elif in_railway:
            where = tr("Railway only", "только Railway")
        else:
            where = CONFIG_FILE + (tr(" (in Railway after small deploy)", " (в Railway — после small deploy)")
                                   if railway_env is not None else "")
        shown = value if getattr(args, "values", False) else "•" * min(len(value), 8) + " (%d)" % len(value)
        print("%-*s%-24s %s" % (width, name, shown, where))
    if not getattr(args, "values", False):
        say(tr("Values hidden: small env list --values", "Значения скрыты: small env list --values"))
    return 0


def cmd_env_set(args):
    root = app_dir(args.path)
    pairs = {}
    for pair in args.pairs:
        name, sep, value = pair.partition("=")
        if not sep or not ENV_NAME_RE.match(name):
            raise CliError(tr("expected KEY=VALUE (KEY: Latin letters, digits, _; must not start with a digit), "
                              "got: %s",
                              "ожидается KEY=VALUE (KEY — латиница, цифры, _; не с цифры), получено: %s") % pair)
        pairs[name] = value
    linked = change_env(root, set_vars=pairs, deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, tr("Set: ", "Заданы: ") + ", ".join(sorted(pairs)))
    return 0


def cmd_env_unset(args):
    root = app_dir(args.path)
    linked = change_env(root, unset=args.names, deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, tr("Removed: ", "Удалены: ") + ", ".join(args.names))
    return 0
