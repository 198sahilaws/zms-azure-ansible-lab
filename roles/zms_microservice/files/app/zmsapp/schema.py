"""DDL for each service's own schema.

One dict entry per service. A service only ever sees its own statements -- the
catalog process has no idea what an order_items table looks like, which is the
separation the lab exists to show.

NOTE ON FOREIGN KEYS. orders.order_items has a real FK to orders.id because both
tables live in the same schema on the same server. There is deliberately NO FK
from order_items.product_id to a products table: that table is in a different
schema, on a different MariaDB server, on a different distro. MariaDB cannot
enforce it and neither can any other engine. Referential integrity across a
service boundary is the application's job -- which is why orders-svc validates
product ids by CALLING catalog-svc before it writes a row.
"""

SCHEMAS = {
    "catalog": [
        """
        CREATE TABLE IF NOT EXISTS products (
            id            INT UNSIGNED NOT NULL,
            sku           VARCHAR(32)  NOT NULL,
            name          VARCHAR(160) NOT NULL,
            category      VARCHAR(64)  NOT NULL,
            supplier      VARCHAR(120) NOT NULL,
            price_cents   INT UNSIGNED NOT NULL,
            active        TINYINT(1)   NOT NULL DEFAULT 1,
            created_at    DATETIME     NOT NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uq_products_sku (sku),
            KEY idx_products_category (category)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
    ],
    "inventory": [
        """
        CREATE TABLE IF NOT EXISTS stock (
            id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
            product_id    INT UNSIGNED NOT NULL,
            warehouse     VARCHAR(32)  NOT NULL,
            on_hand       INT          NOT NULL DEFAULT 0,
            reserved      INT          NOT NULL DEFAULT 0,
            reorder_level INT          NOT NULL DEFAULT 0,
            updated_at    DATETIME     NOT NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uq_stock_product_wh (product_id, warehouse),
            KEY idx_stock_product (product_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS stock_moves (
            id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            product_id INT UNSIGNED    NOT NULL,
            warehouse  VARCHAR(32)     NOT NULL,
            delta      INT             NOT NULL,
            reason     VARCHAR(64)     NOT NULL,
            ref        VARCHAR(64)     DEFAULT NULL,
            created_at DATETIME        NOT NULL,
            PRIMARY KEY (id),
            KEY idx_moves_product (product_id),
            KEY idx_moves_created (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
    ],
    "orders": [
        """
        CREATE TABLE IF NOT EXISTS orders (
            id             INT UNSIGNED NOT NULL AUTO_INCREMENT,
            order_ref      VARCHAR(24)  NOT NULL,
            customer_name  VARCHAR(120) NOT NULL,
            customer_email VARCHAR(160) NOT NULL,
            status         VARCHAR(24)  NOT NULL,
            channel        VARCHAR(24)  NOT NULL,
            total_cents    INT UNSIGNED NOT NULL DEFAULT 0,
            placed_at      DATETIME     NOT NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uq_orders_ref (order_ref),
            KEY idx_orders_status (status),
            KEY idx_orders_placed (placed_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS order_items (
            id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            order_id         INT UNSIGNED    NOT NULL,
            product_id       INT UNSIGNED    NOT NULL,
            qty              INT UNSIGNED    NOT NULL,
            unit_price_cents INT UNSIGNED    NOT NULL,
            PRIMARY KEY (id),
            KEY idx_items_order (order_id),
            KEY idx_items_product (product_id),
            CONSTRAINT fk_items_order FOREIGN KEY (order_id)
                REFERENCES orders (id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
    ],
}


def ensure(service, db_module):
    """Create this service's tables if they are missing. Idempotent.

    Called by the seeder at deploy time and again by each app at import time,
    so a service that starts before its schema exists heals itself instead of
    500ing forever.
    """
    statements = SCHEMAS.get(service, [])
    if not statements:
        return 0
    with db_module.cursor(commit=True) as cur:
        for stmt in statements:
            cur.execute(stmt)
    return len(statements)
