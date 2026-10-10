"""small mail: how the app sends sign-in emails (small access add ivan@company.com).

resend: Resend over HTTPS (works on Railway Hobby); smtp: your own mail server (on Railway, SMTP is open only
from the Pro plan); log: sign-in links are written to the app log (for testing); off: turn it off.
Before saving, resend and smtp send a test email with the same code the app uses.
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
from ..i18n import lang, tr
from ..scaffold import TEMPLATES_DIR
from .env import add_no_deploy, change_env, report_env_change

ALL_VARS = ("RESEND_API_KEY", "SMTP_URL", "LOGIN_LINKS_TO_LOG", "MAIL_FROM")


def register(sub):
    m = sub.add_parser("mail", help=tr("sign-in emails: resend, smtp, log, off",
                                       "письма для входа по почте: resend, smtp, log, off"),
                       description=tr("How to send emails with sign-in links. resend: key from RESEND_API_KEY "
                                      "or hidden input; smtp: server URL from SMTP_URL or hidden input; "
                                      "log: write links to the log (for testing); off: turn off; "
                                      "with no subcommand, shows the status.",
                                      "Чем отправлять письма со ссылкой для входа. resend — ключ из RESEND_API_KEY "
                                      "или ввод без эха; smtp — адрес сервера из SMTP_URL или ввод без эха; "
                                      "log — писать ссылки в лог (для проверки); off — выключить; "
                                      "без подкоманды — статус."))
    m.add_argument("-C", "--path", default=".", help=tr("app folder (default: current folder)",
                                                        "папка приложения (по умолчанию текущая)"))
    m.set_defaults(func=cmd_mail_status)
    mail_sub = m.add_subparsers(dest="mail_command", metavar="<resend|smtp|log|off>")
    mr = mail_sub.add_parser("resend", help=tr("send via Resend (HTTPS)", "отправлять через Resend (HTTPS)"))
    ms = mail_sub.add_parser("smtp", help=tr("send via your own SMTP server", "отправлять через свой SMTP-сервер"))
    for parser in (mr, ms):
        parser.add_argument("--from", dest="sender", help=tr('sender: "Reports <noreply@company.com>"',
                                                             'отправитель: "Отчёты <noreply@company.com>"'))
        parser.add_argument("--to", help=tr("where to send the test email (for Resend without your own domain, "
                                            "use your Resend account email)",
                                            "куда отправить пробное письмо (для Resend без своего домена — "
                                            "адрес вашего аккаунта Resend)"))
    mr.set_defaults(func=cmd_mail_resend)
    ms.set_defaults(func=cmd_mail_smtp)
    ml = mail_sub.add_parser("log", help=tr("write sign-in links to the app log (for testing only)",
                                            "писать ссылки для входа в лог приложения (только для проверки)"))
    ml.set_defaults(func=cmd_mail_log)
    mo = mail_sub.add_parser("off", help=tr("stop sending emails", "выключить отправку писем"))
    mo.set_defaults(func=cmd_mail_off)
    for parser in (mr, ms, ml, mo):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help=tr("app folder", "папка приложения"))
        add_no_deploy(parser)


def secret_input(name, prompt):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if not sys.stdin or not sys.stdin.isatty():
        raise CliError(tr("set %s or run the command in a terminal",
                          "задайте %s или запустите команду в терминале") % name)
    say(prompt + tr(" Input is hidden.", " Ввод не отображается."))
    value = getpass.getpass("%s: " % name).strip()
    if not value:
        raise CliError(tr("%s not entered", "%s не введён") % name)
    return value


def mailer():
    """magic_link.py from the template: the email is sent by the same code as from the app."""
    path = TEMPLATES_DIR / "python-http" / "magic_link.py"
    spec = importlib.util.spec_from_file_location("small_magic_link", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def mail_env(values):
    saved = {name: os.environ.get(name) for name in (*ALL_VARS, *values)}
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
    to = (to or ask(tr("Send the test email to: ", "Куда отправить пробное письмо: ")) or "").strip()
    if not tokens.EMAIL_RE.match(to):
        raise CliError(tr("specify an address for the test email: --to ivan@company.com",
                          "укажите адрес для пробного письма: --to ivan@company.com"))
    app = load_config(root / CONFIG_FILE)["name"] or load_state(root / STATE_FILE).get("name") or root.name
    module = mailer()
    with mail_env(dict(values, OWNER_LANG=lang())):  # magic_link errors in small's language
        try:
            module.send_mail(to, tr("small: email check for %s", "small: проверка почты для %s") % app,
                             tr("Sign-in emails for %s will arrive just like this one.\n",
                                "Письма для входа в %s будут приходить так же, как это.\n") % app,
                             tr("<p>Sign-in emails for <b>%s</b> will arrive just like this one.</p>",
                                "<p>Письма для входа в <b>%s</b> будут приходить так же, как это.</p>") % app)
        except Exception as e:  # noqa: BLE001 — show the reason, but not the key
            hint = ""
            if "RESEND_API_KEY" in values and "403" in str(getattr(e, "code", "")):
                hint = tr(" Without a verified domain, Resend only sends to your account email; "
                          "verify a domain in Resend and pass --from.",
                          " Без подтверждённого домена Resend шлёт только на адрес вашего аккаунта; "
                          "подтвердите домен в Resend и укажите --from.")
            raise CliError(tr("test email was not sent: %s.%s", "пробное письмо не ушло: %s.%s")
                           % (module._describe(e), hint)) from None
    say(tr("Test email sent to %s. Check your inbox (and the Spam folder).",
           "Пробное письмо отправлено на %s — проверьте почту (и папку «Спам»).") % to)


def cmd_mail_resend(args):
    root = app_dir(args.path)
    key = secret_input("RESEND_API_KEY", tr("API key from resend.com → API Keys.",
                                            "Ключ API из resend.com → API Keys."))
    values = {"RESEND_API_KEY": key, "MAIL_FROM": args.sender or ""}
    test_letter(root, values, args.to)
    linked = change_env(root, {k: v for k, v in values.items() if v},
                        unset=["SMTP_URL", "LOGIN_LINKS_TO_LOG"] + ([] if args.sender else ["MAIL_FROM"]),
                        deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, tr("Sign-in emails are sent via Resend",
                                                     "Письма для входа отправляются через Resend"))
    return 0


def cmd_mail_smtp(args):
    root = app_dir(args.path)
    url = secret_input("SMTP_URL", tr("Server URL: smtp://user:password@host:587 or smtps://…:465.",
                                      "Адрес сервера: smtp://логин:пароль@сервер:587 или smtps://…:465."))
    parts = urlsplit(url)
    if parts.scheme not in ("smtp", "smtps") or not parts.hostname:
        raise CliError(tr("SMTP_URL must look like smtp://user:password@host:587 (or smtps://…:465)",
                          "SMTP_URL должен выглядеть так: smtp://логин:пароль@сервер:587 (или smtps://…:465)"))
    values = {"SMTP_URL": url, "MAIL_FROM": args.sender or ""}
    test_letter(root, values, args.to)
    linked = change_env(root, {k: v for k, v in values.items() if v},
                        unset=["RESEND_API_KEY", "LOGIN_LINKS_TO_LOG"] + ([] if args.sender else ["MAIL_FROM"]),
                        deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, tr("Sign-in emails are sent via %s",
                                                     "Письма для входа отправляются через %s") % parts.hostname)
    say(tr("On Railway, SMTP is open only on the Pro plan; on Hobby, use small mail resend.",
           "На Railway SMTP открыт только с тарифа Pro; на Hobby используйте small mail resend."))
    return 0


def cmd_mail_log(args):
    root = app_dir(args.path)
    linked = change_env(root, {"LOGIN_LINKS_TO_LOG": "1"}, deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, tr("Sign-in links are written to the app log instead of emails",
                                                     "Ссылки для входа пишутся в лог приложения вместо писем"))
    # Look for the middleware line in the owner's language (OWNER_LANG = small's language at deploy time).
    search = ('Select-String "%s"' if sys.platform == "win32" else 'grep "%s"') % tr("sign-in link", "ссылка для входа")
    say(tr("For testing only: a link from the log lets anyone into the app. View: small logs | %s",
           "Только для проверки: ссылка из лога пускает в приложение. Смотреть: small logs | %s") % search)
    return 0


def cmd_mail_off(args):
    root = app_dir(args.path)
    linked = change_env(root, unset=list(ALL_VARS), deploy=not args.no_deploy)
    report_env_change(linked, not args.no_deploy, tr("Sign-in emails are turned off",
                                                     "Отправка писем для входа выключена"))
    return 0


def cmd_mail_status(args):
    root = app_dir(args.path)
    env, _ = tokens.app_env(root, load_state(root / STATE_FILE), warn=say)
    sender = env.get("MAIL_FROM") or ""
    if env.get("LOGIN_LINKS_TO_LOG"):
        say(tr("Emails are not sent: sign-in links are written to the app log (test mode).",
               "Письма не отправляются: ссылки для входа пишутся в лог приложения (режим проверки)."))
        print("log")
    elif env.get("RESEND_API_KEY"):
        say(tr("Sign-in emails: Resend, sender %s", "Письма для входа: Resend, отправитель %s")
            % (sender or tr("onboarding@resend.dev (only to your account email)",
                            "onboarding@resend.dev (только на адрес аккаунта)")))
        print("resend")
    elif env.get("SMTP_URL"):
        say(tr("Sign-in emails: SMTP %s%s", "Письма для входа: SMTP %s%s")
            % (urlsplit(env["SMTP_URL"]).hostname, tr(", sender ", ", отправитель ") + sender if sender else ""))
        print("smtp")
    else:
        say(tr("Email is not set up: sign-in by email does not work. Set it up: small mail resend",
               "Почта не настроена: вход по почте не работает. Подключить: small mail resend"))
        print("off")
    return 0
