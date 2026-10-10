"""small commands. Each module adds its subcommands with a register(subparsers) function.

The order here is the order in `small --help`: from creating an app to deleting it.
"""

from . import access, dashboard, db, deploy, destroy, domain, env, init, link, logs, mail, mcp, notify, sleep

ALL = (init, deploy, link, access, mail, notify, env, db, domain, sleep, logs, dashboard, mcp, destroy)
