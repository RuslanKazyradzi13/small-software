"""small domain: a custom domain instead of *.up.railway.app; Railway issues DNS records to add at the registrar."""

import argparse
import re

from .. import gateway
from ..common import CliError, app_dir, linked_state, require_token, say
from ..i18n import tr
from ..railway import Railway

DOMAIN_RE = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")


def register(sub):
    d = sub.add_parser("domain", help=tr("custom domains: add, list, remove", "свой домен: добавить, список, удалить"),
                       description=tr("A custom domain for the app. add registers the domain in Railway and prints "
                                      "the DNS records to set at your registrar; Railway issues the HTTPS "
                                      "certificate itself once the records work.",
                                      "Свой домен для приложения. add регистрирует домен в Railway и печатает "
                                      "DNS-записи, которые нужно прописать у регистратора; HTTPS-сертификат Railway "
                                      "выпустит сам, когда записи заработают."))
    d.add_argument("-C", "--path", default=".", help=tr("app folder (default: current folder)",
                                                        "папка приложения (по умолчанию текущая)"))
    d.set_defaults(func=cmd_domain_list)
    domain_sub = d.add_subparsers(dest="domain_command", metavar="<list|add|remove>")
    dl = domain_sub.add_parser("list", help=tr("app domains and their status", "домены приложения и их состояние"))
    dl.set_defaults(func=cmd_domain_list)
    da = domain_sub.add_parser("add", help=tr("add a domain: small domain add app.example.com",
                                              "добавить домен: small domain add app.example.com"))
    da.add_argument("domain")
    da.add_argument("--port", type=int, help=tr("app port, if the app doesn't listen on $PORT",
                                                "порт приложения, если оно не слушает $PORT"))
    da.set_defaults(func=cmd_domain_add)
    dr = domain_sub.add_parser("remove", help=tr("remove a domain: small domain remove app.example.com",
                                                 "убрать домен: small domain remove app.example.com"))
    dr.add_argument("domain")
    dr.set_defaults(func=cmd_domain_remove)
    for parser in (dl, da, dr):
        parser.add_argument("-C", "--path", default=argparse.SUPPRESS, help=tr("app folder", "папка приложения"))


def normalize(domain):
    domain = domain.strip().lower().rstrip(".")
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.split("/")[0]
    if not DOMAIN_RE.match(domain):
        raise CliError(tr("%r does not look like a domain (example: app.example.com)",
                          "%r не похож на домен (пример: app.example.com)") % domain)
    return domain


def ids(state):
    """The service that has the public address: behind the gateway, the gateway."""
    return state["projectId"], state["environmentId"], gateway.public_service(state)


def cmd_domain_list(args):
    state = linked_state(app_dir(args.path))
    domains = Railway(require_token()).custom_domains(*ids(state))
    say(tr("Railway URL: https://%s", "Адрес Railway: https://%s") % state.get("domain", "—"))
    if not domains:
        say(tr("No custom domains. Add one: small domain add app.example.com",
               "Своих доменов нет. Добавить: small domain add app.example.com"))
        return 0
    for item in domains:
        print("%-40s %s" % (item["domain"], summary(item.get("status") or {})))
    return 0


def cmd_domain_add(args):
    domain = normalize(args.domain)
    state = linked_state(app_dir(args.path))
    api = Railway(require_token())
    if any(item["domain"] == domain for item in api.custom_domains(*ids(state))):
        raise CliError(tr("%s is already added. Records: small domain list",
                          "%s уже добавлен. Записи: small domain list") % domain)
    created = api.create_custom_domain(*ids(state), domain, args.port)
    say(tr("Domain %s added. Set these DNS records at your registrar:",
           "Домен %s добавлен. Пропишите у регистратора DNS-записи:") % domain)
    print_records(created.get("status") or {})
    say(tr("Once the records work (from a few minutes to a day), Railway issues the HTTPS certificate itself.",
           "Когда записи заработают (от минут до суток), Railway сам выпустит HTTPS-сертификат."))
    say(tr("Check: small domain list · the invite link will also work at https://%s/?token=…",
           "Проверить: small domain list · ссылка-приглашение будет работать и на https://%s/?token=…") % domain)
    return 0


def cmd_domain_remove(args):
    domain = normalize(args.domain)
    state = linked_state(app_dir(args.path))
    api = Railway(require_token())
    item = next((d for d in api.custom_domains(*ids(state)) if d["domain"] == domain), None)
    if item is None:
        raise CliError(tr("%s is not one of the app's domains. List: small domain list",
                          "%s не найден среди доменов приложения. Список: small domain list") % domain)
    api.delete_custom_domain(item["id"])
    say(tr("Domain %s removed from Railway. You can delete its DNS records at your registrar.",
           "Домен %s убран из Railway. DNS-записи у регистратора можно удалить.") % domain)
    return 0


def print_records(status):
    rows = [(r["recordType"].replace("DNS_RECORD_TYPE_", ""), r.get("fqdn") or r.get("hostlabel") or "@",
             r["requiredValue"]) for r in status.get("dnsRecords") or []]
    if status.get("verificationToken") and not status.get("verified"):
        rows.append(("TXT", status.get("verificationDnsHost") or "", status["verificationToken"]))
    if not rows:
        say(tr("  Railway returned no records; check later: small domain list",
               "  Railway не вернул записи — посмотрите позже: small domain list"))
    for kind, name, value in rows:
        print("  %-6s %-40s %s" % (kind, name, value))


def summary(status):
    records = status.get("dnsRecords") or []
    dns_ok = bool(records) and all("PROPAGATED" in (r.get("status") or "") for r in records)
    cert = (status.get("certificateStatus") or "").replace("CERTIFICATE_STATUS_TYPE_", "").lower()
    parts = [tr("DNS configured", "DNS настроен") if dns_ok else tr("waiting for DNS records", "ждёт DNS-записей")]
    if cert:
        parts.append(tr("certificate: %s", "сертификат: %s") % cert)
    if status.get("verificationToken") and not status.get("verified"):
        parts.append(tr("TXT verification needed", "нужна TXT-проверка"))
    return ", ".join(parts)
