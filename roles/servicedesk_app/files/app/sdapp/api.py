"""The service desk MIDDLEWARE tier: business logic and the only tier that
talks to MySQL.

This module holds everything that used to live in web.py below the render
line — the queries, the queue and stats shaping, ticket creation, commenting —
plus the rules that make this a tier rather than a database proxy: SLA
evaluation and assignment. The web tier cannot reach MySQL and does not know
the schema; it asks for JSON and renders it.

WHY THE SPLIT IS WHERE IT IS
  Every route here returns JSON and every route here may touch the database.
  Nothing here imports flask.render_template or knows an HTML template exists.
  If you find yourself needing a template on this side, the logic belongs in
  web.py instead; if you find yourself needing pymysql in web.py, it belongs
  here.

THE JSON CONTRACT
  Datetimes are emitted as ISO-8601 strings, NOT flask's default RFC 1123.
  sdapp.client revives exactly the keys in DATETIME_FIELDS back into datetime
  objects so index.html and ticket.html keep working unchanged — they call
  .strftime() on created_at / updated_at / closed_at. If you add a datetime
  column to a response, add its key to DATETIME_FIELDS in client.py or the
  frontend will raise on a str having no .strftime.
"""
import datetime as dt
import decimal

import pymysql
from flask import Flask, jsonify, request

from . import config, db

app = Flask(__name__)

STATUSES = ["new", "open", "pending", "resolved", "closed"]
PRIORITIES = ["P1", "P2", "P3", "P4"]
OPEN_STATUSES = ("new", "open", "pending")

# Hours from creation to breach, by priority. Pure policy, deliberately not in
# the schema: it is the kind of rule that changes without a migration, which is
# most of the argument for having a middleware tier at all.
SLA_HOURS = {"P1": 4, "P2": 8, "P3": 24, "P4": 72}

# A ticket is "at risk" once less than this fraction of its budget remains.
SLA_AT_RISK_REMAINING = 0.25


# --- serialisation -----------------------------------------------------------

def _jsonable(value):
    """Convert driver-native types into things json can hold.

    pymysql hands back datetime for DATETIME and Decimal for aggregates; both
    make flask's encoder either fail or emit a format that is awkward to parse
    on the other side. Doing it here means the wire format is decided in one
    place instead of per route.
    """
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _json(payload, status=200):
    return jsonify(_jsonable(payload)), status


# --- business rules ----------------------------------------------------------

def sla(row, now=None):
    """Evaluate a ticket against its SLA. Returns None if it cannot be judged.

    Open tickets are measured against the clock; closed ones are measured
    against when they actually stopped, so a resolved P1 does not drift into
    'breached' days later just because nobody looked at it.
    """
    created = row.get("created_at")
    if not isinstance(created, dt.datetime):
        return None
    now = now or dt.datetime.now()
    hours = SLA_HOURS.get(row.get("priority"), 24)
    due = created + dt.timedelta(hours=hours)
    if row.get("status") in OPEN_STATUSES:
        measured_at = now
    else:
        measured_at = row.get("closed_at") or row.get("updated_at") or now
    remaining = (due - measured_at).total_seconds() / 3600.0
    if remaining < 0:
        state = "breached"
    elif remaining < hours * SLA_AT_RISK_REMAINING:
        state = "at_risk"
    else:
        state = "on_track"
    return {"hours": hours, "due_at": due, "state": state,
            "remaining_hours": round(remaining, 2)}


def _with_sla(rows):
    now = dt.datetime.now()
    for row in rows:
        row["sla"] = sla(row, now=now)
    return rows


def least_loaded_agent():
    """The agent with the fewest open tickets, ties broken by id so the choice
    is deterministic and a reseed produces the same assignment twice.

    The correlated count rides ix_tickets_assignee (assignee_id, status), so
    this stays cheap as the ticket table grows.
    """
    return db.query_one(
        "SELECT u.id, u.username, u.full_name, "
        "       (SELECT COUNT(*) FROM tickets t "
        "         WHERE t.assignee_id = u.id AND t.status IN %s) AS open_tickets "
        "FROM users u WHERE u.role = 'agent' "
        "ORDER BY open_tickets ASC, u.id ASC LIMIT 1", (OPEN_STATUSES,))


# --- queries -----------------------------------------------------------------

def _next_ref():
    """Next SD-nnnnnn. MAX+1 rather than an auto-increment mirror so a
    force-reseed cannot produce a duplicate ref against a stale sequence."""
    row = db.query_one("SELECT MAX(CAST(SUBSTRING(ref, 4) AS UNSIGNED)) AS n FROM tickets")
    return "SD-%06d" % ((row["n"] or 0) + 1)


def queue(status=None, priority=None, assignee=None, limit=50):
    where, args = [], []
    if status == "open":
        where.append("t.status IN %s")
        args.append(OPEN_STATUSES)
    elif status in STATUSES:
        where.append("t.status = %s")
        args.append(status)
    if priority in PRIORITIES:
        where.append("t.priority = %s")
        args.append(priority)
    if assignee:
        where.append("a.username = %s")
        args.append(assignee)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    args.append(limit)
    rows = db.query(
        "SELECT t.ref, t.subject, t.status, t.priority, t.category, "
        "       t.created_at, t.updated_at, t.closed_at, "
        "       r.full_name AS requester, a.full_name AS assignee, "
        "       (SELECT COUNT(*) FROM comments c WHERE c.ticket_id = t.id) AS comment_count "
        "FROM tickets t "
        "JOIN users r ON r.id = t.requester_id "
        "LEFT JOIN users a ON a.id = t.assignee_id "
        + clause +
        " ORDER BY FIELD(t.priority, 'P1','P2','P3','P4'), t.created_at DESC "
        "LIMIT %s", args)
    return _with_sla(rows)


def stats():
    by_status = db.query("SELECT status, COUNT(*) AS n FROM tickets GROUP BY status")
    by_priority = db.query(
        "SELECT priority, COUNT(*) AS n FROM tickets "
        "WHERE status IN %s GROUP BY priority", (OPEN_STATUSES,))
    totals = db.query_one(
        "SELECT (SELECT COUNT(*) FROM tickets) AS tickets, "
        "       (SELECT COUNT(*) FROM comments) AS comments, "
        "       (SELECT COUNT(*) FROM users) AS users")
    # SLA is evaluated in Python, not SQL, so the policy lives in exactly one
    # place. Bounded to open tickets: closed history is not what a queue
    # dashboard is asking about, and it keeps this off a full table scan.
    open_rows = db.query(
        "SELECT status, priority, created_at, updated_at, closed_at "
        "FROM tickets WHERE status IN %s", (OPEN_STATUSES,))
    sla_counts = {"on_track": 0, "at_risk": 0, "breached": 0}
    now = dt.datetime.now()
    for row in open_rows:
        verdict = sla(row, now=now)
        if verdict:
            sla_counts[verdict["state"]] += 1
    return {
        "totals": totals,
        "by_status": {r["status"]: r["n"] for r in by_status},
        "open_by_priority": {r["priority"]: r["n"] for r in by_priority},
        "open_by_sla": sla_counts,
    }


def ticket(ref):
    """Full detail plus comments, or None. One call so the web tier renders a
    ticket page from a single round trip instead of two."""
    row = db.query_one(
        "SELECT t.*, r.full_name AS requester, r.email AS requester_email, "
        "       a.full_name AS assignee "
        "FROM tickets t JOIN users r ON r.id = t.requester_id "
        "LEFT JOIN users a ON a.id = t.assignee_id WHERE t.ref = %s", (ref,))
    if not row:
        return None
    comments = db.query(
        "SELECT c.body, c.is_internal, c.created_at, u.full_name AS author, u.role "
        "FROM comments c JOIN users u ON u.id = c.author_id "
        "WHERE c.ticket_id = %s ORDER BY c.created_at", (row["id"],))
    row["sla"] = sla(row)
    return {"ticket": row, "comments": comments}


# --- error handling ----------------------------------------------------------

@app.errorhandler(pymysql.MySQLError)
def database_down(exc):
    """A database outage is a 503 with a 'database' object, never a 500.

    Every route on this tier is JSON, so unlike the old combined app there is
    no content negotiation to do here. The web tier turns this into the HTML
    error page; see web.py.
    """
    detail = "%s: %s" % (type(exc).__name__, exc)
    return _json({"service": "servicedesk", "tier": "middleware",
                  "status": "degraded",
                  "database": {"reachable": False, "host": config.DB_HOST,
                               "database": config.DB_NAME, "error": detail}}, 503)


# --- routes ------------------------------------------------------------------

@app.route("/api/tickets", methods=["GET"])
def api_list():
    return _json(queue(status=request.args.get("status", "open"),
                       priority=request.args.get("priority") or None,
                       assignee=request.args.get("assignee") or None,
                       limit=min(int(request.args.get("limit", 25)), 200)))


@app.route("/api/tickets/<ref>", methods=["GET"])
def api_ticket(ref):
    found = ticket(ref)
    if not found:
        return _json({"error": "no such ticket", "ref": ref}, 404)
    return _json(found)


@app.route("/api/tickets", methods=["POST"])
def api_create():
    payload = request.get_json(silent=True) or {}
    subject = (payload.get("subject") or "").strip()
    if not subject:
        return _json({"error": "subject is required"}, 400)

    requester = db.query_one(
        "SELECT id FROM users WHERE username = %s", (payload.get("requester"),))
    if not requester:
        requester = db.query_one(
            "SELECT id FROM users WHERE role = 'requester' ORDER BY RAND() LIMIT 1")
    if not requester:
        return _json({"error": "no users exist; run the seeder first"}, 409)

    # Out-of-enum priorities are still coerced to P3 rather than rejected —
    # that is the long-standing contract the traffic generator relies on — but
    # the response now says so, because silently changing a caller's input and
    # not telling them is the bug people actually trip over.
    wanted = payload.get("priority")
    priority = wanted if wanted in PRIORITIES else "P3"
    agent = least_loaded_agent()

    ref = _next_ref()
    now = dt.datetime.now()
    db.execute(
        "INSERT INTO tickets (ref, subject, body, category, status, priority, "
        "requester_id, assignee_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, 'new', %s, %s, %s, %s, %s)",
        (ref, subject[:200], (payload.get("body") or "")[:4000],
         (payload.get("category") or "general")[:32], priority,
         requester["id"], agent["id"] if agent else None, now, now))
    return _json({"ref": ref, "status": "new", "priority": priority,
                  "priority_coerced": bool(wanted) and wanted != priority,
                  "assignee": agent["full_name"] if agent else None,
                  "sla": sla({"created_at": now, "priority": priority,
                              "status": "new"}, now=now)}, 201)


@app.route("/api/tickets/<ref>/comments", methods=["POST"])
def api_comment(ref):
    payload = request.get_json(silent=True) or {}
    body = (payload.get("body") or "").strip()
    if not body:
        return _json({"error": "body is required"}, 400)

    found = db.query_one("SELECT id, status FROM tickets WHERE ref = %s", (ref,))
    if not found:
        return _json({"error": "no such ticket"}, 404)
    author = db.query_one(
        "SELECT id FROM users WHERE username = %s", (payload.get("author"),)) \
        or db.query_one("SELECT id FROM users ORDER BY RAND() LIMIT 1")

    db.execute("INSERT INTO comments (ticket_id, author_id, body, is_internal) "
               "VALUES (%s, %s, %s, %s)",
               (found["id"], author["id"], body[:4000],
                1 if payload.get("internal") else 0))
    # A comment on an untriaged ticket moves it into the queue, which is what
    # gives the generator a way to change state without a separate endpoint.
    triaged = found["status"] == "new"
    if triaged:
        db.execute("UPDATE tickets SET status = 'open' WHERE id = %s", (found["id"],))
    return _json({"ref": ref, "commented": True, "triaged": triaged}, 201)


@app.route("/stats")
def api_stats():
    return _json(stats())


@app.route("/health")
def health():
    """200 when the database answers, 503 when it does not.

    Unchanged contract: the body always carries a 'database' object so a
    monitor can tell "app down" from "database down". The web tier nests this
    object verbatim inside its own /health so existing checks keep working.
    """
    ok, detail = db.health()
    return _json({"service": "servicedesk", "tier": "middleware",
                  "status": "ok" if ok else "degraded",
                  "database": detail}, 200 if ok else 503)
