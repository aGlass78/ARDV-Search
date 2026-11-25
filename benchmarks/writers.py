"""Production-faithful write loops: v2 (UPSERT) and legacy v1.

Both loops use identical transaction sizing (batch commit every 25, the
crawler's DB_COMMIT_BATCH) and the same SQLite settings (WAL,
synchronous=FULL, busy_timeout=5000) — both constructors set them.

The legacy v1 writer replicates the pre-v2 production write path
(per-page DELETE + INSERT into the FTS5 table). It exists ONLY as a
benchmark comparison; it is not a production code path.
"""

import main

UPSERT = (
    "INSERT INTO pages (url, title, description, body) VALUES "
    "(?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET "
    "title=excluded.title, description=excluded.description, "
    "body=excluded.body"
)

LEGACY_DELETE = "DELETE FROM pages WHERE url = ?"
LEGACY_INSERT = (
    "INSERT INTO pages (url, title, description, body) "
    "VALUES (?, ?, ?, ?)"
)


def v2_fresh_db(db_path):
    """Open a fresh v2 database via the production choke point."""
    return main.open_database(db_path)


def legacy_v1_db(db_path):
    """Open a v1 database via the production v1 constructor."""
    return main.get_database(db_path)


def insert_rows(conn, rows, batch=25):
    """v2 production write pattern (UPSERT, batched commits)."""
    pending = 0
    commits = 0
    for row in rows:
        conn.execute(UPSERT, row)
        pending += 1
        if pending >= batch:
            conn.commit()
            commits += 1
            pending = 0
    if pending:
        conn.commit()
        commits += 1
    return {"rows": len(rows), "commits": commits}


def legacy_write_rows(conn, rows, batch=25):
    """Legacy v1 write pattern (DELETE + INSERT, batched commits)."""
    pending = 0
    commits = 0
    for row in rows:
        conn.execute(LEGACY_DELETE, (row[0],))
        conn.execute(LEGACY_INSERT, row)
        pending += 1
        if pending >= batch:
            conn.commit()
            commits += 1
            pending = 0
    if pending:
        conn.commit()
        commits += 1
    return {"rows": len(rows), "commits": commits}
