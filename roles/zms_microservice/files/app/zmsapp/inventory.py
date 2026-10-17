"""inventory-svc -- stock on hand per product per warehouse.

Owns zms_inventory. Stores product_id as a bare integer and NOTHING else about
a product: no name, no price, no SKU. Anyone who wants those asks catalog-svc.
That is the boundary, and keeping it is what makes the storefront's join real
work instead of decoration.

Deployed on the Ubuntu web host (alongside catalog-svc), talking to the SLES
MariaDB host. On AWS it had an Amazon Linux host of its own.
"""

from flask import jsonify, request

from . import base, config, db

app = base.make_app("inventory")


def _aggregate(rows):
    """Collapse per-warehouse rows into one summary per product id."""
    out = {}
    for r in rows:
        pid = str(r["product_id"])
        entry = out.setdefault(pid, {
            "product_id": r["product_id"],
            "on_hand": 0,
            "reserved": 0,
            "available": 0,
            "sites": 0,
            "below_reorder": False,
            "warehouses": [],
        })
        available = max(0, r["on_hand"] - r["reserved"])
        entry["on_hand"] += r["on_hand"]
        entry["reserved"] += r["reserved"]
        entry["available"] += available
        entry["sites"] += 1
        if r["on_hand"] <= r["reorder_level"]:
            entry["below_reorder"] = True
        entry["warehouses"].append({
            "warehouse": r["warehouse"],
            "on_hand": r["on_hand"],
            "reserved": r["reserved"],
            "available": available,
            "reorder_level": r["reorder_level"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        })
    return out


@app.get("/stock/bulk")
def bulk_stock():
    """Stock for many products in one call -- the N+1 guard, as in catalog."""
    ids = base.parse_ids(request.args.get("ids"))
    if not ids:
        return jsonify({"service": "inventory", "stock": {}})
    marks = ",".join(["%s"] * len(ids))
    rows = db.query(
        "SELECT * FROM stock WHERE product_id IN ({0}) "
        "ORDER BY product_id, warehouse".format(marks), ids)
    return jsonify({
        "service": "inventory",
        "requested": len(ids),
        "stock": _aggregate(rows),
    })


@app.get("/stock/<int:product_id>")
def get_stock(product_id):
    rows = db.query(
        "SELECT * FROM stock WHERE product_id = %s ORDER BY warehouse",
        (product_id,))
    if not rows:
        return jsonify({"error": "no stock record", "product_id": product_id}), 404
    return jsonify(_aggregate(rows)[str(product_id)])


@app.get("/warehouses")
def warehouses():
    rows = db.query(
        "SELECT warehouse, COUNT(*) AS lines, SUM(on_hand) AS on_hand, "
        "       SUM(reserved) AS reserved "
        "FROM stock GROUP BY warehouse ORDER BY warehouse")
    return jsonify({"service": "inventory",
                    "warehouses": [base.numeric_row(r) for r in rows]})


@app.get("/lowstock")
def lowstock():
    limit, offset = base.paging(default_limit=25)
    rows = db.query(
        "SELECT * FROM stock WHERE on_hand <= reorder_level "
        "ORDER BY (reorder_level - on_hand) DESC, product_id LIMIT %s OFFSET %s",
        (limit, offset))
    return jsonify({"service": "inventory", "count": len(rows), "lines": rows})


@app.post("/stock/<int:product_id>/reserve")
def reserve(product_id):
    """Reserve stock for an order line. Called by orders-svc, not by a browser.

    This is the write half of the cross-service story. orders-svc cannot UPDATE
    a stock row -- it has no credentials for this database and no route to this
    schema -- so it asks, and this service decides. If the reservation fails,
    orders-svc gets a 409 and refuses the order rather than writing an order it
    cannot fulfil.
    """
    payload = request.get_json(silent=True) or {}
    try:
        qty = int(payload.get("qty", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "qty must be an integer"}), 400
    if qty < 1:
        return jsonify({"error": "qty must be >= 1"}), 400

    ref = str(payload.get("ref") or "")[:64] or None
    wanted_wh = payload.get("warehouse")

    with db.cursor(commit=True) as cur:
        if wanted_wh:
            cur.execute(
                "SELECT * FROM stock WHERE product_id = %s AND warehouse = %s "
                "FOR UPDATE", (product_id, wanted_wh))
        else:
            # Pick the site with the most free stock. FOR UPDATE holds the row
            # so two concurrent orders cannot both reserve the last unit.
            cur.execute(
                "SELECT * FROM stock WHERE product_id = %s "
                "ORDER BY (on_hand - reserved) DESC LIMIT 1 FOR UPDATE",
                (product_id,))
        row = cur.fetchone()

        if row is None:
            return jsonify({"error": "no stock record",
                            "product_id": product_id}), 404

        available = row["on_hand"] - row["reserved"]
        if available < qty:
            return jsonify({
                "error": "insufficient stock",
                "product_id": product_id,
                "warehouse": row["warehouse"],
                "requested": qty,
                "available": available,
            }), 409

        cur.execute(
            "UPDATE stock SET reserved = reserved + %s, updated_at = UTC_TIMESTAMP() "
            "WHERE id = %s", (qty, row["id"]))
        cur.execute(
            "INSERT INTO stock_moves "
            "(product_id, warehouse, delta, reason, ref, created_at) "
            "VALUES (%s, %s, %s, 'reserve', %s, UTC_TIMESTAMP())",
            (product_id, row["warehouse"], -qty, ref))

    return jsonify({
        "service": "inventory",
        "product_id": product_id,
        "warehouse": row["warehouse"],
        "reserved": qty,
        "remaining_available": available - qty,
        "ref": ref,
    })


@app.post("/stock/<int:product_id>/release")
def release(product_id):
    """Undo a reservation. The compensating action for a failed order.

    There is no distributed transaction here and there cannot be: the order row
    lives in zms_orders on the RHEL MariaDB host and the stock row lives in
    zms_inventory on the SLES one. Two servers, two transaction logs, no shared
    commit. So orders-svc reserves item by item and, if a later item fails,
    calls this endpoint to hand back what it already took. That is a saga --
    forward actions plus compensations -- and it is what replaces ROLLBACK once
    a write spans services.
    """
    payload = request.get_json(silent=True) or {}
    try:
        qty = int(payload.get("qty", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "qty must be an integer"}), 400
    warehouse = payload.get("warehouse")
    ref = str(payload.get("ref") or "")[:64] or None
    if not warehouse:
        return jsonify({"error": "warehouse is required to release"}), 400

    with db.cursor(commit=True) as cur:
        cur.execute(
            "SELECT * FROM stock WHERE product_id = %s AND warehouse = %s "
            "FOR UPDATE", (product_id, warehouse))
        row = cur.fetchone()
        if row is None:
            return jsonify({"error": "no stock record",
                            "product_id": product_id}), 404
        give_back = min(qty, row["reserved"])
        cur.execute(
            "UPDATE stock SET reserved = reserved - %s, "
            "updated_at = UTC_TIMESTAMP() WHERE id = %s",
            (give_back, row["id"]))
        cur.execute(
            "INSERT INTO stock_moves "
            "(product_id, warehouse, delta, reason, ref, created_at) "
            "VALUES (%s, %s, %s, 'release', %s, UTC_TIMESTAMP())",
            (product_id, warehouse, give_back, ref))

    return jsonify({"service": "inventory", "product_id": product_id,
                    "warehouse": warehouse, "released": give_back, "ref": ref})


@app.get("/stats")
def stats():
    row = db.query_one(
        "SELECT COUNT(DISTINCT product_id) AS tracked_products, "
        "       COUNT(*) AS stock_lines, "
        "       SUM(on_hand) AS units_on_hand, "
        "       SUM(reserved) AS units_reserved, "
        "       SUM(CASE WHEN on_hand <= reorder_level THEN 1 ELSE 0 END) AS below_reorder "
        "FROM stock")
    moves = db.query_one("SELECT COUNT(*) AS n FROM stock_moves")
    row["stock_moves"] = moves["n"] if moves else 0
    return jsonify({"service": "inventory", "database": config.DB_NAME,
                    "db_host": config.DB_HOST, "stats": base.numeric_row(row)})
