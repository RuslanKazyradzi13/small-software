# small: deploy to Railway in one command

[Русская версия](https://github.com/RuslanKazyradzi13/small-software/blob/main/small-cli/README.ru.md)

`small` deploys small team apps to Railway in one command and protects them with sign-in by work email or
personal invite links.

```
small init my-service    # skeleton: invite-link protected server, small.json, .gitignore
cd my-service
small deploy             # → https://my-service-3f9a.up.railway.app
small open               # open with the invite link
small access add ivan@company.com   # sign-in by work email, like sharing a Google Doc
small mail resend        # how sign-in emails are sent
small access add bob     # or a personal link for bob (small access revoke bob to revoke it)
small notify telegram    # access requests to Telegram
small env set DEBUG=1    # environment variable in Railway and small.json
small db add postgres    # database, DATABASE_URL in the app
small domain add app.example.com   # custom domain
small sleep on           # sleep when idle (saves money)
small logs -f            # app logs
small dashboard          # all apps in the browser: status, cost, logs
small destroy            # remove from Railway together with databases
```

You can also deploy an existing code folder (`app.py`, `main.py`, `package.json` and so on): `small init` is
optional.

Python 3.9+, no dependencies.

## Install

```bash
pip install small-software
```

The command is `small`. With [pipx](https://pipx.pypa.io): `pipx install small-software`.

From source, in a clone of the repository:

```bash
pip install ./small-cli
```

**Windows.** `pip install` creates `small.exe`, which Smart App Control / Application Control may block because it
is unsigned. If that happens, either:

- run small through Python, which works everywhere: `python -m small_cli deploy` (any command works this way);
- or add the `small-cli` folder of a clone to `PATH`, and `small.cmd` will serve the `small` command. Run this in
  the root of the cloned repository (`$PWD` expands to its path), then open a new terminal:

```powershell
[Environment]::SetEnvironmentVariable("Path", $env:Path + ";$PWD\small-cli", "User")
```

## Language

small speaks English and Russian. It uses Russian when the system language is Russian (`LC_ALL`, `LC_MESSAGES`,
`LANG`, `LANGUAGE`, or the Windows display language) and English otherwise. To choose explicitly, set `SMALL_LANG`
to `en` or `ru`:

```bash
export SMALL_LANG=en
```

```powershell
$env:SMALL_LANG = "en"
```

Deployed apps pick their language separately:

- **Sign-in pages and emails** follow each visitor's browser language. `ACCESS_LANG=ru` or `ACCESS_LANG=en` forces
  one language.
- **App log lines and Telegram notifications** are for the owner and follow `OWNER_LANG=en|ru`. `small deploy` sets
  `OWNER_LANG` to small's own language unless `small.json` sets it. It does this only when `small.json` has at
  least one variable in `env` (apps created by `small init` always do); an app with an empty `env` gets no
  variables from `small deploy` at all.

## Token

You need an **account token** or a **workspace token** (Railway → Account Settings → Tokens). A project token will
not work: it cannot create projects.

Save the token to the `RAILWAY_API_TOKEN` variable, replacing `YOUR_TOKEN` entirely. Only the token itself, in the
form `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`, should remain inside the quotes:

```powershell
[Environment]::SetEnvironmentVariable("RAILWAY_API_TOKEN", "YOUR_TOKEN", "User")  # permanently
$env:RAILWAY_API_TOKEN = "YOUR_TOKEN"                                          # this terminal only
```

On macOS / Linux:

```bash
export RAILWAY_API_TOKEN="YOUR_TOKEN"   # add this line to ~/.bashrc or ~/.zshrc to keep it
```

A variable saved "permanently" is visible to new terminal windows. Terminals and apps that are already open do not
see it; restart them. `small` strips spaces, quotes and angle brackets around the token by itself.

Fallback variable name: `RAILWAY_TOKEN`.

## `small init`: app skeleton

```
small init [folder] [--template python-http|node-http|fastapi|streamlit] [--name NAME] [--force]
```

Creates a ready-to-deploy micro-app in the folder (the current one by default; created if it does not exist).
`--template` (short: `-t`) picks the template, `python-http` by default:

| File | What it is |
|---|---|
| `app.py` + `magic_link.py` (`python-http`, default) | web server on the standard library (`wsgiref`); all your code goes in `app()` |
| `server.js` + `magic-link.js` + `package.json` (`node-http`) | web server on the `http` module, Node 18+; all your code goes in `handler()` |
| `main.py` + `magic_link.py` + `requirements.txt` (`fastapi`) | FastAPI + uvicorn API: a page, `/api/hello`, docs at `/docs`; protected by `MagicLinkASGI`, the token is hidden in uvicorn logs. Railway runs `uvicorn main:app` by itself |
| `app.py` + `requirements.txt` (`streamlit`) | interactive [Streamlit](https://streamlit.io) app: sidebar with settings, metrics, a chart, a table with CSV download; invite-link sign-in is built into `app.py` |
| `small.json` | unique project name and a generated `ACCESS_TOKEN` in `env` |
| `.gitignore` | `small.json`, `.small.json`, `.env` and language clutter (`__pycache__/`, `node_modules/`…) |
| `.railwayignore` | what belongs in git but not in Railway: `*.md`, `tests/`, IDE settings |

The sample apps are in English: both the page text and the code comments.

- **Invite-link protection.** Templates are protected from the start, based on
  [magic-link](https://github.com/RuslanKazyradzi13/small-software/blob/main/magic-link/README.md).
  `small deploy` passes `ACCESS_TOKEN` from `small.json` to Railway. The app opens at
  `https://<domain>/?token=<ACCESS_TOKEN>`; without the token visitors get a 403 page with a **Request access**
  button. The server listens on `$HOST:$PORT`, by default `0.0.0.0` and the `PORT` set by Railway.
- **The `streamlit` template.** Streamlit is not a WSGI app, so `magic_link.py` cannot wrap it. The same logic is
  built into `app.py` as `require_access()`: the same `ACCESS_TOKEN` and `ACCESS_REQUEST_URL`, an "Invite only"
  screen and a **Request access** form that writes an `[access-request]` line to the log. Like magic-link, the
  access screen follows the visitor's browser language (`ACCESS_LANG` forces one). There are two differences.
  Streamlit cannot set cookies, so the token stays in the address bar, which is how access survives a page reload.
  And Streamlit is a single-page app, so the "no access" screen is served with HTTP 200, not 403.
  Railway runs `python app.py`, and `app.py` restarts itself through `streamlit run` on `$HOST:$PORT`.
  Run it locally the same way: `python app.py`.
- **Name.** By default, the folder name plus a random suffix, for example `my-service-3f9a`. An explicit name in
  `small.json` binds deploys to the Railway project with the same name, so the name must not accidentally match
  someone else's project. The suffix also makes it more likely that the `<name>.up.railway.app` subdomain is free.
  `--name` sets a name without the suffix.
- **`small.json`** is added to `.gitignore` because it holds the token. Do not publish it.

**Existing files are not overwritten.** If the folder already has files the template creates:
- in a terminal, `small init` lists them and asks `Overwrite? [y/N]`;
- without a terminal (script, CI), the command fails. Either way, if you decline, nothing in the folder changes.

`--force` overwrites without asking, including `small.json`: you get a new name and a new `ACCESS_TOKEN`, and old
invite links stop working after the next deploy. `.gitignore` and `.railwayignore` are never overwritten: only
missing lines are appended, so running `init` again does not duplicate anything.

## `small dashboard`: all apps in the browser

```
small dashboard [folder] [--port N] [--no-browser]
```

Starts a local server and opens a browser with all micro-apps in a folder: the current one by default, or any
other, for example `small dashboard ..`. An app is a folder with `small.json` and/or `.small.json`.

For each app the dashboard shows:
- name: from `small.json`, otherwise from `.small.json`, otherwise the folder name;
- status: **Linked to Railway** (`.small.json` has a service ID) or **Not deployed**;
- **live status of the latest deploy** from Railway (`SUCCESS`, `BUILDING`, `FAILED`, `CRASHED`…), if a token is
  set;
- **auto-sleep**: sleep mode is on (`small sleep on`);
- **cost estimate** in $/month at the current load (see below), with the total for all apps in the summary line;
- the public URL and a link to the service in the Railway console, if the app has been deployed;
- the folder path;
- names of the variables from `small.json`, without values;
- errors in `small.json` / `.small.json`: the same checks as `small deploy` runs.

The **Refresh** button rescans the folders without a restart. Stop the server with `Ctrl+C`.

Buttons on the card of a deployed app:
- **Open via invite link** opens the app right away with the owner's token, skipping the "invite only" screen.
  The token only goes to the browser's address bar on navigation; it is not in the dashboard page data.
- **Logs** opens a window with the last 200 lines of the app or build log (tabs **App** / **Build**).
  Requires `RAILWAY_API_TOKEN`; without it the button is disabled.

- **Port** 3000 by default; if it is busy, any free port is used. The URL is printed to stdout.
- **The server listens on `127.0.0.1` only** and answers 403 to requests with a foreign `Host` (DNS rebinding
  protection). Values of the variables from `small.json` are never served: they include `ACCESS_TOKEN`.
- **Search** goes up to 6 levels deep. `.git`, `node_modules`, `__pycache__`, `.venv` and `venv` are skipped, and
  the scanner does not descend into an app it has found.
- Styles are loaded from the Tailwind CDN; without internet access the page works, just unstyled.

### Live deploy statuses

If `RAILWAY_API_TOKEN` (or `RAILWAY_TOKEN`) is set at startup, on every refresh the dashboard asks Railway for the
latest deploy of each linked app with the `serviceInstance(serviceId, environmentId) { latestDeployment }` query.
The status is shown as a colored badge:

| Color | Statuses |
|---|---|
| green | `SUCCESS` |
| light blue | `SLEEPING` |
| pulsing yellow | `QUEUED`, `WAITING`, `INITIALIZING`, `BUILDING`, `DEPLOYING` |
| red | `FAILED`, `CRASHED` |
| gray | everything else |

The badge tooltip shows the deploy time.

- **Without a token** the dashboard works offline from local files and never calls Railway.
- **Errors** (no network, the 5 s timeout, a wrong token, a deleted service) do not crash the server: those cards
  simply stay without a live status, and the summary line notes that Railway didn't respond.
- **Speed.** Requests run in parallel, so even a slow Railway delays a refresh by no more than the timeout.
- **Security.** The Railway token stays in the dashboard process and is never sent to the browser.

### Cost estimate

The dashboard takes the project's load measurements for the past week from Railway (the `metrics` query: average
CPU cores, GB of memory and GB of volumes per hour, including the app's databases) and multiplies the average load
by the monthly prices from the [Railway docs](https://docs.railway.com/reference/pricing/plans): $20 per vCPU,
$10 per GB of memory, $0.15 per GB of volume. The result reads "≈ $X/mo at current load".
- **Sleep and new apps.** Hours when the app was asleep (`small sleep`) count as zero. For a new app the average
  starts at its first measurement, so the estimate is not understated.
- **Why not `usage`.** The accumulated `usage` arrives with a delay. In a live check it differed from the actual
  measurements by 7–50×, and differently for CPU and memory, so an honest total cannot be computed from it.
- **This is an estimate, not a bill.** Egress ($0.05/GB), the plan's free allowance ($5 on Hobby, $20 on Pro) and
  the subscription are not included. The exact amount is in Railway → Usage.

## `small deploy`

```
small deploy [folder] [--name NAME] [--workspace ID] [--port N] [-m MESSAGE] [--new] [--no-wait] [--timeout SEC]
```

| Flag | What it does |
|---|---|
| `--name` | project, service and subdomain name; overrides `name` from `small.json` (default: the folder name normalized to `a-z0-9-`) |
| `--workspace` | workspace ID if the account has several (or `RAILWAY_WORKSPACE_ID`) |
| `--port` | port for the public domain if the app does not listen on `$PORT` |
| `-m`, `--message` | deploy message shown in Railway |
| `--new` | create a new project even if the folder has been deployed before |
| `--no-wait` | print the URL right away without waiting for the build |
| `--timeout` | how many seconds to wait for the build (default 900) |

Progress goes to stderr and the final URL to stdout: `URL=$(small deploy)`.
Exit codes: `0` success, `1` error or failed build (the last log lines are printed), `130` interrupted.

## After deploy

These commands work in the folder of a deployed app, that is, where `.small.json` is. `link`, `open`, `logs` and
`destroy` take the path as an argument; the others take it as `-C folder`.

```
small link [folder] [--as NAME]                       # print the invite link
small open [folder] [--as NAME]                       # the same + open it in the browser
small access [list | add EMAIL|@DOMAIN|NAME | revoke EMAIL|NAME]   # who can sign in
small mail [resend | smtp [--from FROM] [--to TO] | log | off]  # emails for sign-in by email
small notify [telegram [--chat-id ID] | off]        # access requests to Telegram
small env [list [--values] | set K=V ... | unset K ...] [--no-deploy]
small db [list | add postgres | remove postgres [--yes]]
small domain [list | add DOMAIN [--port N] | remove DOMAIN]
small sleep [on | off]                              # sleep mode; no argument shows the status
small logs [folder] [--build] [-n N] [-f]            # log of the latest deploy
small destroy [folder] [--yes]                       # remove from Railway
```

Commands that change the app's variables (`access add` / `revoke`, `mail`, `notify`, `env set` / `unset`,
`db add` / `remove`) and `sleep on` / `off` restart the app once. Add `--no-deploy` to skip the restart; the change
then takes effect on the next deploy.

**`small link` / `small open`** build `https://<domain>/?token=<token>`.
- **Where the token comes from.** From `small.json`, or, if it is not there (for example, the token was set in the
  Railway dashboard), from the service variables in Railway; that requires `RAILWAY_API_TOKEN`. Without `--as` the
  first token (the owner's) is used; `--as bob` gives bob's personal token.
- **If there is no token anywhere**, the address is printed without a token.
- **Output.** The link goes to stdout, so you can pass it on: `small link | clip`. It works like a password, so
  send it only to people who need access.

**`small access`** controls who can open the app. There are two ways, and you can combine them.

*By work email*, like sharing a Google Doc. This is the recommended way:
- **`add ivan@company.com`** adds the address to `ACCESS_EMAILS`. Ivan opens the app, enters his email and gets a
  message with a sign-in link: it works for 15 minutes and only once, and the browser remembers the sign-in for
  30 days. Only someone who reads that mailbox can sign in, so passing the link on to someone else gives them
  nothing.
- **`add @company.com`** lets in everyone with an address in that domain.
- **`revoke ivan@company.com`** closes sign-in right after the restart, even in a browser that is already open.
- The first `add` creates `SESSION_SECRET`, the key that signs links and sessions. Email sending must be set up:
  `small mail`.
- `small` passes the address for links in emails to the app itself, in `PUBLIC_URL` (in Railway only): after the
  domain is renamed to `<name>.up.railway.app`, Railway keeps the old address in `RAILWAY_PUBLIC_DOMAIN`, and the
  links would lead to a 404. If you want links on your own domain, set `PUBLIC_URL` in `small.json`; `small` does
  not touch it.
- Someone who is not on the list and enters their email gets the same "Check your email" response, and the owner
  receives an access request (in the log, and in Telegram if connected) with a ready-made
  `small access add their@address` command.
- Works in the `python-http`, `fastapi` and `node-http` templates. `streamlit` supports personal links only for
  now.

*By personal invite link*:
- **`add bob`** creates a token, adds it to `ACCESS_TOKEN` as `bob:token`, restarts the app and prints the link.
  The link works like a password: anyone it is forwarded to can get in.
- **`revoke bob`**: after the restart neither bob's link nor the cookie he already has works; everyone else keeps
  their access.

- **`list`** shows who has access: addresses and names (tokens are hidden).
- **Sign-in log.** Sign-ins show up in the log: `small logs | Select-String "signed in by"` (PowerShell; on
  macOS/Linux use `grep`) → `signed in by email (ivan@company.com)` or `signed in by link (bob)`. With
  `OWNER_LANG=ru` these lines are in Russian; search them with `Select-String`, not `findstr`, which reads the
  argument and the text in different encodings and does not find Cyrillic.

**`small mail`** sets how the app sends sign-in emails.
- **`resend`** sends through [Resend](https://resend.com) over HTTPS. `small` takes the key from `RESEND_API_KEY`
  or asks for it with hidden input. Railway's Hobby plan blocks SMTP ports, so on Hobby this is the main option
  ([Railway docs](https://docs.railway.com/networking/outbound-networking)). Without a domain verified in Resend,
  emails go only to your Resend account's address; for colleagues, verify a domain and pass
  `--from "Reports <noreply@company.com>"`.
- **`smtp`** uses your own mail server: `SMTP_URL` in the form `smtp://user:password@server:587` (or
  `smtps://…:465`), taken from the environment or asked for with hidden input. On Railway it works from the Pro
  plan.
- Before saving, `resend` and `smtp` send a test email (`--to address`) with the same code the app uses. If it
  does not go through, nothing is saved and the reason is printed. `--from` and `--to` work with both.
- **`log`** writes the sign-in link to the app log instead of sending an email. For testing only: a link from the
  log lets you into the app.
- **`off`** turns email off; with no subcommand you get the status (`resend`, `smtp`, `log` or `off`).

**`small notify`** sends access requests to the owner in Telegram.
- **`telegram`** connects a bot. Create one with [@BotFather](https://t.me/BotFather) (`/newbot`). `small` takes
  the bot token from `TELEGRAM_BOT_TOKEN` or asks for it with hidden input, so it does not end up in your terminal
  history. Then `small` asks you to send the bot any message, finds the chat ID itself, sends a test notification
  and writes `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to Railway and `small.json`. You can pass the chat ID right
  away with `--chat-id`.
- **What a notification looks like.** When someone clicks **Request access** and leaves a contact, you get a
  message with the app's address, the contact, the IP and a hint: `small access add NAME`. Works in all templates
  (`python-http`, `fastapi`, `node-http`, `streamlit`) and in `magic-link` in general. The request is still written
  to the log as well.
- **`off`** disconnects the bot; with no subcommand you get the status.

**`small db`** adds a database in one step.
- **`add postgres`** deploys Postgres from the Railway template (like `railway add --database postgres`) into the
  app's project and passes `DATABASE_URL` to the app as the Railway reference `${{Postgres.DATABASE_URL}}`. The
  password is stored only in Railway; `small.json` gets just the reference. The app reaches the database over
  Railway's private network.
- **Startup.** The database takes about a minute to start: if the app connects to it at startup, the first start
  may happen before the database is ready.
- **`list`** shows the app's databases and their status.
- **`remove postgres`** deletes the database together with its data (confirm by typing the name, or pass `--yes`)
  and removes `DATABASE_URL`.
- **`small destroy`** deletes the databases together with the app.

**`small domain`** sets up a custom domain instead of `*.up.railway.app`.
- **`add app.example.com`** registers the domain in Railway and prints the DNS records for your registrar: a CNAME
  pointing to the Railway address and, if Railway asks for it, a TXT record that verifies ownership.
- **Certificate.** Once the records are live (from minutes to a day), Railway issues the HTTPS certificate by
  itself.
- **Invite links** work on the custom domain too: `https://app.example.com/?token=…`.
- **`list`** shows the domains and their state (waiting for DNS records / DNS configured) and the certificate
  status.
- **`remove`** removes the domain.

**`small sleep`** controls Railway's sleep mode (serverless).
- **`on`**: with no incoming requests the app goes to sleep and uses no CPU or memory; the first request wakes it
  up in a few seconds. Handy for apps that are opened a couple of times a day.
- **`off`**: the app always runs. With no argument you get the current mode.
- **Restart.** The setting takes effect after a restart, which `small sleep` does by itself (`--no-deploy`
  postpones it until the next deploy).

**`small env`** manages the app's environment variables.
- **`list`** shows the variables and where they are set: `small.json + Railway`, Railway only, or
  `Railway ≠ small.json`. Values are hidden; `--values` shows them. Internal `RAILWAY_*` variables are not listed.
- **`set` / `unset`** change variables in both Railway and `small.json` at once; otherwise the next `small deploy`
  would bring back the old values. After a change Railway restarts the app once; `--no-deploy` turns that off. If
  the app has not been deployed yet, only `small.json` changes.
- **Removed variables.** If you removed a variable from `small.json` by hand, delete it with `small env unset` so
  that it disappears from Railway too.

**`small logs`** shows the log of the latest deploy.
- **Which log.** The app log by default, the build log with `--build`. `-n` sets the number of lines (default 100).
- **Follow mode.** `-f` appends new lines every 2 seconds; `Ctrl+C` exits.
- **Format.** Every line is printed with its local time. Requests from the **Request access** button show up as
  `[access-request] ...`, and they are easy to filter: `small logs | findstr access-request`.
- **Limitation.** `-f` follows the deploy that was the latest when the command started; after a new deploy, run
  the command again.

**`small destroy`** removes the app from Railway.
- **What gets deleted.** If the project contains only this app's services (the service itself and the databases
  from `small db`), the whole project is deleted: services, domains, variables, deploys, database data. If
  `small deploy` once linked the folder to a project that also has other services, only the app's services are
  deleted and the rest stay.
- **Confirmation.** In a terminal you have to type the project name. Without a terminal (script, CI) the app is
  deleted only with the `--yes` flag. If you decline, nothing is deleted.
- **Local files.** After the deletion `.small.json` is removed, while `small.json` stays, so the next
  `small deploy` creates a new project with the same settings.

## Settings: `small.json`

An optional file in the root of the app folder:

```json
{
  "name": "my-custom-app-name",
  "env": {
    "OPENAI_API_KEY": "sk-...",
    "DEBUG": "true"
  }
}
```

| Field | What it does |
|---|---|
| `name` | Project and service name in Railway, and the desired subdomain `<name>.up.railway.app`. Normalized to `a-z0-9-`. The `--name` flag takes precedence. |
| `env` | The service's environment variables. Names: Latin letters, digits and `_`, not starting with a digit. Values: strings, numbers or `true`/`false` (sent to Railway as strings: `"true"`, `"2"`). |

Both fields are optional; any other field is an error (this catches typos like `"envs"`). An error in the file
stops the deploy before any request to Railway.

**Name.** While the folder is not linked to a service (there is no `.small.json`), an explicitly set name (from
`small.json` or `--name`) works as an identifier: if Railway already has a project with that name, for example
after a deploy from another computer, `small` connects to it instead of creating a duplicate. If there is no such
project, it is created. If there are several projects with that name, the deploy stops with an error. `--new`
always creates a new project. If the folder is already linked, a new name is not applied, and `small` warns you
about it. To link the folder to another project, delete `.small.json`.

**Variables** are written on every `small deploy`, before the code is uploaded, so the new build sees them right
away. small uses `variableCollectionUpsert`, the batch form of `variableUpsert`, with `replace: false`: variables
from `env` are added or updated, and the service's other variables (for example, an `ACCESS_TOKEN` set in the
dashboard) are left alone. A variable removed from `env` **stays** in Railway: delete it with `small env unset`
or in the dashboard (service → Variables). Only variable names are printed, never values.

Unless `env` sets them itself, `small deploy` also passes the app `PUBLIC_URL` (when sign-in by email is on, see
`small access`) and `OWNER_LANG` (small's language, see "Language" above). With an empty `env`, `small deploy`
writes no variables at all, `OWNER_LANG` included.

**Secrets.** The root `small.json` is not uploaded to Railway with the code. If it holds keys, add it to
`.gitignore` so that it does not end up in git.

`small.json` and `.small.json` are different files. You write the first one yourself (settings). `small` creates
the second one: it holds the project and service IDs and the domain.

## Under the hood

1. `small.json` is read and validated, if it exists.
2. The folder's code is packed into a `.tar.gz`. The root `.gitignore` and `.railwayignore` are respected;
   `.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, as well as `small.json` and `.small.json` in the root,
   are always excluded.
3. GraphQL at `https://backboard.railway.com/graphql/v2`: `me.workspaces` (picking the workspace). If the name is
   set explicitly, `projects` looks for a project with that name. Then `projectCreate` and `serviceCreate`, if
   there is no matching project.
4. `serviceDomainCreate` creates a `*.up.railway.app` domain. If `<name>.up.railway.app` is free
   (`serviceDomainAvailable`), the domain is renamed to it (`serviceDomainUpdate`). If the name is taken, the
   domain Railway assigned stays. If the service was found by name and already has a domain, the existing one is
   used. The domain is created before the upload because the very first build needs its address (links in sign-in
   emails).
5. `variableCollectionUpsert` writes the variables from `small.json`, plus `PUBLIC_URL` and `OWNER_LANG` (see
   above). With an empty `env` this step is skipped.
6. The archive goes to `POST https://backboard.railway.com/project/{projectId}/environment/{environmentId}/up?serviceId=...`.
   This is the same endpoint the official `railway up` uses. It starts the build and returns a `deploymentId`.
7. `deployment(id) { status }` is polled every 3 s until `SUCCESS` (or `SLEEPING`) or a failure status:
   `FAILED`, `CRASHED`, `REMOVED`, `REMOVING`, `SKIPPED`, `NEEDS_APPROVAL`. On `FAILED` the `buildLogs` are
   printed, on any other failure the `deploymentLogs`.
8. `https://<domain>` is printed.

The project and service IDs and the domain are saved to `.small.json` in the app folder. The next `small deploy`
ships a new version to the same service and does not create a new project. The file is written right after the
project is created or linked, so a failed build does not spawn extra projects when you retry.

The build is done by Railpack, which detects the language by itself. For Python, `app.py` or `main.py` is enough
(plus `requirements.txt` for dependencies), and the start command will be `python app.py`. The app must listen on
`0.0.0.0:$PORT`.

## Tests

In the `small-cli` folder of a clone:

```bash
python -m unittest discover -s tests
```

The tests start a local mock of the Railway API (GraphQL + the upload endpoint) and run the full scenario: first
deploy, repeat deploy, failed build, crash, taken domain, several workspaces, wrong token, ignore rules.

Template tests on real frameworks are skipped if the framework is not installed; the rest pass.
- 6 `streamlit` tests: the access gate through `streamlit.testing.v1.AppTest` (with personal tokens) and the server
  through `python app.py`.
- 2 `fastapi` tests: `TestClient` and `uvicorn main:app` the way Railway runs it, plus a check that the token is
  hidden in the access log.

`small env` and `small access` are tested against the Railway mock:
- variable sources and hidden values;
- `set` / `unset` together with `small.json`, a single restart, `--no-deploy`;
- before the first deploy;
- a Railway failure that leaves `small.json` unchanged;
- granting, revoking and `link --as`, revoking the last token, a token that exists only in Railway.

Dashboard buttons: the "Open via invite link" redirect and its protection against foreign paths, "Logs" without a
token, with Railway and with Railway unavailable. Sleep mode and the cost estimate in the dashboard, Railway errors
while computing the cost.

`small sleep`, `db`, `domain` and `notify` are tested against mocks of Railway and the Telegram Bot API:
- turning sleep on and off, turning it on again without requests, `--no-deploy`;
- deploying the Postgres template with default values, the `DATABASE_URL` reference, a workflow error, removing
  the database, `destroy` together with the database;
- DNS records for a custom domain, address normalization, repeat and removal;
- finding the bot's chat, the test message, a wrong token, "nobody messaged the bot", a missing token.

`small init` is tested like this (the `streamlit` template too):
- the `python-http` and `node-http` templates and unique names and tokens (`fastapi` and `streamlit` have their
  own tests, see above);
- protection of existing files: declining, `--force`, the terminal prompt;
- appending to `.gitignore` without duplicates;
- the `init` → `deploy` chain;
- `python-http` and `node-http` run as real servers (403 → link → cookie → 200);
- the copies of `magic_link.py` and `magic-link.js` in the templates match the sources in the repository's
  `magic-link/` folder.

`small dashboard` is tested like this (live statuses against the Railway mock: success, partial failure, no
network, timeout, wrong token, garbage status, no token in the response):
- scanning: nested folders, service folders, folders inside an app, the depth limit, an app at the root;
- app descriptions and no variable values in the output;
- broken files and unsafe links;
- the page and the API, including a rescan;
- 403 for a foreign `Host`;
- the fallback port;
- the command itself: URL, browser, `--no-browser`.

`small.json` is tested separately:
- name and variables, the "variables before upload" order;
- excluding the file from the archive;
- linking to an existing project and an ambiguous name;
- `--new` and the precedence of `--name`;
- an already linked folder and an invalid file.

`SMALL_API_URL` overrides the API address.

## Limitations

- Only the root `.gitignore`/`.railwayignore` are read; nested ones are ignored.
- One service is created, in the `production` environment. Databases: Postgres only (`small db`).
