"""storefront -- the web front end. Owns no database.

Every number on every page here was fetched over HTTP from one of the three
microservices and joined in Python. There is no MariaDB driver in this process
and no database credentials in its environment file; if all three services are
down, this application still starts and still renders, it just has nothing to
say.

That is the point of a backend-for-frontend: one place that knows the topology,
so the browser makes one request and the fan-out, the timeouts and the partial
failures are handled server-side.

Deployed on the SLES web host, port 8080.
"""

import time

from flask import (flash, jsonify, redirect, render_template, request,
                   url_for)

from . import base, clients, config

app = base.make_app("storefront", ensure_schema=False)
app.secret_key = "zms-lab-storefront"  # lab only: flash messages, no sessions

SERVICE_ORDER = ["catalog", "inventory", "orders"]


def _svc(name, path, params=None):
    return clients.get_json(clients.peer_url(name, path), params=params)


def collect_status():
    """Health + stats for all three services, fetched concurrently."""
    started = time.time()
    calls = {}
    for name in SERVICE_ORDER:
        calls[name + ":health"] = (lambda n=name: _svc(n, "/health"))
        calls[name + ":stats"] = (lambda n=name: _svc(n, "/stats"))
    results = clients.fan_out(calls)

    services = []
    for name in SERVICE_ORDER:
        health = results[name + ":health"]
        stats = results[name + ":stats"]
        # A 503 from /health still carries a useful body (the process is up,
        # its database is not), and clients.get_json keeps it. Read it rather
        # than collapsing every non-200 into "unreachable" -- those are two
        # different failures with two different fixes.
        body = health.get("data")
        identity = (body or {}).get("identity", {})
        responded = health["ok"] or health.get("status_code") is not None
        services.append({
            "name": name,
            "reachable": responded,
            "error": health.get("error"),
            "url": (config.PEERS.get(name) or ""),
            "latency_ms": health["elapsed_ms"],
            "status": (body or {}).get(
                "status", "degraded" if responded else "unreachable"),
            "hostname": identity.get("hostname"),
            "node_ip": identity.get("node_ip"),
            "distro": identity.get("distro"),
            "port": identity.get("port"),
            "version": identity.get("version"),
            "database": identity.get("database"),
            "db_status": ((body or {}).get("database") or {}).get("status"),
            "db_error": ((body or {}).get("database") or {}).get("error"),
            "stats": (stats.get("data") or {}).get("stats") if stats["ok"] else None,
        })
    return services, round((time.time() - started) * 1000, 1)


@app.get("/")
def dashboard():
    services, elapsed = collect_status()
    healthy = sum(1 for s in services if s["reachable"])
    return render_template(
        "dashboard.html",
        services=services,
        healthy=healthy,
        total=len(services),
        fanout_ms=elapsed,
        identity=config.identity(),
        timeout=config.HTTP_TIMEOUT,
    )


@app.get("/products")
def products():
    """The join the whole lab exists to demonstrate.

    Page 1: ask catalog for a page of products (one call).
    Page 2: collect those product ids and ask inventory for their stock in ONE
            bulk call -- not one call per row.
    Then merge the two dicts here in memory.

    Two databases on two servers on two different Linux distributions, and the
    user sees one table. If inventory is down the table still renders with the
    stock column showing a dash.
    """
    limit, _ = 40, 0
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    q = (request.args.get("q") or "").strip()

    params = {"limit": limit, "offset": offset}
    if q:
        params["q"] = q
    cat = _svc("catalog", "/products", params)

    rows, total, stock_error = [], 0, None
    if cat["ok"]:
        data = cat["data"]
        total = data.get("total", 0)
        rows = data.get("products", [])
        if rows:
            ids = ",".join(str(r["id"]) for r in rows)
            inv = _svc("inventory", "/stock/bulk", {"ids": ids})
            stock = inv["data"].get("stock", {}) if inv["ok"] else {}
            if not inv["ok"]:
                stock_error = inv["error"]
            for row in rows:
                holding = stock.get(str(row["id"]))
                row["available"] = holding["available"] if holding else None
                row["on_hand"] = holding["on_hand"] if holding else None
                row["sites"] = holding["sites"] if holding else 0
                row["below_reorder"] = holding["below_reorder"] if holding else False

    return render_template(
        "products.html",
        rows=rows, total=total, offset=offset, limit=limit, q=q,
        catalog_error=None if cat["ok"] else cat["error"],
        stock_error=stock_error,
    )


@app.get("/orders")
def orders():
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    status = (request.args.get("status") or "").strip()
    params = {"limit": 40, "offset": offset}
    if status:
        params["status"] = status

    res = _svc("orders", "/orders", params)
    data = res["data"] if res["ok"] else {}
    return render_template(
        "orders.html",
        rows=data.get("orders", []),
        total=data.get("total", 0),
        offset=offset, limit=40, status=status,
        orders_error=None if res["ok"] else res["error"],
    )


@app.get("/orders/<int:order_id>")
def order_detail(order_id):
    """One HTTP call from the browser; three services touched behind it.

    The storefront calls orders-svc, which itself calls catalog and inventory to
    enrich the lines. Watch the enrichment_ms values -- that is a second hop,
    made from a different host than this one.
    """
    res = _svc("orders", "/orders/{0}".format(order_id), {"enrich": "1"})
    if not res["ok"]:
        return render_template("order_detail.html", order=None,
                               error=res["error"], order_id=order_id), 502
    return render_template("order_detail.html", order=res["data"],
                           error=None, order_id=order_id)


@app.get("/new")
def new_order_form():
    cat = _svc("catalog", "/products", {"limit": 60, "active": "1"})
    return render_template(
        "new_order.html",
        products=cat["data"].get("products", []) if cat["ok"] else [],
        catalog_error=None if cat["ok"] else cat["error"],
        result=None,
    )


@app.post("/new")
def new_order_submit():
    """Exercises the write path: storefront -> orders -> catalog + inventory."""
    try:
        product_id = int(request.form.get("product_id", 0))
        qty = int(request.form.get("qty", 1))
    except ValueError:
        flash("Product and quantity must be numbers.", "error")
        return redirect(url_for("new_order_form"))

    payload = {
        "customer_name": (request.form.get("customer_name") or "").strip(),
        "customer_email": (request.form.get("customer_email") or "").strip(),
        "channel": "web",
        "items": [{"product_id": product_id, "qty": qty}],
    }
    res = clients.post_json(clients.peer_url("orders", "/orders"), payload,
                            timeout=config.HTTP_TIMEOUT * 2)

    cat = _svc("catalog", "/products", {"limit": 60, "active": "1"})
    return render_template(
        "new_order.html",
        products=cat["data"].get("products", []) if cat["ok"] else [],
        catalog_error=None if cat["ok"] else cat["error"],
        result=res,
    )


@app.get("/api/topology")
def api_topology():
    """The dashboard as JSON. Handy from curl on the control node."""
    services, elapsed = collect_status()
    return jsonify({
        "storefront": config.identity(),
        "fanout_ms": elapsed,
        "timeout_seconds": config.HTTP_TIMEOUT,
        "services": services,
    })
