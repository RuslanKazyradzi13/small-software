"""A FastAPI app created by `small init --template fastapi`. Deploy it with `small deploy`.

Railway (Railpack) finds fastapi and uvicorn in requirements.txt and runs `uvicorn main:app` on $PORT by itself.
Locally: `python main.py` (or `uvicorn main:app --reload`).
Who gets in is decided by MagicLinkASGI from magic_link.py: people you invite by work email
(`small access add ivan@company.com`) or with a personal invite link (`small access add bob`).
The API docs (/docs) and websockets are protected too.
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
    """uvicorn writes the full URL to its access log: hide ?token=..., ?t=... (sign-in link) and the sign-on
    ?code=...&state=... so they never reach the Railway logs."""

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(re.sub(r"\b(token|t|code|state)=[^&\s]*", r"\1=***", a) if isinstance(a, str) else a
                                for a in record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(HideToken())

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>small app</title>
</head>
<body style="font-family: system-ui, sans-serif; text-align: center; padding: 48px 16px;">
  <h1>🚀 small + FastAPI</h1>
  <p>This app was created by <code>small init --template fastapi</code>.
     Edit <code>main.py</code> and run <code>small deploy</code>.</p>
  <p><a href="/docs">API docs</a> · <a href="/api/hello?name=small">/api/hello</a></p>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    return PAGE


@app.get("/api/hello")
def hello(name: str = "world"):
    return {"message": "Hello, %s!" % name, "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 8000)))
