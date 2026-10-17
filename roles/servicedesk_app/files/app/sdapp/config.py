"""Configuration, read entirely from the environment.

Ansible writes /etc/servicedesk/servicedesk.env and systemd passes it in via
EnvironmentFile, so nothing here is hardcoded and nothing is read from a file
this process would have to find. Same contract as the ZMS microservices app.

ONE PAYLOAD, TWO TIERS. The same /opt/servicedesk tree is installed on the
middleware host and on the web host; SD_TIER decides which Flask app
sdapp.wsgi exports. This mirrors the ZMS microservices lab, where one
zmsapp payload becomes four services via ZMS_SERVICE, and it is why there is
no second Ansible role: the tiers differ by environment, not by content.

  SD_TIER=middleware  -> sdapp.api:app   owns MySQL, holds the credentials
  SD_TIER=web         -> sdapp.web:app   owns HTML, holds no credentials
"""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


TIER = (os.environ.get("SD_TIER") or "middleware").strip().lower()

# Where the web tier sends everything. Unset on the middleware host, where it
# is meaningless. No default host: an unset value must fail loudly rather than
# quietly proxying to localhost and looking like a database outage.
MIDDLEWARE_URL = (os.environ.get("SD_MIDDLEWARE_URL") or "").rstrip("/")

# Kept a touch above DB_CONNECT_TIMEOUT so that when MySQL is down the web tier
# receives the middleware's considered 503 instead of timing out first and
# reporting the middleware as unreachable — the demo must blame the right tier.
HTTP_TIMEOUT = _float("SD_HTTP_TIMEOUT", 8.0)

DB_HOST = os.environ.get("SD_DB_HOST", "127.0.0.1")
DB_PORT = _int("SD_DB_PORT", 3306)
DB_NAME = os.environ.get("SD_DB_NAME", "servicedesk")
DB_USER = os.environ.get("SD_DB_USER", "sdapp")
DB_PASSWORD = os.environ.get("SD_DB_PASSWORD", "")

# Connect timeout kept short: a hung database should surface on /health in
# seconds, not hold a gunicorn worker for the default 10s+ per request.
DB_CONNECT_TIMEOUT = _int("SD_DB_CONNECT_TIMEOUT", 5)

APP_NAME = os.environ.get("SD_APP_NAME", "ZMS Lab Service Desk")
APP_PORT = _int("SD_APP_PORT", 8090)

# Seeder controls. Deterministic by default so two runs against two databases
# produce identical data and a diff is meaningful.
SEED_USERS = _int("SD_SEED_USERS", 40)
SEED_TICKETS = _int("SD_SEED_TICKETS", 500)
SEED_DAYS = _int("SD_SEED_DAYS", 90)
SEED_RANDOM = _int("SD_SEED_RANDOM", 20260901)
