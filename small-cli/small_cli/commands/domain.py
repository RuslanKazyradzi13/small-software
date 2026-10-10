"""small domain: свой домен вместо *.up.railway.app — Railway выдаёт DNS-записи, их нужно прописать у регистратора."""

import argparse
import re

from ..common import CliError, app_dir, linked_state, require_token, say
from ..railway import Railway

DOMAIN_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")


def register(sub):
    d = sub.add_parser("domain", help="свой домен: добавить, список, удалить",
                       description="Свой домен для приложения. add регистрирует домен в Railway и печатает DNS-записи, "
                                   "которые нужно прописать у регистратора; HTTPS-сертификат Railway выпустит сам, когда "
                                   "записи заработают.")
    d.add_argument("-C", "--path", default=".", help="папка приложения (по умолчанию текущая)")
    d.set_defaults(func=cmd_domain_list)
    domain_sub = d.add_subparsers(dest="domain_command", metavar="<list|add|remove>")
    dl = domain_sub.add_parser("list", help="домены приложения и их состояние")
    dl.set_defaults(func=cmd_domain_list)
    da = domain_sub.add_parser("add", help="добавить домен: small domain add app.example.com")
    da.add_argument("domain")
    da.add_argument("--port", type=int, help="порт приложения, если оно не слушает $PORT")
    da.set_defaults(func=cmd_domain_add)
    dr = domain_sub.add_parser("remove", help="убрать домен: small domain remove app.example.com")
    dr.add_argument("domain")
    dr.set_defaults(func=cmd_domain_remove)
    for parser in (dl, da, dr):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help="папка приложения")


def normalize(domain):
    domain = domain.strip().lower().rstrip(".")
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.split("/")[0]
    if not DOMAIN_RE.match(domain):
        raise CliError("%r не похож на домен (пример: app.example.com)" % domain)
    return domain


def ids(state):
    return state["projectId"], state["environmentId"], state["serviceId"]


def cmd_domain_list(args):
    state = linked_state(app_dir(args.path))
    domains = Railway(require_token()).custom_domains(*ids(state))
    say("Адрес Railway: https://%s" % state.get("domain", "—"))
    if not domains:
        say("Своих доменов нет. Добавить: small domain add app.example.com")
        return 0
    for item in domains:
        print("%-40s %s" % (item["domain"], summary(item.get("status") or {})))
    return 0


def cmd_domain_add(args):
    domain = normalize(args.domain)
    state = linked_state(app_dir(args.path))
    api = Railway(require_token())
    if any(item["domain"] == domain for item in api.custom_domains(*ids(state))):
        raise CliError("%s уже добавлен. Записи: small domain list" % domain)
    created = api.create_custom_domain(*ids(state), domain, args.port)
    say("Домен %s добавлен. Пропишите у регистратора DNS-записи:" % domain)
    print_records(created.get("status") or {})
    say("Когда записи заработают (от минут до суток), Railway сам выпустит HTTPS-сертификат.")
    say("Проверить: small domain list · ссылка-приглашение будет работать и на https://%s/?token=…" % domain)
    return 0


def cmd_domain_remove(args):
    domain = normalize(args.domain)
    state = linked_state(app_dir(args.path))
    api = Railway(require_token())
    item = next((d for d in api.custom_domains(*ids(state)) if d["domain"] == domain), None)
    if item is None:
        raise CliError("%s не найден среди доменов приложения. Список: small domain list" % domain)
    api.delete_custom_domain(item["id"])
    say("Домен %s убран из Railway. DNS-записи у регистратора можно удалить." % domain)
    return 0


def print_records(status):
    rows = [(r["recordType"].replace("DNS_RECORD_TYPE_", ""), r.get("fqdn") or r.get("hostlabel") or "@",
             r["requiredValue"]) for r in status.get("dnsRecords") or []]
    if status.get("verificationToken") and not status.get("verified"):
        rows.append(("TXT", status.get("verificationDnsHost") or "", status["verificationToken"]))
    if not rows:
        say("  Railway не вернул записи — посмотрите позже: small domain list")
    for kind, name, value in rows:
        print("  %-6s %-40s %s" % (kind, name, value))


def summary(status):
    records = status.get("dnsRecords") or []
    dns_ok = bool(records) and all("PROPAGATED" in (r.get("status") or "") for r in records)
    cert = (status.get("certificateStatus") or "").replace("CERTIFICATE_STATUS_TYPE_", "").lower()
    parts = ["DNS настроен" if dns_ok else "ждёт DNS-записей"]
    if cert:
        parts.append("сертификат: %s" % cert)
    if status.get("verificationToken") and not status.get("verified"):
        parts.append("нужна TXT-проверка")
    return ", ".join(parts)
