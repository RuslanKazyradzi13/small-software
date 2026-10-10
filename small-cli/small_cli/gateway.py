"""The small gateway: sign-in in front of any app, without changes to its code (small gateway on).

The app's Railway project then holds two services: the app itself, without a public address, and <app>-gateway,
which takes the app's address and the sign-in variables. The gateway (templates/gateway: Caddy and magic_link) checks
every request and passes the ones it lets in to the app over Railway's private network, with who is signed in
(X-Small-User) signed by SMALL_GATEWAY_KEY. The app gets the same key as a Railway reference, so magic_link in it, if
it has one, trusts the gateway and nothing else.
"""

import time

from . import __version__
from .archive import collect_files, make_tarball
from .scaffold import TEMPLATES_DIR

GATEWAY_DIR = TEMPLATES_DIR / "gateway"
DEFAULT_PORT = "8080"
GATEWAY_PORT = 8080  # the gateway's Caddy listens here; its public address points here
# Sign-in itself: these live on the gateway only.
GATEWAY_ONLY = ("ACCESS_TOKEN", "ACCESS_EMAILS", "SESSION_SECRET", "ACCESS_REQUEST_URL", "ACCESS_LANG",
                "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET",
                "MICROSOFT_TENANT_ID", "SESSIONS_REVOKED", "LOGIN_LINKS_TO_LOG", "ACCESS_DB")
# Sign-in uses them, and an app may use them too (its own emails, its own address): both services get them.
SHARED = ("RESEND_API_KEY", "SMTP_URL", "MAIL_FROM", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "OWNER_LANG",
          "PUBLIC_URL")


def is_on(state):
    return bool((state.get("gateway") or {}).get("serviceId"))


def has_own_sign_in(root):
    """Whether the app's code protects itself (magic_link next to it, as in small's templates)."""
    return any((root / name).is_file() for name in ("magic_link.py", "magic-link.js"))


def public_service(state):
    """The service that has the app's public address: the gateway when it is on, otherwise the app."""
    return state["gateway"]["serviceId"] if is_on(state) else state["serviceId"]


def services_for(name, state):
    """Which services a variable goes to: [app] without the gateway; with it, by GATEWAY_ONLY and SHARED."""
    if not is_on(state):
        return [state["serviceId"]]
    if name in GATEWAY_ONLY:
        return [state["gateway"]["serviceId"]]
    if name in SHARED:
        return [state["serviceId"], state["gateway"]["serviceId"]]
    return [state["serviceId"]]


def split(variables, state):
    """{service ID: {name: value}} for the variables to write."""
    found = {}
    for name, value in variables.items():
        for service_id in services_for(name, state):
            found.setdefault(service_id, {})[name] = value
    return found


def upstream(app_service_name, port):
    """Where the gateway sends requests: the app's private address, as a Railway reference."""
    return "http://${{%s.RAILWAY_PRIVATE_DOMAIN}}:%s" % (app_service_name, port)


def key_reference(gateway_service_name):
    """The app's SMALL_GATEWAY_KEY: a reference to the gateway's, so the secret is written down only once."""
    return "${{%s.SMALL_GATEWAY_KEY}}" % gateway_service_name


def tarball():
    """The gateway's code as Railway builds it: Dockerfile, Caddyfile, gateway.py, magic_link.py."""
    return make_tarball(str(GATEWAY_DIR), collect_files(str(GATEWAY_DIR)))


def wait_for_private_address(api, state, seconds=120, interval=3):
    """The app's address on Railway's private network, once Railway has given it one (a new service gets it with its
    first deploy), or None. The gateway must not be deployed before: Railway fills in UPSTREAM's reference when the
    gateway deploys, and an empty one would point the gateway at itself."""
    deadline = time.monotonic() + seconds
    while True:
        found = api.service_variables(state["projectId"], state["environmentId"], state["serviceId"]).get(
            "RAILWAY_PRIVATE_DOMAIN")
        if found or time.monotonic() > deadline:
            return found or None
        time.sleep(interval)


def upload(api, state, message=None):
    """Upload and build the gateway; remembers the small version it came from. Returns the deployment ID."""
    up = api.upload(state["projectId"], state["environmentId"], state["gateway"]["serviceId"], tarball(),
                    message or "small gateway %s" % __version__)
    state["gateway"]["version"] = __version__
    return up["deploymentId"]


def outdated(state):
    """Whether the gateway was built by another version of small (small deploy rebuilds it then)."""
    return is_on(state) and state["gateway"].get("version") != __version__
