"""HTTP client for service-to-service and storefront-to-service calls.

This module is the whole reason the application is a distributed system rather
than one program in three files. Two rules it enforces:

  1. EVERY call has a timeout. Without one, a hung dependency turns into a hung
     web page and eventually a hung worker pool -- the classic cascading
     failure. config.HTTP_TIMEOUT is short on purpose.

  2. A FAILED CALL IS DATA, NOT AN EXCEPTION. Callers get {"ok": False,
     "error": ...} back and decide what to render. That is what lets the
     storefront show three panels where one says "unavailable" instead of
     returning a 500 for the whole page.

Stop one service (systemctl stop zms-catalog) and reload the storefront to see
both rules working.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import config

log = logging.getLogger("zmsapp.clients")


def _body(resp):
    """Parse a JSON body, or None if the response is not JSON."""
    try:
        return resp.json()
    except Exception:
        return None


def _detail(body):
    """Pull a short reason out of an error body for the log line."""
    if isinstance(body, dict) and body.get("error"):
        return ": " + str(body["error"])[:160]
    return ""


def get_json(url, params=None, timeout=None):
    """GET a JSON document. Returns an envelope, never raises.

    {"ok": True,  "data": <parsed json>, "status_code": int, "elapsed_ms": float, "url": str}
    {"ok": False, "error": "<reason>", "status_code": int|None, "elapsed_ms": float, "url": str}

    An error RESPONSE still carries its body in "data" when the body is JSON.
    That matters for /health: a service whose database is down answers 503 with
    the connection error inside, and "the service is up but its database is
    not" is a different fact from "the service did not answer" -- different
    badge on the dashboard, different action for whoever is on call. Only a
    missing status_code means nothing answered at all.
    """
    timeout = config.HTTP_TIMEOUT if timeout is None else timeout
    started = time.time()
    if not url:
        return {"ok": False, "error": "no endpoint configured", "url": url,
                "elapsed_ms": 0.0}
    try:
        resp = requests.get(url, params=params, timeout=timeout)
        elapsed = round((time.time() - started) * 1000, 1)
        body = _body(resp)
        if resp.status_code >= 400:
            return {"ok": False, "url": url, "elapsed_ms": elapsed,
                    "status_code": resp.status_code, "data": body,
                    "error": "HTTP {0}{1}".format(resp.status_code,
                                                  _detail(body))}
        return {"ok": True, "url": url, "elapsed_ms": elapsed,
                "status_code": resp.status_code, "data": body}
    except requests.exceptions.Timeout:
        return {"ok": False, "url": url,
                "elapsed_ms": round((time.time() - started) * 1000, 1),
                "error": "timeout after {0}s".format(timeout)}
    except Exception as exc:
        return {"ok": False, "url": url,
                "elapsed_ms": round((time.time() - started) * 1000, 1),
                "error": str(exc)[:200]}


def post_json(url, payload, timeout=None):
    """POST a JSON document. Same envelope contract as get_json."""
    timeout = config.HTTP_TIMEOUT if timeout is None else timeout
    started = time.time()
    if not url:
        return {"ok": False, "error": "no endpoint configured", "url": url,
                "elapsed_ms": 0.0}
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        elapsed = round((time.time() - started) * 1000, 1)
        body = _body(resp)
        if resp.status_code >= 400:
            return {"ok": False, "url": url, "elapsed_ms": elapsed,
                    "status_code": resp.status_code, "data": body,
                    "error": "HTTP {0}{1}".format(resp.status_code,
                                                  _detail(body))}
        return {"ok": True, "url": url, "elapsed_ms": elapsed,
                "status_code": resp.status_code, "data": body}
    except requests.exceptions.Timeout:
        return {"ok": False, "url": url,
                "elapsed_ms": round((time.time() - started) * 1000, 1),
                "error": "timeout after {0}s".format(timeout)}
    except Exception as exc:
        return {"ok": False, "url": url,
                "elapsed_ms": round((time.time() - started) * 1000, 1),
                "error": str(exc)[:200]}


def peer_url(service, path):
    """Build a URL for a peer service from the endpoint map in the env file."""
    base = (config.PEERS.get(service) or "").rstrip("/")
    if not base:
        return ""
    return base + path


def fan_out(calls):
    """Run several get_json/post_json calls CONCURRENTLY and collect them.

    `calls` maps a label to a zero-argument callable. Returns {label: envelope}.

    Sequential fan-out would make the dashboard as slow as the sum of its
    dependencies; concurrent fan-out makes it as slow as the slowest one. With
    three services and a 2.5s timeout that is the difference between a 7.5s
    worst case and a 2.5s worst case.
    """
    results = {}
    if not calls:
        return results
    with ThreadPoolExecutor(max_workers=max(2, len(calls))) as pool:
        futures = {label: pool.submit(fn) for label, fn in calls.items()}
        for label, future in futures.items():
            try:
                results[label] = future.result()
            except Exception as exc:  # a callable itself blew up
                results[label] = {"ok": False, "error": str(exc)[:200],
                                  "url": "", "elapsed_ms": 0.0}
    return results
