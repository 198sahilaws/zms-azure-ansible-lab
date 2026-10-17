"""catalog-svc -- product master data.

Owns zms_catalog. Calls no other service; everything it needs is in its own
database. Deployed on the Ubuntu web host, talking to the RHEL MariaDB host
(which also holds zms_orders) across the VNet.
"""

from flask import jsonify, request

from . import base, config, db

app = base.make_app("catalog")


def _row(r):
    return {
        "id": r["id"],
        "sku": r["sku"],
        "name": r["name"],
        "category": r["category"],
        "supplier": r["supplier"],
        "price_cents": r["price_cents"],
        "price": round(r["price_cents"] / 100.0, 2),
        "active": bool(r["active"]),
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@app.get("/products")
def list_products():
    limit, offset = base.paging()
    where, params = [], []

    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(name LIKE %s OR sku LIKE %s OR supplier LIKE %s)")
        params += ["%" + q + "%"] * 3

    category = (request.args.get("category") or "").strip()
    if category:
        where.append("category = %s")
        params.append(category)

    if request.args.get("active") == "1":
        where.append("active = 1")

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.query_one(
        "SELECT COUNT(*) AS n FROM products" + clause, params)["n"]
    rows = db.query(
        "SELECT * FROM products" + clause +
        " ORDER BY id LIMIT %s OFFSET %s", params + [limit, offset])

    return jsonify({
        "service": "catalog",
        "total": total,
        "limit": limit,
        "offset": offset,
        "products": [_row(r) for r in rows],
    })


@app.get("/products/bulk")
def bulk_products():
    """Fetch many products in ONE call.

    This endpoint exists because of the N+1 problem: the storefront rendering
    50 order lines must not make 50 HTTP calls to this service. It collects the
    ids first and asks once. Every service in a fan-out architecture needs an
    endpoint shaped like this or the page latency is the number of rows times
    the round-trip time.
    """
    ids = base.parse_ids(request.args.get("ids"))
    if not ids:
        return jsonify({"service": "catalog", "products": {}})
    marks = ",".join(["%s"] * len(ids))
    rows = db.query(
        "SELECT * FROM products WHERE id IN ({0})".format(marks), ids)
    return jsonify({
        "service": "catalog",
        "requested": len(ids),
        "found": len(rows),
        "products": {str(r["id"]): _row(r) for r in rows},
    })


@app.get("/products/<int:product_id>")
def get_product(product_id):
    row = db.query_one("SELECT * FROM products WHERE id = %s", (product_id,))
    if row is None:
        return jsonify({"error": "product not found", "id": product_id}), 404
    return jsonify(_row(row))


@app.get("/categories")
def categories():
    rows = db.query(
        "SELECT category, COUNT(*) AS products, "
        "       ROUND(AVG(price_cents) / 100, 2) AS avg_price "
        "FROM products GROUP BY category ORDER BY category")
    return jsonify({"service": "catalog",
                    "categories": [base.numeric_row(r) for r in rows]})


@app.get("/stats")
def stats():
    row = db.query_one(
        "SELECT COUNT(*) AS products, "
        "       SUM(active) AS active_products, "
        "       COUNT(DISTINCT category) AS categories, "
        "       COUNT(DISTINCT supplier) AS suppliers, "
        "       ROUND(AVG(price_cents) / 100, 2) AS avg_price "
        "FROM products")
    return jsonify({"service": "catalog", "database": config.DB_NAME,
                    "db_host": config.DB_HOST, "stats": base.numeric_row(row)})
