"""MariaDB access over PyMySQL.

Deliberately small: connect per request, DictCursor, autocommit off with an
explicit commit on writes. No ORM and no connection pool.

WHY NO POOL. In this lab the interesting failure is a NETWORK failure -- the
app host cannot reach the database host because of bind-address, a grant, or a
security group. A pool hides that behind a cached socket and makes the first
symptom appear minutes later. Connecting per request means a broken path shows
up on the very next page load, which is what you want when you are teaching.
For anything real, pool.
"""

import contextlib
import logging
import time

import pymysql
import pymysql.cursors

from . import config

log = logging.getLogger("zmsapp.db")


class DatabaseUnavailable(RuntimeError):
    """Raised when the database cannot be reached or authenticated."""


def connect(db_name=None, connect_timeout=4, retries=1):
    """Open a connection to this service's database host.

    `db_name=None` selects config.DB_NAME. Pass an empty string to connect to
    the server without selecting a schema (used by the schema bootstrap).
    """
    name = config.DB_NAME if db_name is None else db_name
    last = None
    for attempt in range(retries + 1):
        try:
            return pymysql.connect(
                host=config.DB_HOST,
                port=config.DB_PORT,
                user=config.DB_USER,
                password=config.DB_PASSWORD,
                database=name or None,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=connect_timeout,
                read_timeout=10,
                write_timeout=10,
                autocommit=False,
            )
        except Exception as exc:  # pymysql raises several distinct types
            last = exc
            if attempt < retries:
                time.sleep(0.5)
    raise DatabaseUnavailable(
        "cannot reach {0}:{1}/{2} as {3}: {4}".format(
            config.DB_HOST, config.DB_PORT, name, config.DB_USER, last
        )
    )


@contextlib.contextmanager
def cursor(db_name=None, commit=False):
    """Context manager yielding a DictCursor, closing the connection after."""
    conn = connect(db_name=db_name)
    try:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
    finally:
        conn.close()


def query(sql, params=None):
    """Run a SELECT and return a list of dicts."""
    with cursor() as cur:
        cur.execute(sql, params or ())
        return list(cur.fetchall())


def query_one(sql, params=None):
    """Run a SELECT and return the first row, or None."""
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    """Run one INSERT/UPDATE/DELETE and return (rowcount, lastrowid)."""
    with cursor(commit=True) as cur:
        cur.execute(sql, params or ())
        return cur.rowcount, cur.lastrowid


def health():
    """Cheap liveness probe used by /health. Never raises."""
    started = time.time()
    try:
        with cursor() as cur:
            cur.execute("SELECT VERSION() AS v")
            row = cur.fetchone()
        return {
            "status": "ok",
            "server_version": row["v"],
            "latency_ms": round((time.time() - started) * 1000, 1),
            "host": config.DB_HOST,
            "schema": config.DB_NAME,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc)[:300],
            "host": config.DB_HOST,
            "schema": config.DB_NAME,
        }
