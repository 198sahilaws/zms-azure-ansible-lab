"""Shared Flask scaffolding: identity, health, error handling.

Every service gets the same /health and /meta contract, which is what makes the
storefront's topology panel possible without special-casing each backend and
what lets Ansible verify a deploy with one uri task per host.
"""

import decimal
import logging
import time

from flask import Flask, jsonify, request

from . import config, db, schema

STARTED_AT = time.time()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [{0}] %(name)s %(message)s".format(
        config.SERVICE),
)
log = logging.getLogger("zmsapp")


def make_app(service_name, ensure_schema=True):
    """Build a Flask app with the common endpoints already registered."""
    # Import name is the PACKAGE, not the service: that is what anchors
    # Flask's root_path at .../zmsapp so the storefront finds zmsapp/templates
    # regardless of the working directory systemd starts the unit in. Passing
    # the service name here instead makes Flask fall back to the CWD and the
    # templates silently disappear under gunicorn.
    app = Flask("zmsapp", template_folder="templates", static_folder=None)
    app.config["JSON_SORT_KEYS"] = False
    app.config["ZMS_SERVICE"] = service_name

    if ensure_schema and config.HAS_DB:
        # Self-heal: a service that boots before its schema exists creates it
        # rather than 500ing until someone re-runs the seeder. Failure here is
        # logged, not fatal -- the database may simply not be reachable yet,
        # and /health should be able to say so.
        try:
            schema.ensure(service_name, db)
        except Exception as exc:
            log.warning("schema bootstrap deferred: %s", exc)

    @app.get("/health")
    def health():
        payload = {
            "service": service_name,
            "status": "ok",
            "uptime_seconds": round(time.time() - STARTED_AT, 1),
            "identity": config.identity(),
        }
        if config.HAS_DB:
            payload["database"] = db.health()
            if payload["database"]["status"] != "ok":
                payload["status"] = "degraded"
                return jsonify(payload), 503
        return jsonify(payload)

    @app.get("/meta")
    def meta():
        """Everything the storefront needs to draw the topology panel."""
        return jsonify({
            "identity": config.identity(),
            "peers": {k: v for k, v in config.PEERS.items() if v},
            "http_timeout_seconds": config.HTTP_TIMEOUT,
        })

    @app.errorhandler(404)
    def not_found(_exc):
        return jsonify({"error": "not found", "path": request.path}), 404

    @app.errorhandler(db.DatabaseUnavailable)
    def db_down(exc):
        # 503, not 500: the service is fine, its dependency is not. The
        # storefront renders these differently and so should a load balancer.
        log.error("database unavailable: %s", exc)
        return jsonify({"error": "database unavailable",
                        "detail": str(exc)[:300],
                        "service": service_name}), 503

    @app.errorhandler(Exception)
    def unhandled(exc):
        log.exception("unhandled error")
        return jsonify({"error": "internal error",
                        "detail": str(exc)[:300],
                        "service": service_name}), 500

    return app


def numeric(value):
    """Coerce a MariaDB aggregate into a plain JSON number.

    SUM() and AVG() come back from PyMySQL as decimal.Decimal, which Flask
    serialises as a STRING and which explodes on `Decimal / float`. Both are
    easy to miss until a KPI tile shows "51694" in quotes or /stats returns a
    500 -- so every aggregate goes through here on its way out.
    """
    if isinstance(value, decimal.Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    return value


def numeric_row(row):
    """Apply numeric() to every value in a result row."""
    return {key: numeric(val) for key, val in (row or {}).items()}


def parse_ids(raw, limit=500):
    """Parse a '1,2,3' id list from a query string into a list of ints."""
    if not raw:
        return []
    out = []
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def paging(default_limit=50, max_limit=500):
    """Read limit/offset from the query string, clamped."""
    try:
        limit = int(request.args.get("limit", default_limit))
    except ValueError:
        limit = default_limit
    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0
    return max(1, min(limit, max_limit)), max(0, offset)
