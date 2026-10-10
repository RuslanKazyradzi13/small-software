"""A Streamlit app created by `small init --template streamlit`. Deploy it with `small deploy`.

Locally: `python app.py` (or `streamlit run app.py`).
Who gets in is decided by require_access() below, built on MagicLinkGate from magic_link.py: people you invite by
work email (`small access add ivan@company.com`), who sign in with Google or Microsoft (`small sso`) or by an emailed
link, or with a personal invite link (`small access add bob`).
Build your own interface after the require_access() call.
"""

import hashlib
import html
import json
import os
import re
import sys
import time

import streamlit as st
import streamlit.components.v1 as components
from streamlit.runtime.scriptrunner import get_script_run_ctx

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:  # magic_link.py sits next to app.py; not every runner puts this folder on sys.path
    sys.path.insert(0, APP_DIR)
from magic_link import LOGOS, SSO_TTL, MagicLinkGate  # noqa: E402

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

# ---------- Access: magic_link.py for Streamlit ----------

# Settings that MagicLinkGate reads when it starts: a change (for example, a revoked address) gives a new gate.
GATE_SETTINGS = ("ACCESS_TOKEN", "ACCESS_EMAILS", "SESSION_SECRET", "ACCESS_REQUEST_URL", "ACCESS_LANG",
                 "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                 "MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET", "MICROSOFT_TENANT_ID", "SESSIONS_REVOKED",
                 "ACCESS_DB", "RAILWAY_VOLUME_MOUNT_PATH")

# Streamlit-only labels. Everything else comes from magic_link's MESSAGES and UI, in the visitor's language.
LABELS = {
    "en": {"email": "Work email", "no_contact": "Tell us how to reach you.", "signed_in": "Signed in as %s",
           "sign_out": "Sign out"},
    "ru": {"email": "Рабочая почта", "no_contact": "Укажите, как с вами связаться.", "signed_in": "Вы вошли как %s",
           "sign_out": "Выйти"},
}


@st.cache_resource(show_spinner=False)
def _make_gate(settings_hash):
    # One gate per process: used sign-in links and email rate limits must be shared by all visitors.
    return MagicLinkGate()


def _gate():
    settings = json.dumps([os.environ.get(name, "") for name in GATE_SETTINGS])
    return _make_gate(hashlib.sha256(settings.encode("utf-8")).hexdigest())


def _md(text):
    """Escape Markdown, so that an address like ivan_petrov@company.com is shown as is."""
    return re.sub(r"([!-/:-@\[-`{-~])", r"\\\1", text)


def _write_cookie(name, value, max_age):
    """Write a cookie (max_age=0 deletes it) from the browser: the session, or the single sign-on state.

    Streamlit gives the script no way to send a Set-Cookie header, so a tiny frame does it in JavaScript. The frame is
    same-origin (Streamlit's iframe sandbox includes allow-same-origin), so it writes the cookie of the app itself.
    Trade-off: a cookie written by JavaScript cannot be HttpOnly, so any script running in this page could read it.
    The value is signed, expires in 30 days and stops working once the address leaves ACCESS_EMAILS or
    SESSION_SECRET changes; SameSite=Lax keeps other sites from using it. Never render untrusted HTML or JavaScript
    in a protected app (unsafe_allow_html, components, st.iframe): it could steal the session.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]*", value):  # signed values are base64url: nothing to escape in the script
        raise ValueError("unexpected cookie value")
    cookie = "%s=%s; Path=/; Max-Age=%d; SameSite=Lax" % (name, value, max_age)
    script = ("<script>var page = window.parent; page.document.cookie = %s + "
              "(page.location.protocol === 'https:' ? '; Secure' : '');</script>") % json.dumps(cookie)
    if hasattr(st, "iframe"):  # newer Streamlit deprecates components.v1.html in favour of st.iframe
        st.iframe(script, height=1)
    else:
        components.html(script, height=0)


def _session_cookie(name):
    """The session of this visitor: started in this browser tab, or sent by the browser when the page opened."""
    state = st.session_state
    if "small_session" in state:
        return state.small_session
    if state.get("small_signed_out"):
        return None  # st.context.cookies still shows the cookie the page was opened with
    value = st.context.cookies.get(name)  # what the browser sent when the page opened; later changes need a reload
    return value if isinstance(value, str) else None


def _sign_out():
    st.session_state.pop("small_session", None)
    st.session_state.small_signed_out = True
    st.session_state.small_clear_cookie = True


def _sign_out_everywhere(gate, token, headers):
    """Ends every session of this address, on all devices (the state lives on a volume, so it survives restarts)."""
    st.session_state.small_signed_out_all = gate.sign_out_everywhere(token, headers) or ""
    _sign_out()


def _sso_buttons(gate, lang):
    """"Sign in with Google / Microsoft" links. The provider sends the visitor back to <app>/?code=...&state=...

    The state cookie, written once per browser tab (each tab has its own), proves that the answer comes back to the
    browser that started the sign-in. The links are plain <a target="_self">: a top-level navigation, as the providers require."""
    state = st.session_state
    started = state.get("small_sso")
    if started is None or started["until"] < time.time() + 60:  # a sign-in has 10 minutes: renew it in time
        result = gate.sso_links(st.context.headers, "/")
        if result is None:  # the app's address is unknown (set PUBLIC_URL): no redirect address to give
            return False
        links, cookie_name, cookie_value = result
        started = state.small_sso = {"links": links, "until": time.time() + SSO_TTL}
        _write_cookie(cookie_name, cookie_value, SSO_TTL)
    for provider in gate.sso:
        logo = LOGOS[provider].replace("<svg ", '<svg width="18" height="18" ', 1)
        st.markdown('<a href="%s" target="_self" style="display:flex;align-items:center;justify-content:center;'
                    'gap:10px;padding:8px 16px;margin-bottom:8px;border:1px solid rgba(128,128,128,.4);'
                    'border-radius:8px;color:inherit;text-decoration:none;font-weight:600">%s<span>%s</span></a>'
                    % (html.escape(started["links"][provider]), logo, html.escape(gate.label(provider, lang))),
                    unsafe_allow_html=True)
    return True


def _email_form(gate, lang):
    with st.form("small_login"):
        email = st.text_input(LABELS[lang]["email"], max_chars=254, placeholder="name@company.com",
                              autocomplete="email")
        if st.form_submit_button(gate.label("get_link", lang), type="primary"):
            # The answer is the same for known and unknown addresses; an unknown one becomes an access request.
            result = gate.start_login(email, st.context.headers)
            if result == "check":
                st.session_state.small_mail_sent = True
                st.rerun()
            _, title, text = gate.text(result, lang)
            st.warning("%s. %s" % (title, text))


def _request_form(gate, lang):
    if st.session_state.get("access_requested"):
        _, title, text = gate.text("sent", lang)
        st.success("%s. %s" % (title, text))
        return
    with st.form("access_request"):
        contact = st.text_input(gate.label("contact", lang), max_chars=200)
        if st.form_submit_button(gate.label("request", lang), type="primary"):
            if contact.strip():
                # Goes to the log as one JSON line (a visitor cannot forge log lines) and to Telegram, if connected.
                gate.request_access(contact, st.context.headers)
                st.session_state.access_requested = True
                st.rerun()
            else:
                st.warning(LABELS[lang]["no_contact"])


def _screen(gate, lang, kind, email=""):
    """The access screen with magic_link's texts. Returns True when the "Sign in" button (kind="confirm") is clicked."""
    if kind == "forbidden" and gate.emails:
        kind = "sso" if gate.sso else "login"
    icon, title, text = gate.text(kind, lang, _md(email))
    _, center, _ = st.columns([1, 2, 1])
    with center:
        st.markdown("## %s %s" % (icon, title))
        st.write(text)
        if kind == "confirm":
            return st.button(gate.label("sign_in", lang), type="primary", key="small_sign_in")
        if kind == "check":
            st.button(gate.label("other", lang), on_click=st.session_state.pop, args=("small_mail_sent", None))
        elif kind in ("denied", "unverified"):
            st.button(gate.label("other_account", lang), key="small_other_account")
        elif gate.emails:
            has_buttons = _sso_buttons(gate, lang) if gate.sso else False
            if gate.email_form:
                if has_buttons:
                    st.caption(gate.label("or", lang))
                _email_form(gate, lang)
        elif gate.request_url:
            st.link_button(gate.label("request", lang), gate.request_url, type="primary")
        else:
            _request_form(gate, lang)
    return False


def require_access():
    """magic_link.py for Streamlit: lets in invited people only; everyone else gets an access screen (st.stop()).

    Returns who is signed in: an email address, or a name from ACCESS_TOKEN ("" for the shared link).
    The rules, settings, emails, logs and texts are magic_link's (MagicLinkGate), with these Streamlit specifics:
      * Sign-in with Google or Microsoft (small sso): the provider sends the visitor back to <app>/?code=...&state=...,
        so register the app's address with a trailing slash (https://<app>/) as the redirect URI.
      * Sign-in by email (ACCESS_EMAILS): the visitor enters a work email and gets a link <app>/?t=... that works for
        15 minutes, once. Opening it shows a "Sign in" button: mail scanners open links too, so only a click signs in.
      * The browser keeps the session for 30 days in a signed cookie (see _write_cookie); "Sign out" is in the
        sidebar, and with a volume (small volume) also "Sign out on all devices".
      * Personal invite links ?token=<ACCESS_TOKEN>: the token stays in the address, which is how access survives a
        page reload.
      * Streamlit is a single-page app, so the access screen is served with HTTP 200, not 403.
    ACCESS_TOKEN — comma-separated tokens, optionally named: "alice:token1,bob:token2" (as `small access` keeps them);
    ACCESS_REQUEST_URL — where the "Request access" button leads when sign-in by email is off.
    """
    gate = _gate()
    headers = st.context.headers  # sent when the page opened: IP (X-Forwarded-For), Host, language
    lang = gate.lang(headers)
    state = st.session_state
    if state.get("access_granted"):  # a personal link was accepted earlier in this session
        return state.get("access_name", "")

    token = st.query_params.get("token")
    if token is not None:
        name = gate.token_login(token, headers)
        if name is None:
            _screen(gate, lang, "invalid")
            st.stop()
        state.access_granted, state.access_name = True, name
        return name

    params = st.query_params.to_dict()
    if gate.sso and gate.is_sso_return(params):  # back from Google or Microsoft
        kind, session, email = gate.sso_finish(params, st.context.cookies, headers)
        st.query_params.clear()
        state.pop("small_sso", None)  # the state is spent: the next sign-in gets a new one
        if session is not None:
            state.small_session, state.small_write_cookie = session, True
            state.pop("small_signed_out", None)
            st.rerun()
        _screen(gate, lang, kind, email)
        st.stop()

    link = st.query_params.get("t") if gate.emails else None
    if link is not None:
        email = gate.login_email(link)  # checks the link without spending it
        if email is None:
            st.query_params.clear()
            _screen(gate, lang, "expired")
            st.stop()
        if _screen(gate, lang, "confirm", email):
            session = gate.finish_login(link, headers)  # spends the link, once
            if session is not None:
                st.query_params.clear()
                state.small_session, state.small_write_cookie = session, True
                state.pop("small_signed_out", None)
            st.rerun()  # without a session the link is shown as expired
        st.stop()

    session = _session_cookie(gate.cookie_name)
    owner = gate.session_owner(session)
    if owner is not None:
        if state.pop("small_write_cookie", False):
            _write_cookie(gate.cookie_name, state.small_session, gate.max_age)
        with st.sidebar:
            st.caption(LABELS[lang]["signed_in"] % _md(owner))
            st.button(LABELS[lang]["sign_out"], on_click=_sign_out, key="small_sign_out")
            everywhere = gate.everywhere_token(session)
            if everywhere:
                st.button(gate.label("everywhere", lang), on_click=_sign_out_everywhere,
                          args=(gate, everywhere, headers), key="small_sign_out_everywhere")
        return owner

    if state.pop("small_clear_cookie", False):
        _write_cookie(gate.cookie_name, "", 0)
    if state.get("small_mail_sent"):
        _screen(gate, lang, "check")
    elif state.get("small_signed_out_all"):
        _screen(gate, lang, "logout_all", state.small_signed_out_all)
    else:
        _screen(gate, lang, "logout" if state.get("small_signed_out") and gate.emails else "forbidden")
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
