"""small commands. Each module adds its subcommands with a register(subparsers) function.

The order here is the order in `small --help`: from creating an app to deleting it.
"""

from . import (access, dashboard, db, deploy, destroy, domain, env, init, link, logs, mail, mcp, notify, sleep,
               sso, volume)

ALL = (init, deploy, link, access, sso, mail, notify, env, db, volume, domain, sleep, logs, dashboard, mcp, destroy)
