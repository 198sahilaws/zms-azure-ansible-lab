"""Deterministic fake-data seeder.

    /opt/zms-app/venv/bin/python -m zmsapp.seed          # seed if empty
    /opt/zms-app/venv/bin/python -m zmsapp.seed --force  # wipe and reseed
    /opt/zms-app/venv/bin/python -m zmsapp.seed --check  # report row counts

Ansible runs the plain form on every converge, so it is a no-op once the data
is there.

THE ID TRICK. catalog and inventory are seeded on two different hosts against
two different MariaDB servers, and neither can see the other's data. They line
up because both draw product ids from the SAME range (1..ZMS_SEED_PRODUCTS) and
both use the SAME ZMS_SEED_RANDOM value. That is a lab convenience standing in
for what a real system would do -- publish an event, or have inventory call
catalog on first sight of a product id.
"""

import argparse
import random
import sys
from datetime import datetime, timedelta

from . import config, db, schema

CATEGORIES = [
    "Networking", "Storage", "Compute", "Security", "Peripherals",
    "Cabling", "Power", "Optics", "Racks", "Licensing",
]

WAREHOUSES = ["PAR-1", "FRA-2", "AMS-3", "MUM-4"]

ORDER_STATUS = (
    ["delivered"] * 9 + ["shipped"] * 5 + ["picking"] * 3 +
    ["placed"] * 4 + ["cancelled"] * 1
)

CHANNELS = ["web", "web", "web", "partner", "phone", "field-sales"]


def _faker():
    """Faker if available, else None -- the seeder still works without it."""
    try:
        from faker import Faker
    except ImportError:
        return None
    fake = Faker("en_GB")
    Faker.seed(config.RANDOM_SEED)
    return fake


def _rng(offset=0):
    return random.Random(config.RANDOM_SEED + offset)


def _count(table):
    row = db.query_one("SELECT COUNT(*) AS n FROM {0}".format(table))
    return int(row["n"]) if row else 0


# --- catalog ---------------------------------------------------------------

def seed_catalog(force=False):
    schema.ensure("catalog", db)
    if force:
        db.execute("DELETE FROM products")
    elif _count("products") > 0:
        return 0, "already seeded"

    rng = _rng(1)
    fake = _faker()
    now = datetime.utcnow()
    rows = []
    for pid in range(1, config.SEED_PRODUCTS + 1):
        category = CATEGORIES[pid % len(CATEGORIES)]
        if fake is not None:
            noun = fake.word().capitalize()
            supplier = fake.company()
        else:
            noun = "Part{0:03d}".format(pid)
            supplier = "Supplier {0}".format(rng.randint(1, 40))
        name = "{0} {1} {2}".format(
            category[:-1] if category.endswith("s") else category,
            noun,
            rng.choice(["Mk I", "Mk II", "Pro", "Lite", "XL", "2U", "48p"]),
        )
        rows.append((
            pid,
            "ZMS-{0:05d}".format(pid),
            name[:160],
            category,
            supplier[:120],
            rng.randrange(900, 480000, 50),          # 9.00 .. 4800.00
            0 if rng.random() < 0.04 else 1,          # a few discontinued
            now - timedelta(days=rng.randint(30, 900)),
        ))

    with db.cursor(commit=True) as cur:
        cur.executemany(
            "INSERT INTO products "
            "(id, sku, name, category, supplier, price_cents, active, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
    return len(rows), "inserted"


# --- inventory -------------------------------------------------------------

def seed_inventory(force=False):
    schema.ensure("inventory", db)
    if force:
        db.execute("DELETE FROM stock_moves")
        db.execute("DELETE FROM stock")
    elif _count("stock") > 0:
        return 0, "already seeded"

    rng = _rng(2)
    now = datetime.utcnow()
    stock_rows = []
    move_rows = []
    for pid in range(1, config.SEED_PRODUCTS + 1):
        # Not every product is held in every warehouse -- that is what makes
        # the storefront's "in stock across N sites" column interesting.
        for wh in rng.sample(WAREHOUSES, rng.randint(1, len(WAREHOUSES))):
            on_hand = rng.choice([0, 0, 3, 12, 25, 60, 140, 400])
            reserved = rng.randint(0, min(5, on_hand)) if on_hand else 0
            stock_rows.append((
                pid, wh, on_hand, reserved,
                rng.choice([5, 10, 20, 25]),
                now - timedelta(hours=rng.randint(0, 720)),
            ))
            if on_hand:
                move_rows.append((
                    pid, wh, on_hand, "initial-receipt", None,
                    now - timedelta(days=rng.randint(5, 200)),
                ))

    with db.cursor(commit=True) as cur:
        cur.executemany(
            "INSERT INTO stock "
            "(product_id, warehouse, on_hand, reserved, reorder_level, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            stock_rows,
        )
        if move_rows:
            cur.executemany(
                "INSERT INTO stock_moves "
                "(product_id, warehouse, delta, reason, ref, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                move_rows,
            )
    return len(stock_rows), "inserted"


# --- orders ----------------------------------------------------------------

def seed_orders(force=False):
    schema.ensure("orders", db)
    if force:
        db.execute("DELETE FROM order_items")
        db.execute("DELETE FROM orders")
    elif _count("orders") > 0:
        return 0, "already seeded"

    rng = _rng(3)
    fake = _faker()
    now = datetime.utcnow()

    with db.cursor(commit=True) as cur:
        for n in range(1, config.SEED_ORDERS + 1):
            if fake is not None:
                name = fake.name()
                email = fake.email()
            else:
                name = "Customer {0}".format(n)
                email = "customer{0}@example.invalid".format(n)
            placed = now - timedelta(
                days=rng.randint(0, 45), minutes=rng.randint(0, 1439)
            )
            cur.execute(
                "INSERT INTO orders "
                "(order_ref, customer_name, customer_email, status, channel, "
                " total_cents, placed_at) VALUES (%s, %s, %s, %s, %s, 0, %s)",
                ("ZMSO-{0:06d}".format(n), name[:120], email[:160],
                 rng.choice(ORDER_STATUS), rng.choice(CHANNELS), placed),
            )
            order_id = cur.lastrowid
            total = 0
            # unit_price_cents is a SNAPSHOT of what was charged, not a lookup
            # into catalog. Orders must survive a later price change, and it
            # cannot join to another server's table anyway.
            for pid in rng.sample(
                range(1, config.SEED_PRODUCTS + 1), rng.randint(1, 4)
            ):
                qty = rng.randint(1, 6)
                unit = rng.randrange(900, 480000, 50)
                total += qty * unit
                cur.execute(
                    "INSERT INTO order_items "
                    "(order_id, product_id, qty, unit_price_cents) "
                    "VALUES (%s, %s, %s, %s)",
                    (order_id, pid, qty, unit),
                )
            cur.execute(
                "UPDATE orders SET total_cents = %s WHERE id = %s",
                (total, order_id),
            )
    return config.SEED_ORDERS, "inserted"


SEEDERS = {
    "catalog": (seed_catalog, ["products"]),
    "inventory": (seed_inventory, ["stock", "stock_moves"]),
    "orders": (seed_orders, ["orders", "order_items"]),
}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seed a ZMS lab service database")
    parser.add_argument("--force", action="store_true",
                        help="delete existing rows and regenerate")
    parser.add_argument("--check", action="store_true",
                        help="report row counts and exit")
    parser.add_argument("--service", default=config.SERVICE,
                        help="override ZMS_SERVICE")
    args = parser.parse_args(argv)

    entry = SEEDERS.get(args.service)
    if entry is None:
        print("seed: '{0}' owns no database, nothing to do".format(args.service))
        return 0
    seeder, tables = entry

    if not config.HAS_DB:
        print("seed: ZMS_DB_HOST/ZMS_DB_NAME are not set", file=sys.stderr)
        return 2

    try:
        if args.check:
            schema.ensure(args.service, db)
            for table in tables:
                print("{0}.{1}: {2} rows".format(
                    config.DB_NAME, table, _count(table)))
            return 0
        count, note = seeder(force=args.force)
        print("seed[{0}] -> {1}@{2}: {3} ({4} rows)".format(
            args.service, config.DB_NAME, config.DB_HOST, note, count))
        return 0
    except db.DatabaseUnavailable as exc:
        print("seed: {0}".format(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
