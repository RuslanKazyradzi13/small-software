# Changelog

## 0.7.0 — 2026-10-11

### Added
- **`small mcp` — AI agents deploy and share apps.** An MCP server on stdin/stdout for Claude Code, Cursor and other
  MCP clients: *"Deploy this folder and give ivan@company.com access."* Tools: `init_app`, `deploy_app` (streams
  build progress), `share_app`, `revoke_access`, `list_access`, `list_apps`, `app_logs`, `set_env`. There is no
  delete tool: `small destroy` stays in the terminal. Setup: `claude mcp add small -- small mcp`.
- **Share button in `small dashboard`.** Add or revoke emails, whole domains and personal links without a terminal;
  same effect as `small access add` / `revoke`. A new personal link is shown once. The page's state-changing
  requests need a per-run key header and pass `Host`/`Origin`/`Sec-Fetch-Site` checks; the page can't be framed.
- **Sign-in by email in the `streamlit` template.** `require_access()` now uses `MagicLinkGate` — a new public helper
  in `magic_link.py` for frameworks that can't take middleware: the same one-time links, limits, logs and
  session format as the other templates. The session cookie is written by a same-origin frame, so it is not
  HttpOnly; personal `?token=` links work as before.
- **One-command Windows install:** `irm https://raw.githubusercontent.com/RuslanKazyradzi13/small-software/main/install.ps1 | iex`.
  small gets its own venv and a `small.cmd` shim first on PATH, so Smart App Control has no unsigned `small.exe`
  to block. Re-run to upgrade; `-Uninstall` removes it. Tested in CI under Windows PowerShell 5.1 and PowerShell 7.
- English documentation for `magic-link` (Russian moved to `README.ru.md`); code comments are in English.

### Changed
- `small access` logic is split into functions that return data (`list_access`, `grant_access`, `revoke_access`),
  shared by the CLI, the dashboard and the MCP server. CLI output is unchanged.

## 0.6.0 — 2026-10-10

### Added
- **English CLI.** Every message, prompt, error, `--help` text and the `small dashboard` page now exist in English
  and Russian. small picks Russian when the system language is Russian (`LC_ALL`/`LC_MESSAGES`/`LANG`/`LANGUAGE`,
  or the Windows display language) and English otherwise; `SMALL_LANG=en|ru` forces one.
- **On PyPI:** `pip install small-software` (or `pipx install small-software`). Releases are published from GitHub Actions
  with Trusted Publishing — no API tokens stored anywhere.
- **`OWNER_LANG`** for apps: the owner's log lines and Telegram access requests from `magic-link` (and the
  streamlit template) are in English or Russian. `small deploy` sets it to small's language unless `small.json`
  does.
- English CLI reference (`small-cli/README.md`, also the PyPI page); the Russian one moved to `README.ru.md`.

### Changed
- The sample apps from `small init` (python-http, node-http, fastapi, streamlit) are in English: page text and code
  comments. The streamlit access screen follows the visitor's browser language, like `magic-link`.
- `magic-link` log lines and Telegram messages default to English; set `OWNER_LANG=ru` for Russian.
- The `small mail log` hint uses `grep` on macOS/Linux and `Select-String` on Windows.
- Minimum Python is 3.9 (the oldest version CI tests).
- The changelog is in English.

## 0.5.0 — 2026-10-10

### Added
- **Sign-in by work email**, like sharing a Google Doc. `small access add ivan@company.com` (or `@company.com` for
  the whole domain) adds the address to `ACCESS_EMAILS`. The person opens the app, enters their email and gets a
  message with a sign-in link: it works for 15 minutes and only once, and the sign-in happens through a **Sign in**
  button (so mail scanners don't "use up" the link); the browser remembers the sign-in for 30 days. A forwarded
  link gives nothing: only the owner of the address can sign in.
  - Revoking access (`small access revoke ivan@company.com`) also closes a session that was already issued.
  - The response is the same for any address; an unknown address produces an access request in the log and in
    Telegram with a ready-made command.
  - Links and sessions are signed with `SESSION_SECRET` (created on the first `add`); the address in the email
    comes from `PUBLIC_URL`/`RAILWAY_PUBLIC_DOMAIN`, not from the `Host` header; the limit is 3 emails per address
    per 15 minutes.
  - Works in `magic_link.py` (WSGI and ASGI) and `magic-link.js`, that is, in the `python-http`, `fastapi` and
    `node-http` templates. The `streamlit` template supports only personal links for now.
- **`small mail resend|smtp|log|off`** sets how sign-in emails are sent. Resend works over HTTPS, including on
  Railway Hobby, where SMTP is blocked; SMTP is your own server (on Railway, from the Pro plan); `log` writes the
  link to the log for testing. Before the settings are saved, a test email is sent with the same code the app uses.
- `/_access/logout` to sign out.
- **English interface.** Sign-in pages and emails are in Russian or English depending on the browser language
  (`Accept-Language`); `ACCESS_LANG=ru|en` sets the language explicitly.
- MIT license, README in English and Russian, CONTRIBUTING, SECURITY, issue and PR templates.

### Changed
- `small access add` understands addresses and domains; name tokens work as before. `small access list` shows both
  addresses and names.
- `small deploy` creates the domain before uploading the code. With sign-in by email it passes the app
  `PUBLIC_URL` with the real domain: a live check showed that after a domain rename Railway keeps the old address
  in `RAILWAY_PUBLIC_DOMAIN`, and links from emails led to a 404.
- The `python-http` and `fastapi` templates hide not only `?token=` in the access log but also `?t=`, the one-time
  sign-in link from the email.

## 0.4.1 — 2026-10-10

### Fixed
- On Windows, output to a pipe or a file (for example `small logs | Select-String` with a Cyrillic search string,
  or `small link > link.txt`) was written in the ANSI encoding (cp1251), while PowerShell reads it in the console
  encoding: Cyrillic turned into garbage and searching for it found nothing. Now `small` writes to a pipe in the
  console encoding and respects `PYTHONIOENCODING` when it is set, even though `small.cmd` runs Python with `-I`.
  Terminal output is unchanged.
- `small env list` after `small db add` showed `DATABASE_URL … Railway ≠ small.json` even though everything was
  fine: Railway returned the value with the reference already resolved. Now values are compared as written
  (`variables(unrendered: true)`), and `--values` shows `${{Postgres.DATABASE_URL}}` instead of the address with
  the database password.
- README: the sign-in log tip uses `Select-String` instead of `findstr`, which does not find Cyrillic.

## 0.4.0 — 2026-10-10

### Added
- **`small sleep on|off`**: Railway sleep mode (serverless). With no requests the app goes to sleep and uses no
  resources.
- **`small db add|list|remove postgres`**: Postgres from the Railway template in the app's project; the app gets
  `DATABASE_URL` as the reference `${{Postgres.DATABASE_URL}}`. `small destroy` deletes the databases together
  with the app.
- **`small domain add|list|remove`**: a custom domain. Prints the DNS records (CNAME and TXT verification) and shows
  the DNS and certificate state.
- **`small notify telegram|off`**: requests from the **Request access** button reach the owner in Telegram. The
  command checks the bot token, finds the chat ID itself and sends a test message. Supported in `magic_link.py`,
  `magic-link.js` and the `streamlit` template.
- **Dashboard:** a sleep badge and an "≈ $X/mo at current load" estimate (average load over a week from `metrics` ×
  Railway prices), with the total in the summary.

### Changed
- `cli.py` was split into `small_cli/commands/` modules (one file per command) and `common.py`; behavior is
  unchanged.
- Removed `manual-test-app`: it was only a sample of `small init` output.

## 0.3.0 — 2026-10-10

### Added
- **`small env`**: `list` / `set KEY=VALUE` / `unset KEY`. Variables change in Railway and in `small.json` at
  once, then the app restarts once (`--no-deploy` skips the restart). The list shows where each variable is set: in
  both places, only in Railway, or with different values. Values are hidden; `--values` shows them.
- **`small access`**: a token per person. `add NAME` issues a personal link, `revoke NAME` revokes only that link,
  `list` shows who has access. `small link --as NAME` / `small open --as NAME` give a personal link.
  `ACCESS_TOKEN` now understands the `alice:token1,bob:token2` format, compatible with the old one; each sign-in by
  link is written to the log with the person's name.
- **`fastapi` template**: FastAPI + uvicorn; Railway (Railpack) runs `uvicorn main:app` by itself. The token is
  hidden in uvicorn's access log.
- **`MagicLinkASGI`** in `magic_link.py`: protection for ASGI (FastAPI, Starlette), covering HTTP, websocket and
  lifespan.
- **Dashboard:** an "Open via invite link" button (straight in with the owner's token) and a "Logs" button (a window
  with the app or build log).

### Changed
- `magic_link.py`: the shared WSGI and ASGI logic moved into a single core. WSGI behavior is unchanged.
- `small init` suggests `small open` in its "Next" hint.
- CI: the `streamlit` and `fastapi` templates are tested on the real frameworks; Windows job logs are in UTF-8.

## 0.2.0 — 2026-10-10

### Added
- **`small init`**: an app skeleton from a template, `python-http` (default), `node-http` or `streamlit`. Creates
  a server protected by an invite link, `small.json` with a unique name and `ACCESS_TOKEN`, `.gitignore` and
  `.railwayignore`. Existing files are not overwritten without confirmation.
- **`small.json`**: app settings, `name` (project and subdomain name) and `env` (service variables, passed to
  Railway on every deploy). An explicit name binds the folder to the Railway project with the same name, without
  duplicates.
- **`small dashboard`**: a local page listing the apps in a folder. If `RAILWAY_API_TOKEN` is set, it shows the live
  status of the latest deploy (`SUCCESS`, `BUILDING`, `FAILED`…).
- **`small link` / `small open`**: the invite link of a deployed app; `open` opens it in the browser.
- **`small logs`**: the app or build log (`--build`), `-n`, follow mode `-f`.
- **`small destroy`**: remove the app from Railway, either the whole project or only its own service in a shared
  project.
- `small deploy` renames the domain to `<name>.up.railway.app` if that name is free.

### Fixed
- `small.cmd` printed garbled "is not recognized as an internal or external command" errors: `cmd.exe` misparsed
  UTF-8 and LF line endings. The file is now ASCII-only with CRLF (enforced in git by `.gitattributes`).
- A Railway token saved as `"<token>"` (with the angle brackets from the placeholder) broke every request. Spaces,
  quotes and angle brackets around the token are now stripped.

## 0.1.0 — 2026-10-10

- First version: `small deploy` packs a folder, creates a project and a service in Railway, uploads the code,
  creates a public domain, waits for the build and prints the URL.
