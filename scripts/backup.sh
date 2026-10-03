#!/usr/bin/env bash
#
# Back up Postgres and ClickHouse into a timestamped directory.
#
#   ./scripts/backup.sh
#
# Layout under ${BACKUP_DIR:-./backups}/<UTC-timestamp>/ :
#   postgres.dump           pg_dump -Fc (custom format, restorable with pg_restore)
#   clickhouse/<name>.zip   ClickHouse BACKUP DATABASE ... TO File(...)
#   manifest.txt            versions, sizes and the app git SHA
#
# Env:
#   BACKUP_DIR         output root        (default ./backups)
#   BACKUP_KEEP        runs to retain     (default 7, older runs pruned)
#   BACKUP_S3_BUCKET   optional s3://…     (uploaded with `aws s3 cp` if the CLI exists)
#
# The ClickHouse backup disk `backups` is NOT configured in this stack, so the
# `Disk()` engine cannot be used (it errors with "backups.allowed_disk is not
# set"). We therefore use the File engine, writing a zip inside the container and
# copying it out. This is the documented fallback from the deployment notes.
#
# The INTEGRATIONS_ENCRYPTION_KEY is deliberately NOT exported here: store it
# out-of-band (see `make show-integration-key`). Without it, every encrypted
# ad-platform token in the restored database is unreadable.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

BACKUP_DIR="${BACKUP_DIR:-./backups}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
BACKUP_S3_BUCKET="${BACKUP_S3_BUCKET:-}"

PG_CONTAINER="${POSTGRES_CONTAINER:-tracker_postgres}"
CH_CONTAINER="${CLICKHOUSE_CONTAINER:-tracker_clickhouse}"
CH_BACKUP_DIR="/var/lib/clickhouse/backups"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_DIR%/}/${STAMP}"

warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
info() { printf '\033[36m▸\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Load .env so POSTGRES_USER/POSTGRES_DB/CLICKHOUSE_DB/... are available to the
# host-side command below. Values are overridable from the real environment.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env 2>/dev/null || true
    set +a
fi

container_running() {
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" = "true" ]
}

env_from_container() {
    # $1 container, $2 var name — best-effort read of the container's env_file
    docker exec "$1" printenv "$2" 2>/dev/null || true
}

# ── preflight ───────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "docker is not installed"
container_running "$PG_CONTAINER" || die "container $PG_CONTAINER is not running — start the stack first"
container_running "$CH_CONTAINER" || die "container $CH_CONTAINER is not running — start the stack first"

POSTGRES_USER="${POSTGRES_USER:-$(env_from_container "$PG_CONTAINER" POSTGRES_USER)}"
POSTGRES_DB="${POSTGRES_DB:-$(env_from_container "$PG_CONTAINER" POSTGRES_DB)}"
CLICKHOUSE_DB="${CLICKHOUSE_DB:-$(env_from_container "$CH_CONTAINER" CLICKHOUSE_DB)}"
CLICKHOUSE_DB="${CLICKHOUSE_DB:-default}"

[ -n "$POSTGRES_USER" ] || die "POSTGRES_USER is not set (not in .env and not in $PG_CONTAINER)"
[ -n "$POSTGRES_DB" ]   || die "POSTGRES_DB is not set (not in .env and not in $PG_CONTAINER)"

mkdir -p "$DEST"
info "target: $DEST"

# ── Postgres ────────────────────────────────────────────────────────────────
info "postgres: dumping $POSTGRES_DB as $POSTGRES_USER"
docker exec "$PG_CONTAINER" pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "$DEST/postgres.dump"
[ -s "$DEST/postgres.dump" ] || die "pg_dump produced an empty file"
PG_BYTES="$(wc -c < "$DEST/postgres.dump" | tr -d ' ')"
PG_VERSION="$(docker exec "$PG_CONTAINER" pg_dump --version 2>/dev/null | tr -d '\r' || echo unknown)"

# ── ClickHouse ──────────────────────────────────────────────────────────────
CH_OK=0
CH_NAME="clickhouse-${STAMP}.zip"
CH_BYTES=""
CH_VERSION="$(docker exec "$CH_CONTAINER" clickhouse-client --query 'SELECT version()' 2>/dev/null | tr -d '\r' || echo unknown)"

info "clickhouse: backing up database '$CLICKHOUSE_DB' to a File archive"
if docker exec "$CH_CONTAINER" mkdir -p "$CH_BACKUP_DIR" \
   && docker exec "$CH_CONTAINER" chown clickhouse:clickhouse "$CH_BACKUP_DIR" 2>/dev/null; then
    # File engine in this image: exactly one argument, the full path (a .zip).
    if docker exec "$CH_CONTAINER" clickhouse-client \
           --query "BACKUP DATABASE \`${CLICKHOUSE_DB}\` TO File('${CH_BACKUP_DIR}/${CH_NAME}')"; then
        mkdir -p "$DEST/clickhouse"
        docker cp "$CH_CONTAINER:${CH_BACKUP_DIR}/${CH_NAME}" "$DEST/clickhouse/${CH_NAME}"
        docker exec "$CH_CONTAINER" rm -f "${CH_BACKUP_DIR}/${CH_NAME}" >/dev/null 2>&1 || true
        CH_BYTES="$(wc -c < "$DEST/clickhouse/${CH_NAME}" | tr -d ' ')"
        CH_OK=1
    fi
fi
if [ "$CH_OK" -ne 1 ]; then
    warn "ClickHouse backup FAILED — ${DEST} contains Postgres only."
    warn "The 'backups' disk is not configured; inspect the server error above."
    warn "Postgres restore still works; ClickHouse clicks are NOT in this run."
fi

# ── manifest ────────────────────────────────────────────────────────────────
GIT_SHA="$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
{
    printf 'backup_timestamp_utc=%s\n' "$STAMP"
    printf 'generated_by=scripts/backup.sh\n'
    printf 'hostname=%s\n' "$(hostname 2>/dev/null || echo unknown)"
    printf 'app_git_sha=%s\n' "$GIT_SHA"
    printf 'postgres_container=%s\n' "$PG_CONTAINER"
    printf 'postgres_version=%s\n' "$PG_VERSION"
    printf 'postgres_user=%s\n' "$POSTGRES_USER"
    printf 'postgres_db=%s\n' "$POSTGRES_DB"
    printf 'postgres_dump=postgres.dump\n'
    printf 'postgres_dump_bytes=%s\n' "$PG_BYTES"
    printf 'clickhouse_container=%s\n' "$CH_CONTAINER"
    printf 'clickhouse_version=%s\n' "$CH_VERSION"
    printf 'clickhouse_db=%s\n' "$CLICKHOUSE_DB"
    if [ "$CH_OK" -eq 1 ]; then
        printf 'clickhouse_backup=clickhouse/%s\n' "$CH_NAME"
        printf 'clickhouse_backup_bytes=%s\n' "$CH_BYTES"
    else
        printf 'clickhouse_backup=FAILED\n'
    fi
    printf 'integrations_encryption_key=NOT INCLUDED (escrow separately: make show-integration-key)\n'
} > "$DEST/manifest.txt"

# ── optional off-box copy ───────────────────────────────────────────────────
if [ -n "$BACKUP_S3_BUCKET" ]; then
    if command -v aws >/dev/null 2>&1; then
        info "s3: copying to s3://${BACKUP_S3_BUCKET}/${STAMP}/"
        aws s3 cp "$DEST" "s3://${BACKUP_S3_BUCKET}/${STAMP}/" --recursive
    else
        warn "BACKUP_S3_BUCKET is set but the AWS CLI ('aws') is not installed — skipping off-box copy"
    fi
fi

# ── prune older runs ────────────────────────────────────────────────────────
if [ "$BACKUP_KEEP" -gt 0 ] 2>/dev/null; then
    # Newest first; skip the first $BACKUP_KEEP; remove the remainder.
    find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20*T*Z' -print 2>/dev/null \
        | sort -r \
        | tail -n +$((BACKUP_KEEP + 1)) \
        | while read -r old; do rm -rf "$old"; done
fi

printf '\n\033[32m✓ backup complete\033[0m %s\n\n' "$DEST"
ls -la "$DEST"
[ -d "$DEST/clickhouse" ] && ls -la "$DEST/clickhouse"
printf '\n--- manifest.txt ---\n'
cat "$DEST/manifest.txt"
