"""Apply schema.sql to the configured database.

Run before the seeder. Split out from seed.py so that "create the tables" and
"put data in them" fail independently and report separately — a grant problem
and a data problem look nothing alike and should not share an error message.

Safe to run on every converge: every statement is CREATE TABLE IF NOT EXISTS.
"""
import os
import sys

from . import config, db

SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def statements(sql_text):
    """Split on semicolons at end of line, ignoring -- comment lines.

    Deliberately simple: this only ever parses our own schema.sql, which has no
    stored procedures, no delimiters and no semicolons inside string literals.
    A general SQL splitter here would be more code and no more correct.
    """
    cleaned = "\n".join(line for line in sql_text.splitlines()
                        if not line.strip().startswith("--"))
    return [s.strip() for s in cleaned.split(";") if s.strip()]


def main():
    with open(SCHEMA, encoding="utf-8") as fh:
        stmts = statements(fh.read())

    conn = db.connect()
    try:
        with conn.cursor() as cur:
            for stmt in stmts:
                cur.execute(stmt)
            cur.execute(
                "SELECT COUNT(*) AS n FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name IN "
                "('users', 'tickets', 'comments')", (config.DB_NAME,))
            n = cur.fetchone()["n"]
        conn.commit()
    finally:
        conn.close()

    if n != 3:
        print("ERROR: expected 3 tables in %s, found %d" % (config.DB_NAME, n))
        return 1
    print("schema ready: 3/3 tables present in %s on %s"
          % (config.DB_NAME, config.DB_HOST))
    return 0


if __name__ == "__main__":
    sys.exit(main())
