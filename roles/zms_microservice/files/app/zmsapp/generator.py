"""Fake customer traffic, so the dashboard moves on its own.

    python -m zmsapp.generator --once     # place one random order
    python -m zmsapp.generator --count 5

Runs from a systemd timer on the orders host (see the zms-order-generator
units). It talks to the services over HTTP exactly as a browser would -- it has
no database access of its own, which is deliberate: seeding through the front
door exercises the whole saga, including the reservation and its compensation,
rather than quietly inserting rows that could never have been ordered.
"""

import argparse
import random
import sys

from . import clients, config

NAMES = [
    "Ada Lovelace", "Grace Hopper", "Alan Turing", "Radia Perlman",
    "Vint Cerf", "Barbara Liskov", "Ken Thompson", "Margaret Hamilton",
    "Leslie Lamport", "Karen Sparck Jones", "Jean Bartik", "Tony Hoare",
]


def place_one(rng):
    """Pick real, in-stock products and order them. Returns an envelope."""
    catalog_url = clients.peer_url("catalog", "/products")
    listing = clients.get_json(
        catalog_url,
        params={"limit": 60, "active": "1",
                "offset": rng.randint(0, max(0, config.SEED_PRODUCTS - 60))})
    if not listing["ok"]:
        return {"ok": False, "error": "catalog: " + listing["error"]}

    products = listing["data"].get("products", [])
    if not products:
        return {"ok": False, "error": "catalog returned no products"}

    chosen = rng.sample(products, min(len(products), rng.randint(1, 3)))
    person = rng.choice(NAMES)
    payload = {
        "customer_name": person,
        "customer_email": person.lower().replace(" ", ".") + "@example.invalid",
        "channel": rng.choice(["web", "web", "partner", "phone"]),
        "items": [{"product_id": p["id"], "qty": rng.randint(1, 3)}
                  for p in chosen],
    }
    return clients.post_json(clients.peer_url("orders", "/orders"), payload,
                             timeout=max(6.0, config.HTTP_TIMEOUT * 2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate fake order traffic")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--once", action="store_true",
                        help="alias for --count 1")
    args = parser.parse_args(argv)
    count = 1 if args.once else max(1, args.count)

    rng = random.Random()
    placed = refused = 0
    for _ in range(count):
        result = place_one(rng)
        if result["ok"]:
            placed += 1
            print("placed {0} ({1} ms)".format(
                result["data"]["order_ref"], result["elapsed_ms"]))
        else:
            refused += 1
            # A refusal is a normal outcome here: out of stock, a discontinued
            # product, or a service that is down. Exit 0 either way so the
            # systemd timer does not fill the journal with unit failures during
            # a deliberate failure demo.
            print("refused: {0}".format(result.get("error")), file=sys.stderr)
    print("generator: {0} placed, {1} refused".format(placed, refused))
    return 0


if __name__ == "__main__":
    sys.exit(main())
