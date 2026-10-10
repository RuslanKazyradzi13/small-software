# magic-link: app access by work email or invite link

[Русская версия](https://github.com/RuslanKazyradzi13/small-software/blob/main/magic-link/README.ru.md)

An access gate for micro-apps: one dependency-free file per language. There are two ways to let someone in, and you
can use both at once:
- **by work email**, like sharing a Google Doc: the owner lists the addresses, a visitor enters their email and
  signs in with the link they receive;
- **by invite link** with a token, either shared or personal for each person.

| File | What it's for |
|---|---|
| [magic_link.py](magic_link.py) | `MagicLink` for WSGI (Flask, Django, Bottle, `wsgiref`); `MagicLinkASGI` for ASGI (FastAPI, Starlette); Python 3.8+ |
| [magic-link.js](magic-link.js) | `(req, res, next)` middleware for Express, Connect or plain `http`; Node 18+ |

Copy the file you need into your app folder: `small deploy` uploads only that folder. The easiest way to start is
`small init`, which puts these files into the new app for you. Copies of them live in
`small-cli/small_cli/templates/`. If you change the files here, copy them there as well: the `small-cli` tests fail
when the copies drift apart.

## Sign-in by work email

1. The owner adds addresses to `ACCESS_EMAILS`: `ivan@company.com, @company.com` (`@domain` allows every address on
   that domain). With `small`: `small access add ivan@company.com`.
2. A visitor opens the app and sees "Sign in with your work email". They enter their address and get an email with
   a link.
3. The link works **for 15 minutes and only once**. Opening it shows a page with a "Sign in" button, and only
   clicking it signs the person in. That's because corporate mail scanners open links in incoming email on their
   own and would otherwise use them up. After sign-in, the `small_access` cookie (HttpOnly, SameSite=Lax, Secure
   over HTTPS) keeps the person signed in for 30 days.
4. The log gets `[magic-link] signed in by email (ivan@company.com), ip=…` (log lines follow `OWNER_LANG`, English
   by default).

Security measures:
- **Nothing to forward.** Only someone who reads a listed mailbox can get in.
- **The list stays private.** Every address gets the same answer: "Check your email". An unknown address turns into
  an access request: an `[access-request]` line in the log and a Telegram message with a ready-to-run
  `small access add <address>` command.
- **Revocation is immediate.** Remove an address from `ACCESS_EMAILS` and, after a restart, even a cookie that was
  already issued stops working.
- **Signatures.** Sign-in links and sessions are signed with HMAC-SHA256 using `SESSION_SECRET`, each with its own
  purpose: a sign-in link can't pass for a session, and vice versa.
- **The link in the email can't be spoofed.** It's built from `PUBLIC_URL` or `RAILWAY_PUBLIC_DOMAIN`, not from the
  `Host` header (which is trusted only for localhost). Otherwise an attacker could inject their own domain, and the
  sign-in link from a genuine email would lead to them.
- **Rate limits.** At most 3 sign-in emails per address and 10 requests per IP within 15 minutes.

Sending email:
- **Resend** (`RESEND_API_KEY`, `MAIL_FROM`) works over HTTPS, so it also works on Railway Hobby, where SMTP ports
  are blocked ([Railway docs](https://docs.railway.com/networking/outbound-networking)). Without a verified domain,
  Resend delivers only to the account owner's address.
- **SMTP** (`SMTP_URL=smtp://user:password@host:587` or `smtps://…:465`) uses your own mail server. On Railway it
  requires the Pro plan. Python version only.
- **`LOGIN_LINKS_TO_LOG=1`** writes the link to the log instead of emailing it. For testing only.

With `small`, `small mail` sets all of this up: `small mail resend` and `small mail smtp` send a test email first and
save the settings only after it goes through; `small mail log` turns on log mode, and `small mail off` turns email
off.

## Invite link

1. The owner shares a link like `https://<app>.up.railway.app/?token=<token>`.
2. When it's opened, the middleware sets the `small_access` cookie (30 days) and redirects to the same address
   **without** `token`, so the token doesn't stay in the browser history. The log gets
   `[magic-link] signed in by link (name), ip=…`, with no token in it.
3. Without access, the visitor gets a **403**: the email sign-in form if `ACCESS_EMAILS` is set, otherwise a
   "Request access" button. The button leads to `ACCESS_REQUEST_URL` (`mailto:`, Telegram, a Google Form); if that
   isn't set, the page shows a form with an "Email or Telegram to reach you" field. The request is printed to the
   log as an `[access-request] ...` line and, if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set, sent to
   Telegram. It's sent in the background as plain text, with no markup parsing; if Telegram is down, the visitor
   still sees "Request sent".

The cookie holds a value derived from the token, not the token itself. If you remove one person's token from
`ACCESS_TOKEN` (and restart), their link and their cookie stop working; everyone else is unaffected.

```
small access add bob       # give bob a personal link
small access revoke bob    # revoke it
```

To set tokens by hand: the name goes before the colon, and the token itself must not contain a colon. To generate a
token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Environment variables

| Variable | Meaning |
|---|---|
| `ACCESS_EMAILS` | comma-separated addresses; `@company.com` allows the whole domain |
| `SESSION_SECRET` | key that signs sign-in links and sessions. If it's not set, a random key is generated on every start, so everyone has to sign in again after a restart. `small access add <address>` creates it for you |
| `RESEND_API_KEY`, `MAIL_FROM` | send email via Resend; `MAIL_FROM` is the sender (used for SMTP too), e.g. `Reports <noreply@company.com>`. If it's not set: `small <onboarding@resend.dev>` for Resend, the login from `SMTP_URL` for SMTP |
| `SMTP_URL` | send email via SMTP (Python only) |
| `LOGIN_LINKS_TO_LOG` | `1` writes the sign-in link to the log instead of emailing it (for testing) |
| `PUBLIC_URL` | the app's address for links in emails, such as your own domain; defaults to `https://$RAILWAY_PUBLIC_DOMAIN`, and locally to the `Host` address if it's localhost. After you rename a domain, Railway keeps the old address in that variable, so `small deploy` and `small access add` set `PUBLIC_URL` for you. If the middleware is mounted under a path prefix (`app.use('/admin', …)`, `SCRIPT_NAME`, `root_path`), include the prefix in `PUBLIC_URL`: it isn't appended to it |
| `ACCESS_LANG` | language of pages and emails: `ru`, `en` or `auto` (default: based on the browser language, Russian for Russian speakers and English for everyone else; Russian when there's no `Accept-Language` header) |
| `OWNER_LANG` | language of owner-facing messages, i.e. log lines and Telegram notifications: `en` (default) or `ru`. `small deploy` sets it to match the language `small` runs in |
| `ACCESS_TOKEN` | comma-separated invite-link tokens, optionally named: `alice:token1,bob:token2` |
| `ACCESS_REQUEST_URL` | where the "Request access" button leads when email sign-in is off |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | access requests reach the owner in Telegram (`small notify telegram`) |

If neither `ACCESS_EMAILS` nor `ACCESS_TOKEN` is set, nobody gets in.
`/_access/logout` signs out (the cookie is deleted).

## Setup

**Python, WSGI**

```python
from magic_link import MagicLink

application = MagicLink(application)        # any WSGI app
app.wsgi_app = MagicLink(app.wsgi_app)      # Flask
```

For Django, in `wsgi.py`: `application = MagicLink(get_wsgi_application())`.
Example: the [python-http/app.py](../small-cli/small_cli/templates/python-http/app.py) template.

**Python, ASGI**

```python
from magic_link import MagicLinkASGI

app.add_middleware(MagicLinkASGI)           # FastAPI / Starlette
app = MagicLinkASGI(app)                    # any ASGI app
```

Every HTTP request is protected, including `/docs`. A websocket without a valid cookie is closed before the
connection is accepted, and the server responds with 403. Lifespan events pass through unchecked. Example: the
[fastapi/main.py](../small-cli/small_cli/templates/fastapi/main.py) template; it also has a filter that hides
`?token=` and `?t=` (the sign-in link) from the `uvicorn` access log.

In both classes, the constructor arguments `tokens=`, `emails=`, `secret=` and `request_url=` override the
environment variables (`tokens` and `emails` take a comma-separated string or a list), while `cookie_name=` and
`max_age=` (in seconds) set the cookie's name and lifetime.

**Node.js**

```js
const magicLink = require('./magic-link');

app.use(magicLink());                                   // Express: the whole app
app.use('/admin', magicLink());                         // or just one section

const guard = magicLink();                              // plain http
http.createServer((req, res) => guard(req, res, () => handler(req, res)));
```

Options: `{ tokens, emails, secret, requestUrl, lang, cookieName, maxAge }`; `lang` overrides `ACCESS_LANG`, and the
rest work as in the Python version. Email goes through Resend only: Node can't send SMTP without dependencies.

### Frameworks that can't take middleware (Streamlit)

`MagicLinkGate` is the same gate without HTTP: the same environment variables, signatures, one-time links, email
limits, logs and texts, but the app draws the forms and reads/sets the session cookie (`gate.cookie_name`) itself.
Create one per process: it remembers used links and email counters. The `small` Streamlit template is built on it.

```python
from magic_link import MagicLinkGate

gate = MagicLinkGate()
gate.session_owner(cookie_value)       # who is signed in: email, ACCESS_TOKEN name, or None
gate.token_login(token, headers)       # ?token=: name ("" for the shared link) or None
gate.start_login(email, headers)       # sends the ?t= link: "check" for any address, "bad_email" or "slow"
gate.login_email(t)                    # who the ?t= link is for (does not spend it), or None
gate.finish_login(t, headers)          # on the button click only: spends the link, returns the cookie value or None
gate.request_access(contact, headers)  # access request to the log and Telegram
```

`headers` is any mapping of request headers (case-insensitive): IP, Host, User-Agent and language come from it. Sessions
are interchangeable with `MagicLink`/`MagicLinkASGI` that share the same `SESSION_SECRET`.

## What this gate doesn't do

- **No passwords or roles.** Email sign-in proves that the person owns the address; everyone who signs in has the
  same rights.
- **An invite link is a password in the URL.** Whoever has it gets in; for colleagues, email sign-in is the better
  choice. The first `/?token=...` request ends up in the access logs of your server and of the Railway proxy, and
  so does the sign-in link from an email (`/_access/verify?t=...`). The `python-http` and `fastapi` templates mask
  both values in the server logs (`token=***`, `t=***`). Do the same in your own server (gunicorn, morgan).
- **One-time links and rate limits live in process memory.** That's enough for a micro-app running as a single
  instance; with several instances, a link can be used once on each of them.
- **Revocation takes effect after a restart:** the lists are read at startup. `small access revoke` restarts the app
  for you.
- Sensitive data calls for proper authorization with roles and an audit trail.

## Tests

From the `magic-link` folder:

```bash
python -m unittest test_magic_link     # 51: WSGI, ASGI, email sign-in, languages, personal tokens, Telegram, Resend, SMTP
node --test                            # 20
```

What's covered:
- 403 with the sign-in form or the "Request access" button;
- the redirect without the token and the cookie flags, Secure behind a proxy, open redirects;
- an invalid token, a forged cookie, personal tokens and their revocation;
- email sign-in: the email, the "Sign in" button (a scanner doesn't use up the link), single use, the 15-minute
  expiry;
- the same answer for known and unknown addresses, access requests, whole-domain access;
- revoking an address ends a session that was already issued; forged sessions and a different key are rejected;
- the email rate limit, an invalid address, the `Host` header not affecting the link, `PUBLIC_URL`, signing out;
- sending via Resend and SMTP, errors logged without keys or passwords, log mode;
- the access-request log without line injection, Telegram, and Telegram being unavailable;
- page and email language (`Accept-Language`, `ACCESS_LANG`) and the owner's language (`OWNER_LANG`);
- for ASGI, websockets and lifespan as well.
