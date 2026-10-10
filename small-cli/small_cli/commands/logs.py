"""small logs: the log of the latest deploy (app or build), with a -f follow mode."""

import argparse
import time

from .. import gateway
from ..common import CliError, app_dir, linked_state, log_time, require_token, say
from ..i18n import tr
from ..railway import Railway, RailwayError


def register(sub):
    g = sub.add_parser("logs", help=tr("app logs from Railway", "логи приложения из Railway"),
                       description=tr("Show the log of the latest deploy: the app log (default) or the build log.",
                                      "Показать лог последнего деплоя: приложения (по умолчанию) или сборки."))
    g.add_argument("path", nargs="?", default=".", help=tr("app folder (default: current folder)",
                                                           "папка приложения (по умолчанию текущая)"))
    g.add_argument("--build", action="store_true", help=tr("build log instead of the app log",
                                                           "лог сборки вместо лога приложения"))
    g.add_argument("-n", "--lines", type=int, default=100, help=tr("number of lines (default 100)",
                                                                   "сколько строк (по умолчанию 100)"))
    g.add_argument("-f", "--follow", action="store_true", help=tr("follow new lines (Ctrl+C to exit)",
                                                                  "следить за новыми строками (Ctrl+C — выход)"))
    g.add_argument("--gateway", action="store_true", help=tr("the gateway's log (small gateway on)",
                                                             "лог шлюза (small gateway on)"))
    g.add_argument("--interval", type=float, default=2.0, help=argparse.SUPPRESS)
    g.set_defaults(func=cmd_logs)


def cmd_logs(args):
    root = app_dir(args.path)
    state = linked_state(root)
    api = Railway(require_token())
    if args.gateway and not gateway.is_on(state):
        raise CliError(tr("the app has no gateway (small gateway on)", "у приложения нет шлюза (small gateway on)"))
    service_id = state["gateway"]["serviceId"] if args.gateway else state["serviceId"]
    deployment = api.latest_deployment(service_id, state["environmentId"])
    if not deployment:
        raise CliError(tr("service %s has no deploys yet", "у сервиса %s ещё нет деплоев") % state.get("name", ""))
    say(tr("%s log · deploy %s · %s", "Лог %s · деплой %s · %s")
        % (tr("Build", "сборки") if args.build else tr("App", "приложения"), deployment["id"], deployment["status"]))

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
    say(tr("Following new lines, Ctrl+C to exit", "Слежу за новыми строками, Ctrl+C — выход"))
    try:
        while True:
            time.sleep(args.interval)
            try:
                show(api.log_entries(deployment["id"], build=args.build, limit=args.lines, start_date=last["ts"]))
            except RailwayError as e:
                say(tr("  retrying: %s", "  повторяю запрос: %s") % e)
    except KeyboardInterrupt:
        return 0
