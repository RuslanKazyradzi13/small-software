"""small mail: чем приложение отправляет письма для входа по почте (small access add ivan@company.com).

resend — Resend по HTTPS (работает на Railway Hobby); smtp — свой почтовый сервер (на Railway SMTP открыт только
с тарифа Pro); log — ссылки для входа пишутся в лог приложения (для проверки); off — выключить.
Перед сохранением resend и smtp отправляют пробное письмо тем же кодом, что и приложение.
"""

import argparse
import getpass
import importlib.util
import os
import sys
from contextlib import contextmanager
from urllib.parse import urlsplit

from .. import access as tokens
from ..common import CliError, app_dir, ask, say
from ..config import CONFIG_FILE, STATE_FILE, load_config, load_state
from ..scaffold import TEMPLATES_DIR
from .env import add_no_deploy, change_env, report_env_change

ALL_VARS = ("RESEND_API_KEY", "SMTP_URL", "LOGIN_LINKS_TO_LOG", "MAIL_FROM")


def register(sub):
    m = sub.add_parser("mail", help="письма для входа по почте: resend, smtp, log, off",
                       description="Чем отправлять письма со ссылкой для входа. resend — ключ из RESEND_API_KEY "
                                   "или ввод без эха; smtp — адрес сервера из SMTP_URL или ввод без эха; "
                                   "log — писать ссылки в лог (для проверки); off — выключить; без подкоманды — статус.")
    m.add_argument("-C", "--path", default=".", help="папка приложения (по умолчанию текущая)")
    m.set_defaults(func=cmd_mail_status)
    mail_sub = m.add_subparsers(dest="mail_command", metavar="<resend|smtp|log|off>")
    mr = mail_sub.add_parser("resend", help="отправлять через Resend (HTTPS)")
    ms = mail_sub.add_parser("smtp", help="отправлять через свой SMTP-сервер")
    for parser in (mr, ms):
        parser.add_argument("--from", dest="sender", help='отправитель: "Отчёты <noreply@company.com>"')
        parser.add_argument("--to", help="куда отправить пробное письмо (для Resend без своего домена — "
                                         "адрес вашего аккаунта Resend)")
    mr.set_defaults(func=cmd_mail_resend)
    ms.set_defaults(func=cmd_mail_smtp)
    ml = mail_sub.add_parser("log", help="писать ссылки для входа в лог приложения (только для проверки)")
    ml.set_defaults(func=cmd_mail_log)
    mo = mail_sub.add_parser("off", help="выключить отправку писем")
    mo.set_defaults(func=cmd_mail_off)
    for parser in (mr, ms, ml, mo):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help="папка приложения")
        add_no_deploy(parser)


def secret_input(name, prompt):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if not sys.stdin or not sys.stdin.isatty():
        raise CliError("задайте %s или запустите команду в терминале" % name)
    say(prompt + " Ввод не отображается.")
    value = getpass.getpass("%s: " % name).strip()
    if not value:
        raise CliError("%s не введён" % name)
    return value


def mailer():
    """magic_link.py из шаблона: письмо уходит тем же кодом, что и из приложения."""
    path = TEMPLATES_DIR / "python-http" / "magic_link.py"
    spec = importlib.util.spec_from_file_location("small_magic_link", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def mail_env(values):
    saved = {name: os.environ.get(name) for name in ALL_VARS}
    try:
        for name in ALL_VARS:
            os.environ.pop(name, None)
        os.environ.update({k: v for k, v in values.items() if v})
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_letter(root, values, to):
    to = (to or ask("Куда отправить пробное письмо: ") or "").strip()
    if not tokens.EMAIL_RE.match(to):
        raise CliError("укажите адрес для пробного письма: --to ivan@company.com")
    app = load_config(root / CONFIG_FILE)["name"] or load_state(root / STATE_FILE).get("name") or root.name
    module = mailer()
    with mail_env(values):
        try:
            module.send_mail(to, "small: проверка почты для %s" % app,
                             "Письма для входа в %s будут приходить так же, как это.\n" % app,
                             "<p>Письма для входа в <b>%s</b> будут приходить так же, как это.</p>" % app)
        except Exception as e:  # noqa: BLE001 — показываем причину, но не ключ
            hint = ""
            if "RESEND_API_KEY" in values and "403" in str(getattr(e, "code", "")):
                hint = (" Без подтверждённого домена Resend шлёт только на адрес вашего аккаунта; "
                        "подтвердите домен в Resend и укажите --from.")
            raise CliError("пробное письмо не ушло: %s.%s" % (module._describe(e), hint)) from None
    say("Пробное письмо отправлено на %s — проверьте почту (и папку «Спам»)." % to)


def cmd_mail_resend(args):
    root = app_dir(args.path)
    key = secret_input("RESEND_API_KEY", "Ключ API из resend.com → API Keys.")
    values = {"RESEND_API_KEY": key, "MAIL_FROM": args.sender or ""}
    test_letter(root, values, args.to)
    linked = change_env(root, {k: v for k, v in values.items() if v},
                        unset=["SMTP_URL", "LOGIN_LINKS_TO_LOG"] + ([] if args.sender else ["MAIL_FROM"]),
                        deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "Письма для входа отправляются через Resend")
    return 0


def cmd_mail_smtp(args):
    root = app_dir(args.path)
    url = secret_input("SMTP_URL", "Адрес сервера: smtp://логин:пароль@сервер:587 или smtps://…:465.")
    parts = urlsplit(url)
    if parts.scheme not in ("smtp", "smtps") or not parts.hostname:
        raise CliError("SMTP_URL должен выглядеть так: smtp://логин:пароль@сервер:587 (или smtps://…:465)")
    values = {"SMTP_URL": url, "MAIL_FROM": args.sender or ""}
    test_letter(root, values, args.to)
    linked = change_env(root, {k: v for k, v in values.items() if v},
                        unset=["RESEND_API_KEY", "LOGIN_LINKS_TO_LOG"] + ([] if args.sender else ["MAIL_FROM"]),
                        deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "Письма для входа отправляются через %s" % parts.hostname)
    say("На Railway SMTP открыт только с тарифа Pro; на Hobby используйте small mail resend.")
    return 0


def cmd_mail_log(args):
    root = app_dir(args.path)
    linked = change_env(root, {"LOGIN_LINKS_TO_LOG": "1"}, deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "Ссылки для входа пишутся в лог приложения вместо писем")
    say("Только для проверки: ссылка из лога пускает в приложение. Смотреть: small logs | Select-String \"ссылка для входа\"")
    return 0


def cmd_mail_off(args):
    root = app_dir(args.path)
    linked = change_env(root, unset=list(ALL_VARS), deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, "Отправка писем для входа выключена")
    return 0


def cmd_mail_status(args):
    root = app_dir(args.path)
    env, _ = tokens.app_env(root, load_state(root / STATE_FILE), warn=say)
    sender = env.get("MAIL_FROM") or ""
    if env.get("LOGIN_LINKS_TO_LOG"):
        say("Письма не отправляются: ссылки для входа пишутся в лог приложения (режим проверки).")
        print("log")
    elif env.get("RESEND_API_KEY"):
        say("Письма для входа: Resend, отправитель %s" % (sender or "onboarding@resend.dev (только на адрес аккаунта)"))
        print("resend")
    elif env.get("SMTP_URL"):
        say("Письма для входа: SMTP %s%s" % (urlsplit(env["SMTP_URL"]).hostname, ", отправитель " + sender if sender else ""))
        print("smtp")
    else:
        say("Почта не настроена: вход по почте не работает. Подключить: small mail resend")
        print("off")
    return 0
