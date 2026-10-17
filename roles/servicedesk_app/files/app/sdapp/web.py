"""The service desk WEB tier: HTML rendering and nothing else.

This tier has no database driver, no credentials and no SQL. It renders the
queue and ticket pages from JSON fetched over HTTP from the middleware tier,
and it forwards /api/* and /stats through unchanged so that every caller which
used to talk to the combined app on :8090 still works.

WHY THE PROXY FORWARDS BYTES RATHER THAN RE-SERIALISING
  sd-traffic.py on the client host and playbooks/servicedesk-verify.yml both
  assert on the shape of /api/tickets and /stats. Re-encoding the payload here
  would be an opportunity to change it by accident. Passing the middleware's
  body through verbatim, with its status code, makes shape drift impossible:
  the contract is owned by exactly one tier.

TWO FAILURE MODES, DELIBERATELY DISTINGUISHED
  503 from the middleware  -> the database is down.     "Service degraded"
  no answer at all         -> the middleware is down.   "Middleware unreachable"
  Both render as a 503 so the "watch it degrade" demo behaves the same from the
  outside, but the detail line names the tier that actually failed. Before the
  split there was only one thing that could break; now there are two, and a lab
  whose error page cannot tell you which is a worse lab.
"""
from flask import Flask, Response, jsonify, render_template, request

from . import client, config

app = Flask(__name__)

# Mirrored from sdapp.api rather than imported: importing api would pull in
# pymysql and db on a host that deliberately has neither configured. These are
# presentation-order lists for the filter bar, not business rules.
STATUSES = ["new", "open", "pending", "resolved", "closed"]
PRIORITIES = ["P1", "P2", "P3", "P4"]

PROXY_METHODS = ["GET", "POST"]


# --- helpers -----------------------------------------------------------------

def _degraded_payload(detail, tier):
    """The JSON body returned when this tier cannot serve a request.

    Keeps the 'database' object that /health and the API have always carried,
    because monitors key off database.reachable. When the middleware is the
    thing that is missing we still say the database is unreachable — from here
    it is, and the 'error' string says why.
    """
    return {"service": "servicedesk", "tier": "web", "status": "degraded",
            "failed_tier": tier,
            "middleware": {"url": config.MIDDLEWARE_URL or None,
                           "reachable": tier != "middleware"},
            "database": {"reachable": False, "database": config.DB_NAME,
                         "error": detail}}


def _degraded_page(detail, tier):
    return render_template("error.html", app_name=config.APP_NAME,
                           detail=detail, failed_tier=tier), 503


def _wants_json():
    return request.path.startswith("/api/") or request.path == "/stats"


@app.errorhandler(client.MiddlewareUnreachable)
def middleware_down(exc):
    detail = "middleware unreachable at %s — %s" % (
        config.MIDDLEWARE_URL or "<unset SD_MIDDLEWARE_URL>", exc)
    if _wants_json():
        return jsonify(_degraded_payload(detail, "middleware")), 503
    return _degraded_page(detail, "middleware")


# --- HTML --------------------------------------------------------------------

@app.route("/")
def index():
    status = request.args.get("status", "open")
    priority = request.args.get("priority") or None

    queue = client.get("/api/tickets", {"status": status, "limit": 50,
                                        **({"priority": priority} if priority else {})})
    stats = client.get("/stats")
    if not queue.ok or not stats.ok:
        bad = queue if not queue.ok else stats
        return _degraded_page(_upstream_detail(bad), "database")

    return render_template("index.html", app_name=config.APP_NAME,
                           tickets=queue.json(), stats=stats.json(),
                           status=status, priority=priority,
                           statuses=STATUSES, priorities=PRIORITIES)


@app.route("/ticket/<ref>")
def ticket_detail(ref):
    resp = client.get("/api/tickets/" + ref)
    if resp.status == 404:
        return render_template("error.html", app_name=config.APP_NAME,
                               detail="No ticket with reference %s." % ref,
                               failed_tier=None), 404
    if not resp.ok:
        return _degraded_page(_upstream_detail(resp), "database")

    payload = resp.json()
    return render_template("ticket.html", app_name=config.APP_NAME,
                           ticket=payload.get("ticket") or {},
                           comments=payload.get("comments") or [])


def _upstream_detail(resp):
    """Pull the middleware's own explanation out of its error body.

    The middleware already composed a precise message ("OperationalError:
    (2003, Can't connect to MySQL server on ...")). Surfacing that beats
    inventing a vaguer one here, and it keeps the error page's text identical
    to what it said before the tiers were split.
    """
    body = resp.json()
    database = body.get("database") or {}
    return database.get("error") or body.get("error") or \
        "middleware returned HTTP %s" % resp.status


# --- pass-through API --------------------------------------------------------

@app.route("/api/<path:subpath>", methods=PROXY_METHODS)
def api_proxy(subpath):
    """Forward /api/* to the middleware verbatim, both ways.

    Query string and JSON body go up unmodified; status code, content type and
    body come back unmodified. Nothing in here inspects the payload, which is
    precisely why the API contract cannot drift between the tiers.
    """
    upstream = client.call(
        request.method, "/api/" + subpath,
        query=request.query_string.decode("utf-8") or None,
        body=request.get_json(silent=True) if request.method == "POST" else None)
    return Response(upstream.body, status=upstream.status,
                    content_type=upstream.content_type)


@app.route("/stats")
def stats_proxy():
    upstream = client.get("/stats")
    return Response(upstream.body, status=upstream.status,
                    content_type=upstream.content_type)


# --- health ------------------------------------------------------------------

@app.route("/health")
def health():
    """200 only when this tier, the middleware AND the database are all good.

    The middleware's 'database' object is nested verbatim so that checks
    written against the single-tier app — servicedesk-verify.yml asserts
    json.database.reachable and json.database.tickets — keep passing without
    modification. The added 'middleware' object is what tells you which leg
    broke when it does not.
    """
    reachable, status, payload = client.health()

    if not reachable:
        body = _degraded_payload(
            "middleware unreachable at %s — %s" % (
                config.MIDDLEWARE_URL or "<unset SD_MIDDLEWARE_URL>",
                payload.get("error", "no answer")), "middleware")
        return jsonify(body), 503

    database = payload.get("database") or {
        "reachable": False, "database": config.DB_NAME,
        "error": "middleware answered HTTP %s without a database object" % status}
    ok = status == 200 and bool(database.get("reachable"))
    return jsonify({
        "service": "servicedesk", "tier": "web",
        "status": "ok" if ok else "degraded",
        "middleware": {"url": config.MIDDLEWARE_URL, "reachable": True,
                       "status": status},
        "database": database,
    }), (200 if ok else 503)
