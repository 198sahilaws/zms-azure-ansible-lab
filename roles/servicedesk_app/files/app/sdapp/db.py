"""Database access.

MIDDLEWARE TIER ONLY. This module is imported by sdapp.api and by nothing else;
sdapp.web reaches the data through sdapp.client over HTTP. The file is present
on the web host because the payload is byte-identical across both tiers, but
importing it there would fail at the first connect() — that host has no
SD_DB_PASSWORD in its environment at all.

A connection per request, not a pool. In a lab that is the POINT: every request
opens a real TCP connection from the middleware host to the database host, so
the flow logs show connection churn rather than one long-lived socket that a
microsegmentation agent sees once and never again.
"""
import contextlib

import pymysql
from pymysql.cursors import DictCursor

from . import config


def connect(db_name=None):
    """Open a connection. db_name=None connects with no database selected,
    which is what the schema bootstrap needs before the database exists."""
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=db_name if db_name is not None else config.DB_NAME,
        charset="utf8mb4",
        cursorclass=DictCursor,
        connect_timeout=config.DB_CONNECT_TIMEOUT,
        autocommit=False,
    )


@contextlib.contextmanager
def cursor(commit=False):
    conn = connect()
    try:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
    finally:
        conn.close()


def query(sql, args=None):
    with cursor() as cur:
        cur.execute(sql, args or ())
        return cur.fetchall()


def query_one(sql, args=None):
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql, args=None):
    """Run one statement and commit. Returns lastrowid."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            rowid = cur.lastrowid
        conn.commit()
        return rowid
    finally:
        conn.close()


def health():
    """Return (ok, detail). Never raises — /health must answer even when the
    database is gone, because 'the database is gone' is the answer."""
    try:
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                cur.fetchone()
                cur.execute("SELECT COUNT(*) AS n FROM tickets")
                n = cur.fetchone()["n"]
            return True, {"reachable": True, "host": config.DB_HOST,
                          "database": config.DB_NAME, "tickets": n}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - any failure is a health failure
        return False, {"reachable": False, "host": config.DB_HOST,
                       "database": config.DB_NAME,
                       "error": "%s: %s" % (type(exc).__name__, exc)}
