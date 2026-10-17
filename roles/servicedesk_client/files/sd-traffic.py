#!/usr/bin/env python3
"""Service desk traffic generator — runs on the CLIENT host, not the web host.

WHY STDLIB ONLY. This is deliberately urllib.request and nothing else, so the
client host needs no venv, no pip and no packages beyond the Python that Ubuntu
already ships. The whole point is that the client host stays a plain client.

WHY A REAL HTTP CLIENT AND NOT A LOAD GENERATOR. A microsegmentation agent
attributes a flow to the process that opened it. Traffic from hping3 or
tcpreplay is attributed to the generator binary, so the packets look right and
the flow records are useless. This opens ordinary TCP connections from python3
to the web host's listener, which is what a real user's browser does.

WHY ONE SHOT PER INVOCATION. systemd runs this on a timer with
RandomizedDelaySec, so ten hosts never fire on the same second. A long-running
daemon holding one socket would produce a single flow record and then nothing.

    sd-traffic.py --once            one burst of activity (what the timer runs)
    sd-traffic.py --loop --sleep 20 continuous, for interactive demos
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("SD_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
TIMEOUT = float(os.environ.get("SD_HTTP_TIMEOUT", "10"))

SUBJECTS = [
    "Cannot access shared drive from the new laptop",
    "Outlook stuck on 'Trying to connect'",
    "Request: additional monitor for desk move",
    "VPN drops when switching to Wi-Fi",
    "Password reset for the finance portal",
    "Printer on floor 3 offline again",
    "Teams audio not working after update",
    "New starter account setup for next Monday",
    "Laptop fan running constantly",
    "Cannot join the weekly project meeting",
    "Software licence expired warning",
    "Badge not opening the server room door",
]
BODIES = [
    "Started this morning, worked fine yesterday.",
    "Happens on both the office network and at home.",
    "Tried a reboot and it did not help.",
    "Blocking my work, please advise on a workaround.",
    "Low priority, whenever someone has time.",
]
REPLIES = [
    "Any update on this please?",
    "Still seeing the same behaviour.",
    "Tried the workaround, no change.",
    "That worked, thank you.",
    "Adding more detail: it only happens over VPN.",
]
CATEGORIES = ["access", "hardware", "software", "network", "email", "vpn", "printing"]
PRIORITIES = ["P1", "P2", "P3", "P3", "P3", "P4"]  # weighted toward P3


def call(method, path, payload=None):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "sd-traffic/1.0")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(200000)
            ctype = resp.headers.get("Content-Type", "")
            parsed = json.loads(body) if "json" in ctype and body else None
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception as exc:  # noqa: BLE001
        print("  %-6s %-34s -> ERROR %s: %s" % (method, path, type(exc).__name__, exc))
        return None, None


def burst(rng):
    """One plausible slice of user activity: look at the queue, open something,
    and sometimes raise or comment. Weighted so reads dominate writes, which is
    how a real service desk behaves and what makes the flow mix interesting."""
    actions = 0

    status, _ = call("GET", "/health")
    print("  %-6s %-34s -> %s" % ("GET", "/health", status))
    actions += 1

    # Browse the queue the way a person would: the open queue, then a filter.
    for path in ("/api/tickets?status=open&limit=25",
                 "/api/tickets?status=%s&limit=10" % rng.choice(["new", "pending", "resolved"])):
        status, rows = call("GET", path)
        print("  %-6s %-34s -> %s (%s rows)"
              % ("GET", path, status, len(rows) if rows else 0))
        actions += 1

    # Open a couple of individual tickets — the HTML detail page, with its
    # join across tickets/users/comments.
    status, rows = call("GET", "/api/tickets?status=open&limit=40")
    refs = [r["ref"] for r in rows] if rows else []
    for ref in rng.sample(refs, min(len(refs), rng.randint(1, 3))):
        status, _ = call("GET", "/ticket/%s" % ref)
        print("  %-6s %-34s -> %s" % ("GET", "/ticket/%s" % ref, status))
        actions += 1

    # Raise a new ticket about a third of the time.
    if rng.random() < 0.35:
        payload = {"subject": rng.choice(SUBJECTS), "body": rng.choice(BODIES),
                   "category": rng.choice(CATEGORIES), "priority": rng.choice(PRIORITIES)}
        status, created = call("POST", "/api/tickets", payload)
        print("  %-6s %-34s -> %s %s"
              % ("POST", "/api/tickets", status, (created or {}).get("ref", "")))
        actions += 1

    # Comment on an existing ticket about half the time.
    if refs and rng.random() < 0.5:
        ref = rng.choice(refs)
        status, _ = call("POST", "/api/tickets/%s/comments" % ref,
                         {"body": rng.choice(REPLIES)})
        print("  %-6s %-34s -> %s" % ("POST", "/api/tickets/%s/comments" % ref, status))
        actions += 1

    status, data = call("GET", "/stats")
    if data:
        print("  %-6s %-34s -> %s tickets, %s comments"
              % ("GET", "/stats", data["totals"]["tickets"], data["totals"]["comments"]))
    else:
        print("  %-6s %-34s -> %s" % ("GET", "/stats", status))
    actions += 1
    return actions


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="one burst then exit (default)")
    parser.add_argument("--loop", action="store_true", help="keep going until interrupted")
    parser.add_argument("--sleep", type=float, default=20.0, help="seconds between bursts in --loop")
    parser.add_argument("--seed", type=int, default=None, help="fix the RNG for reproducible runs")
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    print("service desk traffic -> %s" % BASE)

    if not args.loop:
        return 0 if burst(rng) else 1
    while True:
        burst(rng)
        time.sleep(args.sleep + rng.uniform(0, args.sleep * 0.5))


if __name__ == "__main__":
    sys.exit(main())
