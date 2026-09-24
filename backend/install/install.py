import psycopg2
import os
import shutil
import sys
from clickhouse_connect import get_client

DB_HOST = "tracker_postgres"
DB_PORT = "5432"
DB_NAME = "db"
DB_USER = "user"
DB_PASSWORD = "password_password_password"

INIT_SQL_FILE = "/app/install/sql/init.sql"


def connect_db():
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )


conn = connect_db()


def check_and_reset_database():
    # return
    try:
        conn.autocommit = True
        cur = conn.cursor()

        # Check for existing tables
        cur.execute("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public';
                    """)
        tables = cur.fetchall()

        if tables:
            print("\n⚠️  Tables found in the database:\n")
            for table in tables:
                print(f" - {table[0]}")
            confirm = input(
                "\n❓ Do you really want to DROP ALL TABLES? Type 'yes' to confirm: ").strip()

            if confirm.lower() == "yes":
                print("\n🗑️ Dropping all tables...")
                for table in tables:
                    cur.execute(f'DROP TABLE IF EXISTS "{table[0]}" CASCADE;')
                print("✅ All tables dropped successfully.\n")
            else:
                print("\n❌ Operation cancelled. The database was left unchanged.\n")
                sys.exit(1)

        else:
            print("ℹ️  No tables in the database. Continuing installation...\n")

        cur.close()
        conn.close()

    except psycopg2.OperationalError as e:
        print(f"\n🚫 Failed to connect to the database: {e}\n")
        sys.exit(1)


def run_install():
    try:
        check_and_reset_database()
        # Connect to the database
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD
        )
        conn.autocommit = True
        cursor = conn.cursor()

        # Read init.sql
        with open(INIT_SQL_FILE, "r", encoding="utf-8") as f:
            sql_commands = f.read()

        # Execute every statement
        cursor.execute(sql_commands)

        print("✅ Postgres database initialized successfully.")

        cursor.close()
        conn.close()

        run_clickhouse_install()

        print('\n✅ Installation complete. ClickHouse database initialized.')

    except Exception as e:
        print(f"❌ Installation error: {e}")


def run_clickhouse_install():
    print("Connecting to ClickHouse...")

    client = get_client(
        host=os.getenv("CLICKHOUSE_HOST", "tracker_clickhouse"),
        username=os.getenv("CLICKHOUSE_USER", "user"),
        password=os.getenv("CLICKHOUSE_PASSWORD", "password_password_password"),
        port=int(os.getenv("CLICKHOUSE_PORT", 8123)),
        secure=False
    )

    sql_file_path = os.path.join(os.path.dirname(__file__), 'sql/clickHouse.sql')
    if not os.path.exists(sql_file_path):
        raise FileNotFoundError(f"SQL file not found: {sql_file_path}")

    with open(sql_file_path, 'r', encoding='utf-8') as f:
        raw_sql = f.read()

    # Split on ; and drop empty statements
    statements = [s.strip() for s in raw_sql.split(';') if s.strip()]

    for statement in statements:
        print(f"\nExecuting:\n{statement}")
        client.command(statement)

    print("\n✅ ClickHouse install complete.")


if __name__ == "__main__":
    run_install()
