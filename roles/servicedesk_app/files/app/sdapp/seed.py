"""Populate the service desk with deterministic, realistic-looking data.

    python -m sdapp.seed          # seed if empty, otherwise no-op
    python -m sdapp.seed --force  # wipe and reseed

IDEMPOTENT BY DESIGN. Ansible runs this on every converge, so it checks for
existing tickets and exits without writing. Only a first run (or --force) prints
"inserted", which is what the role's changed_when keys on.

DETERMINISTIC BY DESIGN. Everything derives from one seeded Random, so the same
SD_SEED_RANDOM produces byte-identical data on every host and every rerun. That
matters more than it looks: it makes "did the data change?" a real question you
can answer with a checksum instead of a guess.

WHY THE DISTRIBUTIONS ARE SKEWED. Uniform random data makes every dashboard look
the same and every query cost the same, which is the opposite of useful for a
traffic lab. Real service desks are mostly-closed, mostly-P3, mostly quiet at
3am and busy at 10am, with a long tail of tickets nobody commented on and a few
that turned into 20-comment epics. The shapes below reproduce that.
"""
import argparse
import datetime as dt
import random
import sys

from . import config, db

# --- source material ---------------------------------------------------------

FIRST_NAMES = [
    "Aditi", "Ravi", "Priya", "Marcus", "Elena", "Tom", "Sofia", "Jonas",
    "Mei", "Hassan", "Clara", "Diego", "Anna", "Yusuf", "Grace", "Ivan",
    "Nadia", "Peter", "Lucia", "Omar", "Hannah", "Karl", "Rosa", "Ben",
    "Chiara", "Dmitri", "Fatima", "Liam", "Ingrid", "Sanjay",
]
LAST_NAMES = [
    "Sharma", "Novak", "Okafor", "Lindqvist", "Rossi", "Dubois", "Muller",
    "Costa", "Nakamura", "Ahmed", "Kowalski", "Silva", "Hansen", "Petrov",
    "Bianchi", "Fischer", "Moreau", "Tanaka", "Reyes", "Vasquez",
]

# category -> (subject templates, opening-comment templates)
CATEGORIES = {
    "access": ([
        "Cannot log in to {sys} after password change",
        "Access request: {sys} for new starter",
        "Locked out of {sys} following MFA reset",
        "Permission denied opening shared folder {share}",
        "Group membership missing for {sys}",
    ], [
        "Tried three times, account says locked. Can you reset?",
        "Manager has approved, ticket raised for the access grant.",
        "MFA prompt never arrives on my phone.",
    ]),
    "hardware": ([
        "Laptop will not power on",
        "Docking station not detecting second monitor",
        "Keyboard keys unresponsive after spill",
        "Replacement headset request",
        "Laptop battery drains within an hour",
    ], [
        "No lights at all when plugged in. Tried a different cable.",
        "Worked yesterday, nothing changed that I know of.",
        "Happens only on the docking station, fine when direct.",
    ]),
    "software": ([
        "{app} crashes on startup",
        "License activation failing for {app}",
        "Request installation of {app}",
        "{app} update broke saved templates",
        "Excel macro blocked by policy",
    ], [
        "Splash screen shows then it closes. No error dialog.",
        "It asks for a licence key I was never given.",
        "Needed for the reporting work this quarter.",
    ]),
    "network": ([
        "Wi-Fi drops every few minutes in {loc}",
        "Cannot reach {sys} from the office network",
        "Slow file transfers to the {share} share",
        "Ethernet port dead at desk {desk}",
        "DNS resolution failing for internal hosts",
    ], [
        "Reconnects on its own but drops again within five minutes.",
        "Works from home over VPN, fails in the office.",
        "Speed test looks fine, it is only that one host.",
    ]),
    "email": ([
        "Not receiving external email since {when}",
        "Mailbox full, cannot send",
        "Distribution list {share} missing members",
        "Calendar invites not syncing to phone",
        "Suspicious phishing email reported",
    ], [
        "Internal mail arrives fine, external nothing.",
        "Quota warning every morning, archive is already on.",
        "Forwarded the original as an attachment.",
    ]),
    "vpn": ([
        "VPN disconnects after {n} minutes",
        "Cannot connect to VPN from hotel Wi-Fi",
        "VPN client update loop on Windows",
        "Split tunnel not routing {sys} traffic",
    ], [
        "Reproduces on two different networks.",
        "Client says 'connected' but nothing routes.",
        "Started after the client auto-updated.",
    ]),
    "printing": ([
        "Printer {loc} jams on duplex jobs",
        "Cannot add network printer {loc}",
        "Print jobs stuck in queue",
        "Badge release not recognising my card",
    ], [
        "Single sided is fine, duplex jams every time.",
        "Driver install fails at the last step.",
        "Queue shows the job then it silently disappears.",
    ]),
    "onboarding": ([
        "New starter setup: {name}, starts {when}",
        "Leaver offboarding: {name}",
        "Desk move request to {loc}",
        "Equipment request for contractor {name}",
    ], [
        "Standard build plus the finance software please.",
        "Please disable accounts end of day Friday.",
        "Moving with the whole team, four desks total.",
    ]),
}

SYSTEMS = ["the HR portal", "Confluence", "the finance system", "Jira",
           "the VPN gateway", "the intranet", "SharePoint", "the CRM"]
APPS = ["Outlook", "Teams", "AutoCAD", "Photoshop", "Power BI", "Slack", "Zoom"]
LOCATIONS = ["Floor 2 East", "Floor 3 North", "the Dublin office",
             "the Berlin office", "Meeting Room 4", "the ground floor"]
SHARES = ["\\\\fs01\\finance", "\\\\fs01\\projects", "\\\\fs02\\shared",
          "all-engineering", "\\\\fs01\\hr"]

AGENT_REPLIES = [
    "Thanks for raising this, picking it up now.",
    "Could you confirm the exact error message you see?",
    "I have reproduced this on a test account.",
    "Escalating to the platform team, they own that system.",
    "Rolled back the change, please try again and confirm.",
    "Access has been granted, allow 15 minutes to propagate.",
    "Replacement ordered, should arrive tomorrow.",
    "Cleared the queue on the server, jobs are flowing.",
    "This looks like the same root cause as the earlier incident.",
    "Closing as resolved. Reopen if it recurs.",
]
REQUESTER_REPLIES = [
    "That fixed it, thanks very much.",
    "Still happening, same error.",
    "Sorry for the delay, just tested and it looks better.",
    "Attaching a screenshot of the error.",
    "Any update on this one?",
    "Confirmed working from the office too.",
]
INTERNAL_NOTES = [
    "Root cause was a stale DNS entry, cleaned up.",
    "Known issue with the 4.2 client, vendor case open.",
    "User is on the legacy build, needs the migration.",
    "Duplicate of an earlier ticket this week.",
    "Waiting on the change window before applying.",
]

# Weighted so the queue looks like a real one: mostly finished work, a
# manageable amount in flight, a handful untriaged.
STATUS_WEIGHTS = [("closed", 50), ("resolved", 16), ("open", 16),
                  ("pending", 12), ("new", 6)]
PRIORITY_WEIGHTS = [("P1", 4), ("P2", 20), ("P3", 55), ("P4", 21)]
# Comment-count distribution: a long tail, and a real share with none at all.
COMMENT_WEIGHTS = [(0, 14), (1, 20), (2, 22), (3, 16), (4, 11),
                   (5, 7), (6, 4), (8, 3), (11, 2), (17, 1)]


def _weighted(rng, pairs):
    total = sum(w for _, w in pairs)
    r = rng.uniform(0, total)
    upto = 0.0
    for value, weight in pairs:
        upto += weight
        if r <= upto:
            return value
    return pairs[-1][0]


def _business_moment(rng, start, days):
    """A datetime inside the window, biased to weekday business hours.

    Without this every chart is a flat band across 24 hours and the traffic
    looks synthetic at a glance. Roughly 80% of tickets land Mon-Fri 08:00-18:00
    with a mid-morning peak; the rest are spread thin across evenings and
    weekends, which is what an on-call tail actually looks like.
    """
    for _ in range(12):
        offset = rng.random() * days
        moment = start + dt.timedelta(days=offset)
        weekend = moment.weekday() >= 5
        # Triangular around 10:30 keeps a realistic mid-morning peak.
        hour = min(23, max(0, int(rng.triangular(7, 18, 10.5))))
        moment = moment.replace(hour=hour,
                                minute=rng.randrange(60),
                                second=rng.randrange(60),
                                microsecond=0)
        if weekend and rng.random() > 0.12:
            continue
        if not (8 <= hour < 18) and rng.random() > 0.25:
            continue
        return moment
    return moment


def _fill(rng, template, names):
    return template.format(
        sys=rng.choice(SYSTEMS), app=rng.choice(APPS), loc=rng.choice(LOCATIONS),
        share=rng.choice(SHARES), name=rng.choice(names),
        desk="%d-%02d" % (rng.randint(1, 4), rng.randint(1, 40)),
        n=rng.choice([5, 10, 15, 20, 30]),
        when=rng.choice(["Monday", "last Friday", "the weekend",
                         "next Monday", "this morning"]),
    )


def build(rng, now):
    """Produce (users, tickets, comments) as plain tuples ready for executemany."""
    people = []
    seen = set()
    while len(people) < config.SEED_USERS:
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        username = ("%s.%s" % (first, last)).lower()
        if username in seen:
            continue
        seen.add(username)
        people.append((first, last, username))

    # First quarter of the list are the agents who get tickets assigned.
    n_agents = max(3, config.SEED_USERS // 4)
    users = []
    for i, (first, last, username) in enumerate(people):
        role = "agent" if i < n_agents else "requester"
        users.append((username, "%s %s" % (first, last),
                      "%s@example.internal" % username, role))
    agent_ids = list(range(1, n_agents + 1))          # ids are 1-based, in order
    requester_ids = list(range(n_agents + 1, len(users) + 1))
    full_names = ["%s %s" % (f, l) for f, l, _ in people]

    window_start = now - dt.timedelta(days=config.SEED_DAYS)
    tickets, comments = [], []

    for i in range(config.SEED_TICKETS):
        category = rng.choice(list(CATEGORIES))
        subjects, openers = CATEGORIES[category]
        created = _business_moment(rng, window_start, config.SEED_DAYS)
        status = _weighted(rng, STATUS_WEIGHTS)
        priority = _weighted(rng, PRIORITY_WEIGHTS)

        # A brand-new ticket that was raised 80 days ago is not credible, so
        # pull anything still 'new' into the last few days.
        if status == "new" and (now - created).days > 4:
            created = now - dt.timedelta(days=rng.random() * 4)
            created = created.replace(microsecond=0)

        requester = rng.choice(requester_ids)
        assignee = None if status == "new" else rng.choice(agent_ids)

        # Higher priority closes faster. P1 in hours, P4 in weeks.
        hours = {"P1": rng.uniform(0.5, 8), "P2": rng.uniform(2, 48),
                 "P3": rng.uniform(4, 240), "P4": rng.uniform(24, 600)}[priority]
        resolved_at = created + dt.timedelta(hours=hours)
        if resolved_at > now:
            resolved_at = now
        closed_at = resolved_at if status in ("resolved", "closed") else None
        updated = closed_at or min(now, created + dt.timedelta(hours=hours * rng.random()))

        tickets.append((
            "SD-%06d" % (i + 1),
            _fill(rng, rng.choice(subjects), full_names),
            _fill(rng, rng.choice(openers), full_names),
            category, status, priority, requester, assignee,
            created, updated, closed_at,
        ))

        # Comments run between the ticket opening and its last activity.
        n_comments = _weighted(rng, COMMENT_WEIGHTS)
        if status == "new":
            n_comments = min(n_comments, 1)
        span = max((updated - created).total_seconds(), 60)
        moments = sorted(created + dt.timedelta(seconds=rng.random() * span)
                         for _ in range(n_comments))
        for j, moment in enumerate(moments):
            # Agents answer first and most; internal notes are agent-only.
            by_agent = (j % 2 == 0) if assignee else False
            if by_agent:
                internal = rng.random() < 0.22
                body = rng.choice(INTERNAL_NOTES if internal else AGENT_REPLIES)
                author = assignee
            else:
                internal = False
                body = rng.choice(REQUESTER_REPLIES)
                author = requester
            comments.append((i + 1, author, body, 1 if internal else 0, moment))

    return users, tickets, comments


def seed(force=False):
    rng = random.Random(config.SEED_RANDOM)
    now = dt.datetime.now().replace(microsecond=0)

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM tickets")
            existing = cur.fetchone()["n"]
            if existing and not force:
                print("already seeded: %d tickets present, nothing to do" % existing)
                return 0
            if force and existing:
                # Children first; FK on comments is ON DELETE CASCADE but
                # tickets->users is not, so order still matters.
                cur.execute("DELETE FROM comments")
                cur.execute("DELETE FROM tickets")
                cur.execute("DELETE FROM users")
                for table in ("comments", "tickets", "users"):
                    cur.execute("ALTER TABLE %s AUTO_INCREMENT = 1" % table)
                print("cleared existing data (--force)")

            users, tickets, comments = build(rng, now)

            cur.executemany(
                "INSERT INTO users (username, full_name, email, role) "
                "VALUES (%s, %s, %s, %s)", users)
            cur.executemany(
                "INSERT INTO tickets (ref, subject, body, category, status, "
                "priority, requester_id, assignee_id, created_at, updated_at, "
                "closed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                tickets)
            cur.executemany(
                "INSERT INTO comments (ticket_id, author_id, body, is_internal, "
                "created_at) VALUES (%s, %s, %s, %s, %s)", comments)
        conn.commit()
        print("inserted %d users, %d tickets, %d comments (seed=%d, %d days)"
              % (len(users), len(tickets), len(comments),
                 config.SEED_RANDOM, config.SEED_DAYS))
        return 0
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seed the service desk database.")
    parser.add_argument("--force", action="store_true",
                        help="delete existing rows and reseed")
    args = parser.parse_args(argv)
    return seed(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
