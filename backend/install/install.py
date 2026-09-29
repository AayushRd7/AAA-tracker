"""AAA Tracker database initializer.

Idempotent by default: the schema SQL uses IF NOT EXISTS / ON CONFLICT, so running
this on an existing install creates whatever is missing and changes nothing else.

    python3 install.py              # ensure schema (safe to re-run)
    python3 install.py --recreate   # DROP every table first, then create (destroys data)

Exits non-zero on failure so callers (make install) notice.
"""
import argparse
import os
import sys

import psycopg2
from clickhouse_connect import get_client

DB_HOST = os.getenv("POSTGRES_HOST", "tracker_postgres")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "db")
DB_USER = os.getenv("POSTGRES_USER", "user")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "_".join(["password"] * 3))

INIT_SQL_FILE = "/app/install/sql/init.sql"


def connect_db():
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )


def existing_tables(cur):
    cur.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name;
                """)
    return [row[0] for row in cur.fetchall()]


def drop_all_tables(cur, tables):
    print("  ▸ postgres: dropping all tables (--recreate)")
    for table in tables:
        cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE;')
    print("  ▸ postgres: all tables dropped\n")


def run_postgres_install(recreate: bool):
    conn = connect_db()
    conn.autocommit = True
    cur = conn.cursor()

    tables = existing_tables(cur)
    if tables:
        print(f"  ▸ postgres: {len(tables)} existing table(s) found")
        if recreate:
            drop_all_tables(cur, tables)
        else:
            print("    ensuring the schema is complete — nothing is dropped")
            print("    (use --recreate to wipe and start over)\n")
    else:
        print("  ▸ postgres: no tables yet — creating the schema\n")

    with open(INIT_SQL_FILE, "r", encoding="utf-8") as f:
        cur.execute(f.read())

    # A fresh install creates the admin user HERE, after the services have already booted — so
    # the app's startup backfill (which grants every user a workspace membership) may never see
    # it. The result is a login that "succeeds" and then 403s on every request. Ensure tenant 1
    # and a membership for every user that has none, exactly as the app's migration does.
    cur.execute("INSERT INTO tenants (id, name, slug) VALUES (1, 'Default', 'default') "
                "ON CONFLICT DO NOTHING")
    cur.execute("SELECT setval(pg_get_serial_sequence('tenants','id'), "
                "GREATEST((SELECT COALESCE(MAX(id), 1) FROM tenants), 1))")
    cur.execute("""
        INSERT INTO tenant_memberships (user_id, tenant_id, role, permissions)
        SELECT u.id, 1,
               CASE WHEN u.is_admin AND u.id = (SELECT MIN(id) FROM users WHERE is_admin)
                         THEN 'owner'
                    WHEN u.is_admin THEN 'admin'
                    ELSE 'editor' END,
               u.permissions
        FROM users u
        WHERE NOT EXISTS (SELECT 1 FROM tenant_memberships m WHERE m.user_id = u.id)
        ON CONFLICT (user_id, tenant_id) DO NOTHING
    """)
    print("  ▸ memberships: every user belongs to a workspace")

    cur.close()
    conn.close()
    print("  ▸ postgres: schema ready")


def reconcile_columns():
    """Add any column the ORM models declare but the live schema is missing.

    The app's startup migrations cannot repair a database whose tables do not exist
    yet (they run before the installer creates them), and CREATE TABLE IF NOT EXISTS
    never adds columns to a table that already exists. Deriving the expected columns
    from the models keeps every install — fresh, upgraded, or half-initialised —
    consistent with the code.
    """
    try:
        sys.path.insert(0, "/app")
        import importlib
        import pathlib

        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import text as sa_text

        # Import every model module first so Base.metadata is fully populated
        # (Base itself lives in models/base.py; db.py owns the engine).
        for path in sorted(pathlib.Path("/app/models").glob("*.py")):
            if path.stem != "__init__":
                try:
                    importlib.import_module(f"models.{path.stem}")
                except Exception:
                    pass

        from db import engine
        from models.base import Base
    except Exception as e:  # pragma: no cover - diagnostics only
        print(f"  ▸ schema reconcile: skipped ({type(e).__name__}: {e})")
        return

    added = []
    inspector = sa_inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl_type = column.type.compile(engine.dialect)
                parts = [f'"{column.name}" {ddl_type}']

                # Carry over scalar model defaults so the new column is populated
                # (and NOT NULL where the model requires a value).
                literal = None
                default = column.default
                if default is not None and getattr(default, "is_scalar", False):
                    arg = default.arg
                    if isinstance(arg, bool):
                        literal = "true" if arg else "false"
                    elif isinstance(arg, (int, float)):
                        literal = str(arg)
                    elif isinstance(arg, str):
                        literal = "'" + arg.replace("'", "''") + "'"
                if literal is not None:
                    parts.append(f"DEFAULT {literal}")
                    if not column.nullable:
                        parts.append("NOT NULL")

                conn.execute(sa_text(
                    f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS '
                    + " ".join(parts)))
                added.append(f"{table.name}.{column.name}")

    if added:
        print(f"  ▸ schema reconcile: added {len(added)} missing column(s)")
        for item in added[:10]:
            print(f"      {item}")
        if len(added) > 10:
            print(f"      … and {len(added) - 10} more")
    else:
        print("  ▸ schema reconcile: up to date")


def split_sql_statements(raw_sql: str) -> list:
    """Split a SQL file into statements on top-level semicolons.

    A naive ``raw_sql.split(";")`` truncates a statement at any semicolon that really sits
    inside a comment or a quoted literal. That produced a broken CREATE TABLE (unbalanced
    parentheses) on a fresh install while it went unnoticed where the table already existed.
    This walks the text and splits only on a semicolon outside quotes and comments, and drops
    comments so they never reach the server.
    """
    statements, current = [], []
    quote = None            # active quote character, when inside a literal
    line_comment = False
    block_comment = False
    i = 0
    while i < len(raw_sql):
        ch = raw_sql[i]
        nxt = raw_sql[i + 1] if i + 1 < len(raw_sql) else ""

        if line_comment:
            if ch == "\n":
                line_comment = False
            i += 1
            continue
        if block_comment:
            if ch == "*" and nxt == "/":
                block_comment = False
                i += 2
                continue
            i += 1
            continue
        if quote:
            current.append(ch)
            if ch == quote:
                if nxt == quote:           # doubled quote inside a literal
                    current.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue

        if ch == "-" and nxt == "-":
            line_comment = True
            current.append("\n")          # keep the line break, drop the comment text
            i += 2
            continue
        if ch == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1

    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def run_clickhouse_install():
    print("  ▸ clickhouse: connecting")
    client = get_client(
        host=os.getenv("CLICKHOUSE_HOST", "tracker_clickhouse"),
        username=os.getenv("CLICKHOUSE_USER", "user"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "_".join(["password"] * 3)),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        secure=False,
    )

    sql_file_path = os.path.join(os.path.dirname(__file__), "sql/clickHouse.sql")
    if not os.path.exists(sql_file_path):
        raise FileNotFoundError(f"SQL file not found: {sql_file_path}")

    with open(sql_file_path, "r", encoding="utf-8") as f:
        raw_sql = f.read()

    for statement in split_sql_statements(raw_sql):
        client.command(statement)

    print("  ▸ clickhouse: schema ready")


def main():
    parser = argparse.ArgumentParser(description="Initialize the AAA Tracker databases.")
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="drop every table before creating the schema (destroys all tracking data)",
    )
    args = parser.parse_args()

    try:
        run_postgres_install(args.recreate)
        reconcile_columns()
        run_clickhouse_install()
    except psycopg2.OperationalError as e:
        print(f"\n🚫 Failed to connect to PostgreSQL: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Installation error: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n  ✓ databases ready\n")


if __name__ == "__main__":
    main()
