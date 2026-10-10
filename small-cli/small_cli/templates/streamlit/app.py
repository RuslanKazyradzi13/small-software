"""Streamlit-приложение, созданное `small init --template streamlit`. Деплой: `small deploy`.

Локально: `python app.py` (или `streamlit run app.py`).
Доступ по ссылке-приглашению https://<домен>/?token=<ACCESS_TOKEN>: токен лежит в small.json,
проверку делает require_access() ниже. Свой интерфейс пишите после вызова require_access().
"""

import hmac
import os
import sys

import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

if __name__ == "__main__" and get_script_run_ctx(suppress_warning=True) is None:
    # Railway запускает `python app.py`; Streamlit так не работает — перезапускаемся через `streamlit run`.
    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", __file__,
                "--server.address", os.environ.get("HOST", "0.0.0.0"),
                "--server.port", os.environ.get("PORT", "8501"),
                "--server.headless", "true",
                "--browser.gatherUsageStats", "false",
                "--client.toolbarMode", "minimal"]  # без кнопки «Deploy» в Streamlit Cloud
    sys.exit(stcli.main())

import numpy as np  # noqa: E402  (numpy и pandas ставятся вместе со streamlit)
import pandas as pd  # noqa: E402

st.set_page_config(page_title="small · Streamlit", page_icon="🚀", layout="wide")


@st.cache_resource
def _warn_once(message):
    print(message, flush=True)


def require_access():
    """Аналог magic_link.py для Streamlit: пускает только по ссылке ?token=<ACCESS_TOKEN>.

    Streamlit не умеет ставить куки, поэтому токен остаётся в адресе — так доступ переживает перезагрузку
    страницы. Без верного токена показывается экран «Доступ по приглашению» с кнопкой «Запросить доступ».
    ACCESS_TOKEN — токены через запятую, можно с именами: "alice:токен1,bob:токен2" (так их ведёт `small access`);
    ACCESS_REQUEST_URL — куда ведёт кнопка.
    """
    if st.session_state.get("access_granted"):
        return
    entries = []  # "alice:токен1,bob:токен2" -> [("alice", "токен1"), ("bob", "токен2")]
    for item in os.environ.get("ACCESS_TOKEN", "").split(","):
        name, _, token = item.strip().rpartition(":")
        if token.strip():
            entries.append((name.strip(), token.strip()))
    if not entries:
        _warn_once("[magic-link] ACCESS_TOKEN не задан: доступ закрыт для всех")
    given = st.query_params.get("token")
    owner = next((name for name, token in entries
                  if given is not None and hmac.compare_digest(given.encode(), token.encode())), None)
    if owner is not None:
        st.session_state.access_granted = True
        print("[magic-link] вход по ссылке%s" % (" (%s)" % owner if owner else ""), flush=True)
        return

    _, center, _ = st.columns([1, 2, 1])
    with center:
        st.markdown("## 🔒 " + ("Ссылка недействительна" if given is not None else "Доступ по приглашению"))
        if given is not None:
            st.write("Ссылка устарела или скопирована не полностью. Запросите новую.")
        else:
            st.write("Это приложение открывается только по ссылке-приглашению от владельца.")
        request_url = os.environ.get("ACCESS_REQUEST_URL", "")
        if request_url:
            st.link_button("Запросить доступ", request_url, type="primary")
        elif st.session_state.get("access_requested"):
            st.success("Запрос отправлен. Владелец увидит его и пришлёт вам ссылку-приглашение.")
        else:
            with st.form("access_request"):
                contact = st.text_input("Email или Telegram для ответа", max_chars=200)
                if st.form_submit_button("Запросить доступ", type="primary"):
                    if contact.strip():
                        # repr() экранирует переводы строк: пользователь не подделает строки лога.
                        print("[access-request] contact=%r" % contact.strip(), flush=True)
                        st.session_state.access_requested = True
                        st.rerun()
                    else:
                        st.warning("Укажите, как с вами связаться.")
    st.stop()


require_access()

# ---------- Ваше приложение ----------

st.title("🚀 small + Streamlit")
st.caption("Шаблон интерактивного приложения. Измените `app.py` и выполните `small deploy`.")

with st.sidebar:
    st.header("Параметры")
    points = st.slider("Точек", min_value=20, max_value=500, value=120, step=10)
    volatility = st.slider("Волатильность", min_value=0.1, max_value=5.0, value=1.0, step=0.1)
    seed = st.number_input("Seed", value=42, step=1)
    kind = st.radio("Тип графика", ["Линия", "Область", "Столбцы"], horizontal=True)

rng = np.random.default_rng(int(seed))
data = pd.DataFrame(rng.normal(0, volatility, size=(points, 3)).cumsum(axis=0), columns=["Альфа", "Бета", "Гамма"])

for column, name in zip(st.columns(3), data.columns):
    column.metric(name, "%.2f" % data[name].iloc[-1], "%+.2f" % (data[name].iloc[-1] - data[name].iloc[0]))

{"Линия": st.line_chart, "Область": st.area_chart, "Столбцы": st.bar_chart}[kind](data)

with st.expander("Данные"):
    st.dataframe(data.round(3))
    st.download_button("Скачать CSV", data.to_csv(index_label="t").encode("utf-8"), "data.csv", "text/csv")
