"""small notify: уведомления о запросах доступа в Telegram.

Кнопка «Запросить доступ» на экране 403 отправляет запрос в приложение; если у приложения заданы
TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID, middleware пересылает его владельцу в Telegram. Эта команда
проверяет токен бота, находит ID чата, шлёт пробное сообщение и записывает обе переменные.
"""

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request

from ..common import CliError, app_dir, say
from ..config import CONFIG_FILE, STATE_FILE, load_config, load_state
from .env import add_no_deploy, change_env, report_env_change

TELEGRAM_API = "https://api.telegram.org"
CHAT_WAIT_SECONDS = 120


def register(sub):
    n = sub.add_parser("notify", help="уведомления о запросах доступа в Telegram",
                       description="Запросы с кнопки «Запросить доступ» приходят владельцу в Telegram. "
                                   "telegram — подключить бота (токен из TELEGRAM_BOT_TOKEN или ввод без эха), "
                                   "off — отключить, без подкоманды — статус.")
    n.add_argument("-C", "--path", default=".", help="папка приложения (по умолчанию текущая)")
    n.set_defaults(func=cmd_notify_status)
    notify_sub = n.add_subparsers(dest="notify_command", metavar="<telegram|off>")
    nt = notify_sub.add_parser("telegram", help="подключить Telegram-бота")
    nt.add_argument("--chat-id", help="ID чата, если он известен (иначе small найдёт его сам)")
    nt.add_argument("--wait", type=int, default=CHAT_WAIT_SECONDS, help=argparse.SUPPRESS)
    nt.set_defaults(func=cmd_notify_telegram)
    no = notify_sub.add_parser("off", help="отключить уведомления")
    no.set_defaults(func=cmd_notify_off)
    for parser in (nt, no):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help="папка приложения")
        add_no_deploy(parser)


def telegram(token, method, params=None):
    url = "%s/bot%s/%s" % (os.environ.get("TELEGRAM_API_URL", TELEGRAM_API).rstrip("/"), token, method)
    req = urllib.request.Request(url, data=json.dumps(params or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {"description": "HTTP %d" % e.code}
    except OSError as e:
        raise CliError("нет связи с Telegram: %s" % getattr(e, "reason", e)) from None
    if not payload.get("ok"):
        raise CliError("Telegram: %s" % payload.get("description", "неизвестная ошибка"))
    return payload["result"]


def bot_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        return token
    if not sys.stdin or not sys.stdin.isatty():
        raise CliError("задайте TELEGRAM_BOT_TOKEN (токен от @BotFather) или запустите команду в терминале")
    say("Токен бота от @BotFather (создать: /newbot). Ввод не отображается.")
    token = getpass.getpass("TELEGRAM_BOT_TOKEN: ").strip()
    if not token:
        raise CliError("токен не введён")
    return token


def find_chat(token, username, wait):
    """ID чата из последнего сообщения боту; если сообщений нет — просим написать и ждём."""
    deadline, offset, asked = time.monotonic() + wait, None, False
    while True:
        updates = telegram(token, "getUpdates", {"timeout": 0, **({"offset": offset} if offset else {})})
        chats = [u["message"]["chat"] for u in updates if u.get("message", {}).get("chat")]
        if chats:
            return chats[-1]
        if updates:
            offset = updates[-1]["update_id"] + 1
        if not asked:
            say("Напишите боту @%s любое сообщение в Telegram — жду до %d с…" % (username, wait))
            asked = True
        if time.monotonic() > deadline:
            raise CliError("боту никто не написал за %d с. Напишите @%s и повторите, или передайте --chat-id"
                           % (wait, username))
        time.sleep(2)


def cmd_notify_telegram(args):
    root = app_dir(args.path)
    token = bot_token()
    username = telegram(token, "getMe")["username"]
    if args.chat_id:
        chat_id, who = args.chat_id, args.chat_id
    else:
        chat = find_chat(token, username, args.wait)
        chat_id = chat["id"]
        who = chat.get("title") or " ".join(filter(None, (chat.get("first_name"), chat.get("last_name")))) or chat_id
    app = load_config(root / CONFIG_FILE)["name"] or load_state(root / STATE_FILE).get("name") or root.name
    telegram(token, "sendMessage", {"chat_id": chat_id, "text": "✅ small: сюда будут приходить запросы доступа к %s "
                                                              "с кнопки «Запросить доступ»." % app})
    linked = change_env(root, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": str(chat_id)},
                        deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "Уведомления через @%s → %s включены" % (username, who))
    return 0


def cmd_notify_off(args):
    root = app_dir(args.path)
    linked = change_env(root, unset=["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"], deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "Уведомления в Telegram отключены")
    return 0


def cmd_notify_status(args):
    env = load_config(app_dir(args.path) / CONFIG_FILE)["env"]
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        say("Уведомления о запросах доступа: Telegram, чат %s" % env["TELEGRAM_CHAT_ID"])
        print("telegram")
    else:
        say("Уведомления выключены: запросы доступа видны только в логе (small logs). Включить: small notify telegram")
        print("off")
    return 0
