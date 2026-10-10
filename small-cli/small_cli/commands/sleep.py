"""small sleep: Railway's serverless mode — with no requests the app falls asleep and uses no resources,
the first request wakes it up (a few seconds of delay)."""

import argparse

from ..common import app_dir, linked_state, require_token, say
from ..i18n import tr
from ..railway import Railway


def register(sub):
    s = sub.add_parser("sleep", help=tr("sleep when idle to save cost: on / off / status",
                                        "засыпать без запросов (экономия): on / off / статус"),
                       description=tr("Railway sleep mode (serverless): with no incoming requests the app sleeps "
                                      "and uses no CPU or memory; the first request wakes it up in a few seconds. "
                                      "Handy for apps that are opened a couple of times a day.",
                                      "Режим сна Railway (serverless): без входящих запросов приложение засыпает и "
                                      "не расходует CPU и память; первый запрос будит его за несколько секунд. "
                                      "Удобно для приложений, которые открывают пару раз в день."))
    s.add_argument("mode", nargs="?", choices=("on", "off"),
                   help=tr("turn on or off; with no argument, shows the status",
                           "включить или выключить; без аргумента — статус"))
    s.add_argument("-C", "--path", default=".", help=tr("app folder (default: current folder)",
                                                        "папка приложения (по умолчанию текущая)"))
    s.add_argument("--no-deploy", action="store_true",
                   help=tr("don't restart the app (the setting takes effect on the next deploy)",
                           "не перезапускать приложение (настройка вступит в силу при следующем деплое)"))
    s.set_defaults(func=cmd_sleep)


def cmd_sleep(args):
    root = app_dir(args.path)
    state = linked_state(root)
    api = Railway(require_token())
    sid, eid = state["serviceId"], state["environmentId"]
    current = api.instance_status(sid, eid)
    if args.mode is None:
        deployment = current["deployment"]
        say(tr("Sleep mode: %s%s", "Режим сна: %s%s") % (
            tr("on, the app sleeps when there are no requests", "включён — без запросов приложение засыпает")
            if current["sleep"] else
            tr("off, the app is always running", "выключен — приложение работает всегда"),
            tr("; now: %s", "; сейчас: %s") % deployment["status"] if deployment else ""))
        print("on" if current["sleep"] else "off")
        return 0
    wanted = args.mode == "on"
    if current["sleep"] == wanted:
        say(tr("Sleep mode is already %s.", "Режим сна уже %s.")
            % (tr("on", "включён") if wanted else tr("off", "выключен")))
        return 0
    api.update_instance(sid, eid, sleepApplication=wanted)
    if not args.no_deploy:
        api.redeploy(sid, eid)
    say(tr("Sleep mode turned %s%s.", "Режим сна %s%s.") % (
        tr("on", "включён") if wanted else tr("off", "выключен"),
        tr(", Railway is restarting the app", ", Railway перезапускает приложение") if not args.no_deploy else
        tr("; takes effect on the next deploy", "; вступит в силу при следующем деплое")))
    if wanted:
        say(tr("The first request after idle time takes a few seconds longer while the app wakes up.",
               "Первый запрос после простоя будет на несколько секунд дольше: приложение просыпается."))
    return 0
