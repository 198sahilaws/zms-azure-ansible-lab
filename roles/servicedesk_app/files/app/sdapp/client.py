"""HTTP client the WEB tier uses to reach the MIDDLEWARE tier.

Standard library only, on purpose. The web host installs Flask and gunicorn
because it serves HTTP; making it also install requests would add a dependency
whose only job is three functions, and the traffic generator on the client host
already proves urllib is enough for this lab.

TWO RULES THIS FILE EXISTS TO ENFORCE

1. An HTTP error status is NOT an exception. When MySQL is down the middleware
   answers 503 with a considered body naming the database, and that body is the
   whole point of the outage demo. urllib raises HTTPError for 4xx/5xx, so it
   is caught and turned back into an ordinary response. Only a genuine failure
   to get any answer at all — connection refused, DNS, timeout — raises
   MiddlewareUnreachable, which is a different fault with a different message.

2. Datetimes come back as ISO strings and must become datetime objects again.
   index.html and ticket.html call .strftime() on created_at, updated_at and
   closed_at. Before the split those were pymysql datetimes; across JSON they
   are str, and str has no .strftime. revive() walks the payload and converts
   the keys in DATETIME_FIELDS. Add a datetime column to an API response and
   you must add its key here too.
"""
import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request

from . import config

# Keys converted from ISO-8601 back into datetime on the way in. Nested dicts
# and lists are walked, so ticket.sla.due_at is covered by naming due_at once.
DATETIME_FIELDS = ("created_at", "updated_at", "closed_at", "due_at")


class MiddlewareUnreachable(Exception):
    """No answer at all from the middleware tier.

    Distinct from a 503: a 503 means the middleware is up and telling us the
    database is not, which is a different page and a different fix.
    """


class Response(object):
    __slots__ = ("status", "body", "content_type")

    def __init__(self, status, body, content_type):
        self.status = status
        self.body = body                  # bytes, verbatim
        self.content_type = content_type

    @property
    def ok(self):
        return 200 <= self.status < 300

    def json(self):
        """Parsed body with datetime strings revived. {} if it is not JSON —
        the caller is always better off rendering an empty page than a
        traceback about the shape of an error body it did not expect."""
        if not self.body:
            return {}
        try:
            return revive(json.loads(self.body.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            return {}


def revive(value, _fields=DATETIME_FIELDS):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _fields and isinstance(item, str):
                out[key] = _parse_iso(item)
            else:
                out[key] = revive(item, _fields)
        return out
    if isinstance(value, list):
        return [revive(item, _fields) for item in value]
    return value


def _parse_iso(text):
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        # Not a datetime after all (or a format this Python cannot read).
        # Hand back the string rather than raising — a template printing an
        # odd timestamp is a cosmetic bug; a 500 on the queue page is not.
        return text


def _url(path, query=None):
    if not config.MIDDLEWARE_URL:
        raise MiddlewareUnreachable(
            "SD_MIDDLEWARE_URL is not set. The web tier has no middleware to "
            "talk to; check /etc/servicedesk/servicedesk.env.")
    url = config.MIDDLEWARE_URL + path
    if query:
        url += "?" + (query if isinstance(query, str)
                      else urllib.parse.urlencode(query, doseq=True))
    return url


def call(method, path, query=None, body=None, timeout=None):
    """One request. Returns a Response for any HTTP status the middleware
    produced; raises MiddlewareUnreachable only when there was no status."""
    data, headers = None, {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(_url(path, query), data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(
                req, timeout=timeout or config.HTTP_TIMEOUT) as resp:
            return Response(resp.status, resp.read(),
                            resp.headers.get("Content-Type", "application/json"))
    except urllib.error.HTTPError as exc:
        # See rule 1 above: this is an answer, not a failure.
        return Response(exc.code, exc.read(),
                        exc.headers.get("Content-Type", "application/json"))
    except Exception as exc:  # URLError, socket.timeout, ssl, DNS...
        raise MiddlewareUnreachable(
            "%s: %s" % (type(exc).__name__, exc)) from exc


def get(path, query=None, timeout=None):
    return call("GET", path, query=query, timeout=timeout)


def post(path, body, timeout=None):
    return call("POST", path, body=body, timeout=timeout)


def health(timeout=None):
    """(reachable, status_code, payload) — never raises.

    Used by the web tier's own /health, which must answer even when the
    middleware is gone, for the same reason the middleware's /health answers
    when MySQL is gone.
    """
    try:
        resp = get("/health", timeout=timeout)
    except MiddlewareUnreachable as exc:
        return False, None, {"error": str(exc)}
    return True, resp.status, resp.json()
