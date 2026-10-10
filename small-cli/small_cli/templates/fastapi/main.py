"""FastAPI-приложение, созданное `small init --template fastapi`. Деплой: `small deploy`.

Railway (Railpack) видит fastapi и uvicorn в requirements.txt и сам запускает `uvicorn main:app` на $PORT.
Локально: `python main.py` (или `uvicorn main:app --reload`).
Доступ по ссылке-приглашению https://<домен>/?token=<ACCESS_TOKEN>: токен лежит в small.json, проверку делает
MagicLinkASGI из magic_link.py. Документация API (/docs) и websocket тоже закрыты.
"""

import logging
import os
import re
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from magic_link import MagicLinkASGI

app = FastAPI(title="small app", version="1.0.0")
app.add_middleware(MagicLinkASGI)


class HideToken(logging.Filter):
    """uvicorn пишет в access-лог полный адрес — прячем ?token=..., чтобы он не попал в логи Railway."""

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(re.sub(r"token=[^&\s]*", "token=***", a) if isinstance(a, str) else a
                                for a in record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(HideToken())

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>small app</title>
</head>
<body style="font-family: system-ui, sans-serif; text-align: center; padding: 48px 16px;">
  <h1>🚀 small + FastAPI</h1>
  <p>Приложение создано командой <code>small init --template fastapi</code>.
     Измените <code>main.py</code> и выполните <code>small deploy</code>.</p>
  <p><a href="/docs">Документация API</a> · <a href="/api/hello?name=small">/api/hello</a></p>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    return PAGE


@app.get("/api/hello")
def hello(name: str = "мир"):
    return {"message": "Привет, %s!" % name, "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 8000)))
