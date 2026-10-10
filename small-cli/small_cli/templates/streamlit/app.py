"""A Streamlit app created by `small init --template streamlit`. Deploy it with `small deploy`.

Locally: `python app.py` (or `streamlit run app.py`).
Access is by personal invite link https://<domain>/?token=<ACCESS_TOKEN> (`small access add bob`); the check is
require_access() below. Build your own interface after the require_access() call.
"""

import hmac
import json
import os
import sys
import urllib.request

import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

if __name__ == "__main__" and get_script_run_ctx(suppress_warning=True) is None:
    # Railway runs `python app.py`, which Streamlit does not support — restart through `streamlit run`.
    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", __file__,
                "--server.address", os.environ.get("HOST", "0.0.0.0"),
                "--server.port", os.environ.get("PORT", "8501"),
                "--server.headless", "true",
                "--browser.gatherUsageStats", "false",
                "--client.toolbarMode", "minimal"]  # no Streamlit Cloud "Deploy" button
    sys.exit(stcli.main())

import numpy as np  # noqa: E402  (numpy and pandas come with streamlit)
import pandas as pd  # noqa: E402

st.set_page_config(page_title="small · Streamlit", page_icon="🚀", layout="wide")

# The access screen speaks the visitor's language (Accept-Language or ACCESS_LANG=ru|en), like magic_link.py.
GATE = {
    "en": {"forbidden": "Invite only", "invalid": "This link is not valid",
           "forbidden_text": "This app opens only with an invite link from its owner.",
           "invalid_text": "The link has expired or was copied incompletely. Ask for a new one.",
           "request": "Request access", "contact": "Email or Telegram to reply to",
           "sent": "Request sent. The owner will see it and send you an invite link.",
           "no_contact": "Tell us how to reach you."},
    "ru": {"forbidden": "Доступ по приглашению", "invalid": "Ссылка недействительна",
           "forbidden_text": "Это приложение открывается только по ссылке-приглашению от владельца.",
           "invalid_text": "Ссылка устарела или скопирована не полностью. Запросите новую.",
           "request": "Запросить доступ", "contact": "Email или Telegram для ответа",
           "sent": "Запрос отправлен. Владелец увидит его и пришлёт вам ссылку-приглашение.",
           "no_contact": "Укажите, как с вами связаться."},
}


def _header(name):
    return st.context.headers.get(name, "") if hasattr(st, "context") else ""


def _visitor_lang():
    setting = os.environ.get("ACCESS_LANG", "auto").strip().lower()
    if setting in GATE:
        return setting
    header = _header("accept-language")
    if not header.strip():
        return "ru"
    best, best_q = "en", -1.0  # any other language (de, fr…) gets English
    for part in header.split(","):
        tag, _, params = part.strip().partition(";")
        base = tag.strip().lower().split("-")[0]
        base = "ru" if base == "*" else base
        try:
            q = float(params.strip()[2:]) if params.strip().startswith("q=") else 1.0
        except ValueError:
            q = 0.0
        if base in GATE and q > best_q:
            best, best_q = base, q
    return best


def _for_owner(en, ru):
    """Logs and Telegram messages are for the owner: OWNER_LANG=ru|en (small deploy sets it)."""
    return ru if os.environ.get("OWNER_LANG", "").strip().lower()[:2] == "ru" else en


@st.cache_resource
def _warn_once(message):
    print(message, flush=True)


def _notify_telegram(contact):
    """With TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set (small notify telegram), the request reaches the owner."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return
    host = _header("host") or _for_owner("the app", "приложению")
    text = _for_owner("🔑 Access request to %s\nContact: %s\n\nSend a personal link: small access add NAME",
                      "🔑 Запрос доступа к %s\nКонтакт: %s\n\nВыдать личную ссылку: small access add ИМЯ") % (host, contact)
    api = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/")
    request = urllib.request.Request("%s/bot%s/sendMessage" % (api, token), headers={"Content-Type": "application/json"},
                                     data=json.dumps({"chat_id": chat, "text": text}).encode("utf-8"))
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except Exception as e:  # a failed notification must not break the page; never log the bot token
        print(_for_owner("[magic-link] could not send the access request to Telegram: %s",
                         "[magic-link] не удалось отправить запрос доступа в Telegram: %s") % type(e).__name__,
              flush=True)


def require_access():
    """magic_link.py for Streamlit: lets people in only with ?token=<ACCESS_TOKEN>.

    Streamlit cannot set cookies, so the token stays in the address — that way access survives a page reload.
    Without a valid token the visitor sees an "Invite only" screen with a "Request access" button.
    ACCESS_TOKEN — comma-separated tokens, optionally named: "alice:token1,bob:token2" (as `small access` keeps them);
    ACCESS_REQUEST_URL — where the button leads.
    """
    if st.session_state.get("access_granted"):
        return
    entries = []  # "alice:token1,bob:token2" -> [("alice", "token1"), ("bob", "token2")]
    for item in os.environ.get("ACCESS_TOKEN", "").split(","):
        name, _, token = item.strip().rpartition(":")
        if token.strip():
            entries.append((name.strip(), token.strip()))
    if not entries:
        _warn_once(_for_owner("[magic-link] ACCESS_TOKEN is not set: nobody can get in",
                              "[magic-link] ACCESS_TOKEN не задан: доступ закрыт для всех"))
    given = st.query_params.get("token")
    owner = next((name for name, token in entries
                  if given is not None and hmac.compare_digest(given.encode(), token.encode())), None)
    if owner is not None:
        st.session_state.access_granted = True
        print(_for_owner("[magic-link] signed in by link%s", "[magic-link] вход по ссылке%s")
              % (" (%s)" % owner if owner else ""), flush=True)
        return

    t = GATE[_visitor_lang()]
    _, center, _ = st.columns([1, 2, 1])
    with center:
        st.markdown("## 🔒 " + (t["invalid"] if given is not None else t["forbidden"]))
        st.write(t["invalid_text"] if given is not None else t["forbidden_text"])
        request_url = os.environ.get("ACCESS_REQUEST_URL", "")
        if request_url:
            st.link_button(t["request"], request_url, type="primary")
        elif st.session_state.get("access_requested"):
            st.success(t["sent"])
        else:
            with st.form("access_request"):
                contact = st.text_input(t["contact"], max_chars=200)
                if st.form_submit_button(t["request"], type="primary"):
                    if contact.strip():
                        # repr() escapes line breaks, so a visitor cannot forge log lines.
                        print("[access-request] contact=%r" % contact.strip(), flush=True)
                        _notify_telegram(contact.strip()[:200])
                        st.session_state.access_requested = True
                        st.rerun()
                    else:
                        st.warning(t["no_contact"])
    st.stop()


require_access()

# ---------- Your app ----------

st.title("🚀 small + Streamlit")
st.caption("An interactive app template. Edit `app.py` and run `small deploy`.")

with st.sidebar:
    st.header("Settings")
    points = st.slider("Points", min_value=20, max_value=500, value=120, step=10)
    volatility = st.slider("Volatility", min_value=0.1, max_value=5.0, value=1.0, step=0.1)
    seed = st.number_input("Seed", value=42, step=1)
    kind = st.radio("Chart type", ["Line", "Area", "Bars"], horizontal=True)

rng = np.random.default_rng(int(seed))
data = pd.DataFrame(rng.normal(0, volatility, size=(points, 3)).cumsum(axis=0), columns=["Alpha", "Beta", "Gamma"])

for column, name in zip(st.columns(3), data.columns):
    column.metric(name, "%.2f" % data[name].iloc[-1], "%+.2f" % (data[name].iloc[-1] - data[name].iloc[0]))

{"Line": st.line_chart, "Area": st.area_chart, "Bars": st.bar_chart}[kind](data)

with st.expander("Data"):
    st.dataframe(data.round(3))
    st.download_button("Download CSV", data.to_csv(index_label="t").encode("utf-8"), "data.csv", "text/csv")
