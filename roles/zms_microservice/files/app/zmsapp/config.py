"""Configuration, read once from the process environment.

Ansible writes /etc/zms-app/<service>.env and the systemd unit loads it with
EnvironmentFile=, so nothing in this package ever has to know an IP address or
a hostname at author time. That is what lets one payload serve four roles.
"""

import os
import socket


def _int(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# --- Identity --------------------------------------------------------------

SERVICE = os.environ.get("ZMS_SERVICE", "unknown")
VERSION = os.environ.get("ZMS_VERSION", "0.0.0")
PORT = _int("ZMS_PORT", 8000)

# Reported on /health and shown in the storefront topology panel, so you can see
# at a glance which box answered. Handy when you have four services that all
# look identical from the browser.
HOSTNAME = socket.gethostname()
NODE_IP = os.environ.get("ZMS_NODE_IP", "")
DISTRO = os.environ.get("ZMS_DISTRO", "")

# --- Database (absent for the storefront) ----------------------------------

DB_HOST = os.environ.get("ZMS_DB_HOST", "")
DB_PORT = _int("ZMS_DB_PORT", 3306)
DB_NAME = os.environ.get("ZMS_DB_NAME", "")
DB_USER = os.environ.get("ZMS_DB_USER", "")
DB_PASSWORD = os.environ.get("ZMS_DB_PASSWORD", "")
DB_DISTRO = os.environ.get("ZMS_DB_DISTRO", "")

HAS_DB = bool(DB_HOST and DB_NAME)

# --- Peer services ---------------------------------------------------------
# Every host gets the full endpoint map, whether it uses it or not. The
# storefront calls all three; orders-svc calls catalog and inventory; catalog
# and inventory call nobody.

PEERS = {
    "catalog": os.environ.get("ZMS_CATALOG_URL", ""),
    "inventory": os.environ.get("ZMS_INVENTORY_URL", ""),
    "orders": os.environ.get("ZMS_ORDERS_URL", ""),
}

# Deliberately short. A slow dependency must degrade the page, not hang the
# browser -- the storefront renders an "unavailable" badge instead of blocking.
# Raise it and the failure demo stops being instructive.
HTTP_TIMEOUT = _float("ZMS_HTTP_TIMEOUT", 2.5)

# --- Fake data -------------------------------------------------------------

SEED_PRODUCTS = _int("ZMS_SEED_PRODUCTS", 240)
SEED_ORDERS = _int("ZMS_SEED_ORDERS", 320)
# Fixed seed on purpose. catalog and inventory generate their rows INDEPENDENTLY
# on two different hosts against two different databases; the shared seed and
# the shared 1..SEED_PRODUCTS id range are what make product 42 mean the same
# thing on both sides without either service reading the other's data.
RANDOM_SEED = _int("ZMS_SEED_RANDOM", 20260829)


def identity():
    """Descriptive block returned by /health and /meta on every service."""
    return {
        "service": SERVICE,
        "version": VERSION,
        "port": PORT,
        "hostname": HOSTNAME,
        "node_ip": NODE_IP,
        "distro": DISTRO,
        "database": {
            "host": DB_HOST,
            "port": DB_PORT,
            "name": DB_NAME,
            "distro": DB_DISTRO,
        } if HAS_DB else None,
    }
