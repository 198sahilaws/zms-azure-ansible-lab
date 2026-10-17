"""Gunicorn entry point. One payload, four roles.

    gunicorn --bind 0.0.0.0:$ZMS_PORT zmsapp.wsgi:app

ZMS_SERVICE (from /etc/zms-app/<service>.env) decides which application object
this process becomes. The alternative -- four entry points and four deploy
paths -- means four things to keep in step; this way the Ansible role copies
one tree everywhere and only the environment file differs per host.
"""

import importlib
import sys

from . import config

APPS = {
    "catalog": "zmsapp.catalog",
    "inventory": "zmsapp.inventory",
    "orders": "zmsapp.orders",
    "storefront": "zmsapp.storefront",
}


def load():
    target = APPS.get(config.SERVICE)
    if target is None:
        raise SystemExit(
            "ZMS_SERVICE='{0}' is not one of: {1}. Check "
            "/etc/zms-app/*.env and the systemd unit's EnvironmentFile.".format(
                config.SERVICE, ", ".join(sorted(APPS))))
    return importlib.import_module(target).app


app = load()

if __name__ == "__main__":
    # Development convenience only; systemd always runs gunicorn.
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
    sys.exit(0)
