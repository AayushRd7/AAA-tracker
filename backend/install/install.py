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
    print("\n🗑️  Dropping all tables (--recreate)...")
    for table in tables:
        cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE;')
    print("✅ All tables dropped.\n")


def run_postgres_install(recreate: bool):
    conn = connect_db()
    conn.autocommit = True
    cur = conn.cursor()

    tables = existing_tables(cur)
    if tables:
        print(f"\nℹ️  Found {len(tables)} existing table(s): {', '.join(tables)}")
        if recreate:
            drop_all_tables(cur, tables)
        else:
            print("   Ensuring the schema is complete — nothing is dropped.")
            print("   (use --recreate to wipe and start over)\n")
    else:
        print("ℹ️  No tables yet — creating the schema...\n")

    with open(INIT_SQL_FILE, "r", encoding="utf-8") as f:
        cur.execute(f.read())

    cur.close()
    conn.close()
    print("✅ PostgreSQL schema ready.")


def run_clickhouse_install():
    print("Connecting to ClickHouse...")
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

    statements = [s.strip() for s in raw_sql.split(";") if s.strip()]
    for statement in statements:
        client.command(statement)

    print("✅ ClickHouse schema ready.")


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
        run_clickhouse_install()
    except psycopg2.OperationalError as e:
        print(f"\n🚫 Failed to connect to PostgreSQL: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Installation error: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n✅ Installation complete — PostgreSQL and ClickHouse are ready.")


if __name__ == "__main__":
    main()
