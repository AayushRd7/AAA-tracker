#!/usr/bin/env bash
#
# Restore a backup produced by scripts/backup.sh into this stack.
#
#   ./scripts/restore.sh [BACKUP_DIR]        # newest run when omitted
#   FORCE=1 ./scripts/restore.sh backups/<ts>  # allow restoring over a non-empty DB
#
# !! THIS OVERWRITES DATA. It is meant for a fresh/empty stack (or after
#    `make reset && make install` recreated the schema). It refuses to touch a
#    database that already holds tables unless FORCE=1 is set.
#
# Restores:
#   postgres.dump          via `pg_restore --clean --if-exists`
#   clickhouse/*.zip       via `RESTORE DATABASE ... FROM File(...)`
#
# Reminder: the INTEGRATIONS_ENCRYPTION_KEY is not in the backup. Restore it to
# the same value first (`make show-integration-key`), or encrypted platform
# tokens in the database cannot be decrypted.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

FORCE="${FORCE:-0}"

PG_CONTAINER="${POSTGRES_CONTAINER:-tracker_postgres}"
CH_CONTAINER="${CLICKHOUSE_CONTAINER:-tracker_clickhouse}"
CH_BACKUP_DIR="/var/lib/clickhouse/backups"

warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
info() { printf '\033[36m▸\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Resolve the backup directory: explicit argument, else newest run under ./backups.
DIR="${1:-}"
if [ -z "$DIR" ]; then
    # Newest first; consume the whole listing so no producer gets SIGPIPE under pipefail.
    _dirs="$(find "${BACKUP_DIR:-./backups}" -mindepth 1 -maxdepth 1 -type d -name '20*T*Z' -print 2>/dev/null | sort -r || true)"
    DIR="${_dirs%%$'\n'*}"
fi
[ -n "$DIR" ] && [ -d "$DIR" ] || die "no backup directory found (pass one explicitly: ./scripts/restore.sh backups/<timestamp>)"
DIR="${DIR%/}"

# ── preflight ───────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "docker is not installed"
[ -f "$DIR/postgres.dump" ] || die "$DIR/postgres.dump is missing"

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env 2>/dev/null || true
    set +a
fi

container_running() {
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" = "true" ]
}
env_from_container() { docker exec "$1" printenv "$2" 2>/dev/null || true; }

container_running "$PG_CONTAINER" || die "container $PG_CONTAINER is not running"
container_running "$CH_CONTAINER" || die "container $CH_CONTAINER is not running"

POSTGRES_USER="${POSTGRES_USER:-$(env_from_container "$PG_CONTAINER" POSTGRES_USER)}"
POSTGRES_DB="${POSTGRES_DB:-$(env_from_container "$PG_CONTAINER" POSTGRES_DB)}"
CLICKHOUSE_DB="${CLICKHOUSE_DB:-$(env_from_container "$CH_CONTAINER" CLICKHOUSE_DB)}"
CLICKHOUSE_DB="${CLICKHOUSE_DB:-default}"
[ -n "$POSTGRES_USER" ] && [ -n "$POSTGRES_DB" ] || die "POSTGRES_USER/POSTGRES_DB are not set"

# ── loud warning ────────────────────────────────────────────────────────────
printf '\n\033[33m\033[1m!! DESTRUCTIVE RESTORE !! It OVERWRITES the data in %s / ClickHouse db %s\033[0m\n' "$PG_CONTAINER" "$CLICKHOUSE_DB"
printf '   source: %s\n\n' "$DIR"

# ── emptiness gate ──────────────────────────────────────────────────────────
pg_tables="$(docker exec "$PG_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')" 2>/dev/null | tr -d '[:space:]')"
ch_tables="$(docker exec "$CH_CONTAINER" clickhouse-client --query \
    "SELECT count() FROM system.tables WHERE database = '${CLICKHOUSE_DB}' AND database NOT IN ('system')" 2>/dev/null | tr -d '[:space:]')"
pg_tables="${pg_tables:-0}"
ch_tables="${ch_tables:-0}"

if [ "$pg_tables" -gt 0 ] && [ "$FORCE" != "1" ]; then
    die "Postgres already holds ${pg_tables} tables — refusing. Re-run with FORCE=1 if you really mean to overwrite."
fi
if [ "$ch_tables" -gt 0 ] && [ "$FORCE" != "1" ]; then
    die "ClickHouse db '${CLICKHOUSE_DB}' already holds ${ch_tables} tables — refusing. Re-run with FORCE=1 if you really mean to overwrite."
fi
[ "$FORCE" = "1" ] && warn "FORCE=1 — existing tables will be overwritten"

# ── restore Postgres ────────────────────────────────────────────────────────
info "postgres: restoring ${POSTGRES_DB} (pg_restore --clean --if-exists)"
docker exec -i "$PG_CONTAINER" pg_restore --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$DIR/postgres.dump"
info "postgres: restored"

# ── restore ClickHouse ──────────────────────────────────────────────────────
CH_ZIPS="$(find "$DIR/clickhouse" -maxdepth 1 -type f -name '*.zip' -print 2>/dev/null | sort || true)"
CH_ZIP="${CH_ZIPS%%$'\n'*}"
if [ -n "$CH_ZIP" ]; then
    CH_REMOTE="${CH_BACKUP_DIR}/$(basename "$CH_ZIP")"
    info "clickhouse: restoring ${CLICKHOUSE_DB} from $(basename "$CH_ZIP")"
    docker exec "$CH_CONTAINER" mkdir -p "$CH_BACKUP_DIR"
    docker cp "$CH_ZIP" "$CH_CONTAINER:$CH_REMOTE"
    docker exec "$CH_CONTAINER" chown clickhouse:clickhouse "$CH_REMOTE" 2>/dev/null || true
    if [ "$FORCE" = "1" ]; then
        # Existing tables: permit the restore to write into a non-empty database.
        docker exec "$CH_CONTAINER" clickhouse-client \
            --query "RESTORE DATABASE \`${CLICKHOUSE_DB}\` FROM File('${CH_REMOTE}') SETTINGS allow_non_empty_tables = 1"
    else
        docker exec "$CH_CONTAINER" clickhouse-client \
            --query "RESTORE DATABASE \`${CLICKHOUSE_DB}\` FROM File('${CH_REMOTE}')"
    fi
    docker exec "$CH_CONTAINER" rm -f "$CH_REMOTE" >/dev/null 2>&1 || true
    info "clickhouse: restored"
else
    warn "no clickhouse/*.zip in ${DIR} — skipping ClickHouse restore (Postgres was restored)"
fi

printf '\n\033[32m✓ restore complete\033[0m from %s\n' "$DIR"
printf '  restart the app so it reconnects: make restart\n\n'
