"""orders-svc -- orders and order lines, plus the only cross-service WRITE.

Owns zms_orders. Unlike catalog and inventory, this service is a CLIENT as well
as a server: it calls catalog-svc to price and validate a product, and
inventory-svc to reserve stock, before it writes anything of its own.

Deployed on the RHEL 9 web host, talking to the RHEL 9 MariaDB host. It is the
only service whose app and database share a distro, which makes it the control
case when a cross-distro path misbehaves.
"""

from flask import jsonify, request

from . import base, clients, config, db

app = base.make_app("orders")

VALID_STATUS = {"placed", "picking", "shipped", "delivered", "cancelled"}


def _order_row(r):
    return {
        "id": r["id"],
        "order_ref": r["order_ref"],
        "customer_name": r["customer_name"],
        "customer_email": r["customer_email"],
        "status": r["status"],
        "channel": r["channel"],
        "total_cents": r["total_cents"],
        "total": round(r["total_cents"] / 100.0, 2),
        "placed_at": r["placed_at"].isoformat() if r["placed_at"] else None,
    }


@app.get("/orders")
def list_orders():
    limit, offset = base.paging()
    where, params = [], []

    status = (request.args.get("status") or "").strip()
    if status in VALID_STATUS:
        where.append("status = %s")
        params.append(status)

    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(order_ref LIKE %s OR customer_name LIKE %s "
                     "OR customer_email LIKE %s)")
        params += ["%" + q + "%"] * 3

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.query_one("SELECT COUNT(*) AS n FROM orders" + clause, params)["n"]
    rows = db.query(
        "SELECT o.*, "
        "  (SELECT COUNT(*) FROM order_items i WHERE i.order_id = o.id) AS lines "
        "FROM orders o" + clause +
        " ORDER BY placed_at DESC LIMIT %s OFFSET %s", params + [limit, offset])

    out = []
    for r in rows:
        item = _order_row(r)
        item["lines"] = r["lines"]
        out.append(item)

    return jsonify({"service": "orders", "total": total, "limit": limit,
                    "offset": offset, "orders": out})


@app.get("/orders/<int:order_id>")
def get_order(order_id):
    """One order. `?enrich=1` adds product names and live stock.

    Without enrich this returns exactly what zms_orders knows: product ids and
    the price that was charged. With enrich it makes TWO bulk calls -- one to
    catalog, one to inventory -- and merges the answers in memory here. There
    is no SQL join anywhere in this function, and there could not be: the three
    tables are on three different servers.

    Enrichment degrades. If catalog is down you still get the order, with
    enrichment_errors telling you which part is missing. An order the customer
    placed is more important than the product name next to it.
    """
    order = db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))
    if order is None:
        return jsonify({"error": "order not found", "id": order_id}), 404

    items = db.query(
        "SELECT * FROM order_items WHERE order_id = %s ORDER BY id", (order_id,))
    payload = _order_row(order)
    payload["items"] = [{
        "product_id": i["product_id"],
        "qty": i["qty"],
        "unit_price_cents": i["unit_price_cents"],
        "unit_price": round(i["unit_price_cents"] / 100.0, 2),
        "line_total": round(i["qty"] * i["unit_price_cents"] / 100.0, 2),
    } for i in items]

    if request.args.get("enrich") != "1" or not items:
        return jsonify(payload)

    ids = ",".join(str(i["product_id"]) for i in items)
    results = clients.fan_out({
        "catalog": lambda: clients.get_json(
            clients.peer_url("catalog", "/products/bulk"), params={"ids": ids}),
        "inventory": lambda: clients.get_json(
            clients.peer_url("inventory", "/stock/bulk"), params={"ids": ids}),
    })

    products = {}
    stock = {}
    errors = {}
    if results["catalog"]["ok"]:
        products = results["catalog"]["data"].get("products", {})
    else:
        errors["catalog"] = results["catalog"]["error"]
    if results["inventory"]["ok"]:
        stock = results["inventory"]["data"].get("stock", {})
    else:
        errors["inventory"] = results["inventory"]["error"]

    for line in payload["items"]:
        key = str(line["product_id"])
        product = products.get(key)
        line["name"] = product["name"] if product else None
        line["sku"] = product["sku"] if product else None
        line["current_price"] = product["price"] if product else None
        line["price_changed"] = (
            bool(product) and product["price_cents"] != line["unit_price_cents"])
        holding = stock.get(key)
        line["available_now"] = holding["available"] if holding else None

    payload["enriched_from"] = {
        "catalog": results["catalog"]["url"],
        "inventory": results["inventory"]["url"],
    }
    payload["enrichment_ms"] = {
        "catalog": results["catalog"]["elapsed_ms"],
        "inventory": results["inventory"]["elapsed_ms"],
    }
    if errors:
        payload["enrichment_errors"] = errors
    return jsonify(payload)


@app.post("/orders")
def create_order():
    """Place an order across three services. The saga.

      1. ask catalog to confirm the products exist, are active, and what they cost
      2. ask inventory to reserve each line
      3. write the order locally
      4. if step 2 fails partway, release everything already reserved

    Steps 1-3 are three separate transactions on three separate servers. There
    is no way to make them atomic, so the code is written to leave the estate
    consistent at every point where it can fail.
    """
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("customer_name") or "").strip()
    email = str(payload.get("customer_email") or "").strip()
    channel = str(payload.get("channel") or "web").strip()[:24]
    raw_items = payload.get("items") or []

    if not name or not email:
        return jsonify({"error": "customer_name and customer_email are required"}), 400
    if not isinstance(raw_items, list) or not raw_items:
        return jsonify({"error": "items must be a non-empty list"}), 400

    items = []
    for entry in raw_items[:20]:
        try:
            pid = int(entry.get("product_id"))
            qty = int(entry.get("qty", 1))
        except (AttributeError, TypeError, ValueError):
            return jsonify({"error": "each item needs product_id and qty"}), 400
        if qty < 1:
            return jsonify({"error": "qty must be >= 1"}), 400
        items.append({"product_id": pid, "qty": qty})

    # --- 1. price and validate against catalog -----------------------------
    ids = ",".join(str(i["product_id"]) for i in items)
    cat = clients.get_json(
        clients.peer_url("catalog", "/products/bulk"), params={"ids": ids})
    if not cat["ok"]:
        # Refuse rather than guess a price. A wrong price is worse than a
        # failed order, and the customer can retry.
        return jsonify({"error": "catalog unavailable, cannot price order",
                        "detail": cat["error"]}), 503
    products = cat["data"].get("products", {})

    for item in items:
        product = products.get(str(item["product_id"]))
        if product is None:
            return jsonify({"error": "unknown product",
                            "product_id": item["product_id"]}), 400
        if not product["active"]:
            return jsonify({"error": "product is discontinued",
                            "product_id": item["product_id"]}), 400
        item["unit_price_cents"] = product["price_cents"]
        item["name"] = product["name"]

    # --- 2. reserve stock, remembering what to undo ------------------------
    reserved = []
    for item in items:
        res = clients.post_json(
            clients.peer_url(
                "inventory", "/stock/{0}/reserve".format(item["product_id"])),
            {"qty": item["qty"], "ref": "pending"})
        if not res["ok"]:
            _release_all(reserved)
            code = 409 if "409" in str(res["error"]) else 503
            return jsonify({
                "error": "could not reserve stock",
                "product_id": item["product_id"],
                "detail": res["error"],
                "compensated": [r["product_id"] for r in reserved],
            }), code
        reserved.append({
            "product_id": item["product_id"],
            "qty": item["qty"],
            "warehouse": res["data"]["warehouse"],
        })

    # --- 3. write our own order --------------------------------------------
    total = sum(i["qty"] * i["unit_price_cents"] for i in items)
    try:
        with db.cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO orders (order_ref, customer_name, customer_email, "
                " status, channel, total_cents, placed_at) "
                "VALUES ('PENDING', %s, %s, 'placed', %s, %s, UTC_TIMESTAMP())",
                (name[:120], email[:160], channel, total))
            order_id = cur.lastrowid
            cur.execute(
                "UPDATE orders SET order_ref = %s WHERE id = %s",
                ("ZMSO-{0:06d}".format(order_id), order_id))
            for item in items:
                cur.execute(
                    "INSERT INTO order_items "
                    "(order_id, product_id, qty, unit_price_cents) "
                    "VALUES (%s, %s, %s, %s)",
                    (order_id, item["product_id"], item["qty"],
                     item["unit_price_cents"]))
    except Exception:
        # Our own database failed after stock was already reserved elsewhere.
        # Hand it all back before surfacing the error, or the estate is left
        # holding stock for an order that does not exist.
        _release_all(reserved)
        raise

    return jsonify({
        "service": "orders",
        "id": order_id,
        "order_ref": "ZMSO-{0:06d}".format(order_id),
        "status": "placed",
        "total": round(total / 100.0, 2),
        "items": [{"product_id": i["product_id"], "name": i["name"],
                   "qty": i["qty"],
                   "unit_price": round(i["unit_price_cents"] / 100.0, 2)}
                  for i in items],
        "reservations": reserved,
    }), 201


def _release_all(reserved):
    """Best-effort compensation. Logged, never raised -- we are already failing."""
    for entry in reserved:
        clients.post_json(
            clients.peer_url(
                "inventory", "/stock/{0}/release".format(entry["product_id"])),
            {"qty": entry["qty"], "warehouse": entry["warehouse"],
             "ref": "compensate"})


@app.get("/dependencies")
def dependencies():
    """What orders-svc can see from where it stands.

    Useful when the storefront reaches a service but orders-svc cannot -- a
    security group or host firewall difference between two app hosts shows up
    here and nowhere else.
    """
    results = clients.fan_out({
        "catalog": lambda: clients.get_json(clients.peer_url("catalog", "/health")),
        "inventory": lambda: clients.get_json(
            clients.peer_url("inventory", "/health")),
    })
    return jsonify({
        "service": "orders",
        "seen_from": config.HOSTNAME,
        "dependencies": {
            name: {"ok": r["ok"], "url": r["url"],
                   "elapsed_ms": r["elapsed_ms"],
                   "error": r.get("error")}
            for name, r in results.items()
        },
    })


@app.get("/stats")
def stats():
    totals = db.query_one(
        "SELECT COUNT(*) AS orders, "
        "       SUM(total_cents) AS revenue_cents, "
        "       ROUND(AVG(total_cents) / 100, 2) AS avg_order_value "
        "FROM orders WHERE status <> 'cancelled'")
    by_status = db.query(
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC")
    recent = db.query_one(
        "SELECT COUNT(*) AS n FROM orders "
        "WHERE placed_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 DAY)")
    # SUM() and AVG() arrive as Decimal; base.numeric() turns them into plain
    # numbers before any arithmetic. Dividing a Decimal by a float raises.
    revenue_cents = base.numeric(totals["revenue_cents"]) or 0
    return jsonify({
        "service": "orders",
        "database": config.DB_NAME,
        "db_host": config.DB_HOST,
        "stats": {
            "orders": base.numeric(totals["orders"]),
            "revenue": round(revenue_cents / 100.0, 2),
            "avg_order_value": base.numeric(totals["avg_order_value"]),
            "last_24h": base.numeric(recent["n"]),
            "by_status": {r["status"]: base.numeric(r["n"]) for r in by_status},
        },
    })
