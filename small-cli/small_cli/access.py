"""ACCESS_TOKEN приложения: формат "имя:токен,…", поиск токена (small.json или Railway), ссылки-приглашения.

Тот же формат понимают magic_link.py, magic-link.js и шаблон streamlit: убрали чей-то токен — его ссылка
и куки перестают работать, у остальных всё остаётся.
"""

import re
import secrets
from urllib.parse import quote

from .config import CONFIG_FILE, load_config
from .railway import Railway, RailwayError, token_from_env

NAME_RE = re.compile(r"[A-Za-z0-9_.@-]{1,40}\Z")


def parse(value):
    """"alice:tok1, tok2" -> [("alice", "tok1"), ("", "tok2")]."""
    entries = []
    for item in (value or "").split(","):
        name, _, token = item.strip().rpartition(":")
        if token.strip():
            entries.append((name.strip(), token.strip()))
    return entries


def serialize(entries):
    return ",".join("%s:%s" % (name, token) if name else token for name, token in entries)


def new_token():
    return secrets.token_urlsafe(32)


def current(root, state, warn=None):
    """(записи ACCESS_TOKEN, откуда): из small.json, иначе из переменных сервиса в Railway, иначе ([], None)."""
    raw = load_config(root / CONFIG_FILE)["env"].get("ACCESS_TOKEN")
    if raw:
        return parse(raw), CONFIG_FILE
    linked = all(state.get(k) for k in ("projectId", "environmentId", "serviceId"))
    if linked and token_from_env():
        try:
            raw = Railway(token_from_env()).service_variables(
                state["projectId"], state["environmentId"], state["serviceId"]).get("ACCESS_TOKEN")
        except RailwayError as e:
            if warn:
                warn("Не удалось прочитать переменные сервиса в Railway: %s" % e)
            raw = None
        if raw:
            return parse(raw), "Railway"
    return [], None


def pick(entries, name=None):
    """Токен человека name; без имени — первый (владельца). None, если такого нет."""
    if name is None:
        return entries[0][1] if entries else None
    return next((token for who, token in entries if who == name), None)


def invite_link(domain, token):
    url = "https://%s/" % domain
    return url + "?token=" + quote(token, safe="") if token else url
