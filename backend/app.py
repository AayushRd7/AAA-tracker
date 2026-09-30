from fastapi import FastAPI, Request, Depends
from auth import (require_api_auth, require_section, require_section_write,
                  require_tenant_feature, resolve_api_token, resolve_session_tenant)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from fastapi.responses import FileResponse
from clickHouse import get_clickhouse_client, ensure_report_dimensions_schema
from starlette.concurrency import run_in_threadpool
from tenant_context import (set_api_token_tenant, set_current_tenant, set_tenant_missing,
                            reset_api_token_tenant, reset_current_tenant)
from types import SimpleNamespace

app = FastAPI(
    title="AAA Tracker API",
    description="API Documentation",
    version="1.0.0",
    docs_url="/api_docs",        # Swagger UI
    redoc_url="/api_redoc",      # ReDoc (alternative docs UI)
    openapi_url="/api_openapi.json",  # OpenAPI schema)
    root_path="/backend"  # when the server sits behind a proxy at /backend
)
app.state = SimpleNamespace()

# Self-diagnosis for a wedged worker: register a stack-dump signal so a stalled
# process can be inspected without a debugger attached (containers rarely allow
# ptrace). `docker exec tracker_backend kill -USR1 <worker-pid>` writes every
# thread's stack to stderr, which is the container log.
import faulthandler as _faulthandler
import signal as _signal
try:
    _faulthandler.register(_signal.SIGUSR1, chain=False)
except Exception:  # pragma: no cover - diagnostics only
    pass


def _session_token_from_scope(scope) -> str:
    """The session cookie value from an ASGI scope ('' when absent)."""
    for name, value in scope.get("headers") or []:
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                key, _, val = part.strip().partition("=")
                if key == "session_token":
                    return val
    return ""


def _bearer_from_scope(scope) -> str:
    """The Bearer API token from an ASGI scope ('' when absent)."""
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            raw = value.decode("latin-1").strip()
            if raw.lower().startswith("bearer "):
                return raw.split(" ", 1)[1].strip()
    return ""


class TenantContextMiddleware:
    """Resolve the request's tenant once, before anything else runs.

    The resolved tenant lives in a contextvar (tenant_context.py) that the
    ORM session hooks, the ClickHouse helpers and the audit writer read.

    Deliberately a plain ASGI middleware rather than Starlette's
    BaseHTTPMiddleware: the inner app is awaited in the same task, so the
    contextvar set here is guaranteed to be visible to the endpoint — both for
    async endpoints and for sync ones (anyio's threadpool copies the context).

    Resolution is session-based, plus a Bearer API token (which resolves to the
    workspace that owns it). A tenant id from a query parameter or header is
    never accepted, so there is no spoofing surface.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        tenant_id, has_membership, token_tenant = None, True, None
        try:
            bearer = _bearer_from_scope(scope)
            if bearer:
                token_tenant = await run_in_threadpool(resolve_api_token, bearer)
            if token_tenant is not None:
                # A valid API token wins over any cookie: the request runs in
                # the workspace the token belongs to, nowhere else.
                tenant_id, has_membership = token_tenant, True
            else:
                tenant_id, has_membership = await run_in_threadpool(
                    resolve_session_tenant, _session_token_from_scope(scope))
        except Exception:
            pass
        token = set_current_tenant(tenant_id)
        api_token = set_api_token_tenant(token_tenant)
        set_tenant_missing(not has_membership)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_tenant(token)
            reset_api_token_tenant(api_token)


app.add_middleware(TenantContextMiddleware)


def _ensure_capi_tables(conn):
    """Idempotent CAPI schema (pixels, bindings, channel toggles)."""
    from sqlalchemy import text
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS capi_pixels (
            id SERIAL PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            platform VARCHAR(32) NOT NULL DEFAULT 'meta',
            pixel_id VARCHAR(255) NOT NULL DEFAULT '',
            access_token TEXT,
            default_event_name VARCHAR(100) NOT NULL DEFAULT 'Purchase',
            event_url TEXT,
            action_source VARCHAR(64) NOT NULL DEFAULT 'website',
            data_quality_token TEXT,
            custom_matching BOOLEAN NOT NULL DEFAULT false,
            conversion_matching JSONB NOT NULL DEFAULT '[]'::jsonb,
            payout_customisations JSONB NOT NULL DEFAULT '[]'::jsonb,
            status VARCHAR(16) NOT NULL DEFAULT 'active',
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            updated_at TIMESTAMP NOT NULL DEFAULT now()
        )"""))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS capi_pixel_bindings (
            id SERIAL PRIMARY KEY,
            pixel_id INTEGER NOT NULL REFERENCES capi_pixels(id) ON DELETE CASCADE,
            scope VARCHAR(16) NOT NULL,
            scope_id INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            UNIQUE (pixel_id, scope, scope_id)
        )"""))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS capi_channel_settings (
            source_id INTEGER PRIMARY KEY,
            active BOOLEAN NOT NULL DEFAULT true,
            impression_cost_sync BOOLEAN NOT NULL DEFAULT false,
            updated_at TIMESTAMP NOT NULL DEFAULT now()
        )"""))


def _ensure_logs_tables(conn):
    """Idempotent Logs-area schema (audit trails for the Logs section).

    The tracking plane (frontend/app.py) creates the same tables on its own
    startup so either service can boot first; writes happen there off the
    request path and cost_update_logs is written by backend/app_pages/costs.py.
    """
    from sqlalchemy import text
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS postback_logs (
            id BIGSERIAL PRIMARY KEY,
            received_at TIMESTAMP NOT NULL DEFAULT now(),
            click_id VARCHAR(100),
            status VARCHAR(50),
            payout DOUBLE PRECISION,
            transaction_id VARCHAR(100),
            source_ip TEXT,
            url TEXT,
            result VARCHAR(32) NOT NULL,
            reason TEXT,
            raw JSONB NOT NULL DEFAULT '{}'::jsonb
        )"""))
    conn.execute(text("CREATE INDEX IF NOT EXISTS postback_logs_received_at_idx "
                      "ON postback_logs (received_at)"))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS click_forward_logs (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            click_id VARCHAR(100),
            campaign_id INTEGER,
            flow_index INTEGER,
            schema VARCHAR(32),
            offer_id INTEGER,
            destination_url TEXT,
            status VARCHAR(32) NOT NULL,
            reason TEXT,
            ip TEXT,
            user_agent TEXT
        )"""))
    conn.execute(text("CREATE INDEX IF NOT EXISTS click_forward_logs_created_at_idx "
                      "ON click_forward_logs (created_at)"))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS cost_update_logs (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            username VARCHAR(255),
            campaign_id INTEGER,
            date_from DATE,
            date_to DATE,
            cost DOUBLE PRECISION,
            updated_rows INTEGER
        )"""))
    # The outbound CAPI delivery trail predates the Logs area; create it if the
    # tracking plane has not yet, then extend it in place rather than adding a
    # parallel table.
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS meta_capi_log (
            id BIGSERIAL PRIMARY KEY,
            at TIMESTAMP NOT NULL DEFAULT now(),
            click_id VARCHAR(100),
            status VARCHAR(50),
            event_name VARCHAR(100),
            dataset_id VARCHAR(100),
            outcome VARCHAR(32),
            attempt INTEGER DEFAULT 1,
            response_status INTEGER,
            detail TEXT,
            platform VARCHAR(32),
            pixel_id VARCHAR(100),
            http_status INTEGER,
            response_snippet TEXT,
            attempts INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT now()
        )"""))
    for ddl in (
        "ALTER TABLE meta_capi_log ADD COLUMN IF NOT EXISTS platform VARCHAR(32)",
        "ALTER TABLE meta_capi_log ADD COLUMN IF NOT EXISTS pixel_id VARCHAR(100)",
        "ALTER TABLE meta_capi_log ADD COLUMN IF NOT EXISTS http_status INTEGER",
        "ALTER TABLE meta_capi_log ADD COLUMN IF NOT EXISTS response_snippet TEXT",
        "ALTER TABLE meta_capi_log ADD COLUMN IF NOT EXISTS attempts INTEGER",
        "ALTER TABLE meta_capi_log ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT now()",
    ):
        conn.execute(text(ddl))


def _ensure_wave18_tables(conn):
    """Idempotent schema for saved library entities (filter presets, scripts,
    funnel templates). The frontend tracking plane does not touch these tables,
    so creating them here is sufficient — either service boot order works.
    """
    from sqlalchemy import text
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS filter_presets (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            scope VARCHAR(64) NOT NULL,
            filters JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_by VARCHAR(255),
            created_at TIMESTAMP NOT NULL DEFAULT now()
        )"""))
    conn.execute(text("CREATE INDEX IF NOT EXISTS filter_presets_scope_idx "
                      "ON filter_presets (scope)"))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS scripts (
            id SERIAL PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            code TEXT NOT NULL DEFAULT '',
            description TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT now()
        )"""))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS funnel_templates (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            steps JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMP NOT NULL DEFAULT now()
        )"""))


def _ensure_meta_ads_tables(conn):
    """Idempotent Meta Ads cost-sync schema (raw daily platform audit trail).

    One row per platform + ad account + platform campaign + day; the UNIQUE
    key backs the upsert in app_pages/meta_ads.py, so re-syncing a day replaces
    its totals instead of duplicating them. This is the source of truth for
    what the platform reported; matched_campaign_id records the mapping (NULL
    for unmatched rows).
    """
    from sqlalchemy import text
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS ad_cost_daily (
            id BIGSERIAL PRIMARY KEY,
            platform VARCHAR(32) NOT NULL DEFAULT 'meta',
            ad_account_id VARCHAR(64) NOT NULL,
            platform_campaign_id VARCHAR(64) NOT NULL DEFAULT '',
            campaign_name VARCHAR(255) NOT NULL DEFAULT '',
            date DATE NOT NULL,
            spend DOUBLE PRECISION NOT NULL DEFAULT 0,
            impressions BIGINT NOT NULL DEFAULT 0,
            clicks BIGINT NOT NULL DEFAULT 0,
            matched_campaign_id INTEGER,
            synced_at TIMESTAMP NOT NULL DEFAULT now(),
            UNIQUE (platform, ad_account_id, platform_campaign_id, date)
        )"""))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ad_cost_daily_date_idx "
                      "ON ad_cost_daily (date)"))


def _ensure_integrations_tables(conn):
    """Idempotent ad-platform OAuth schema.

    ``integration_connections`` holds one stored token per platform (the
    UNIQUE platform key backs the upsert in app_pages/integrations.py);
    ``oauth_states`` is the single-use CSRF state with a 10-minute TTL, deleted
    on consumption. Scope-creep note: business-owned pixel listing needs the
    ``business_management`` scope, which the Connect flow deliberately does not
    request.
    """
    from sqlalchemy import text
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS integration_connections (
            id BIGSERIAL PRIMARY KEY,
            platform VARCHAR(32) NOT NULL UNIQUE,
            access_token TEXT,
            token_type VARCHAR(32) NOT NULL DEFAULT 'bearer',
            expires_at TIMESTAMP,
            scopes TEXT NOT NULL DEFAULT '',
            account_label VARCHAR(255) NOT NULL DEFAULT '',
            raw JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            updated_at TIMESTAMP NOT NULL DEFAULT now()
        )"""))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY,
            platform VARCHAR(32) NOT NULL,
            username VARCHAR(255) NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT now()
        )"""))
    conn.execute(text("CREATE INDEX IF NOT EXISTS oauth_states_created_at_idx "
                      "ON oauth_states (created_at)"))


# Registered as the app's startup hook. It was defined but never wired, so every
# backend-side migration below (user security columns, archived flags, ownership, monitor
# state, auto rules, domain groups, CAPI records, log trails, tooling tables) silently
# never ran — the tracking plane's own ensure_schema was covering for it. That is the
# "column does not exist" failure mode seen on a fresh server install.
def _mig(conn, sql):
    """Run one startup migration statement. A failure is reported and skipped so it can
    never abort the migrations that follow it (the previous shape wrapped the whole list
    in one try/except, so the first error silently skipped everything after it)."""
    from sqlalchemy import text
    try:
        conn.execute(text(sql))
    except Exception as e:
        print("startup migration skipped:", e)


# Tenant-owned tables. Kept as one list so the migration loop and any future
# audit agree on the set; the same array lives in install/sql/init.sql.
TENANT_TABLES = [
    "campaigns", "domains", "landings", "affiliate_networks", "offers", "sources",
    "conversions_data", "audit_log", "capi_pixels", "capi_pixel_bindings",
    "capi_channel_settings", "capi_pixel_sent", "meta_capi_sent", "meta_capi_log",
    "ad_cost_daily", "integration_connections", "scripts", "filter_presets",
    "funnel_templates", "domain_groups", "auto_rules", "monitor_state",
    "honeypot_hits", "postback_logs", "click_forward_logs", "cost_update_logs",
    "settings",
]

# {table: [(legacy global unique constraint, composite columns)]}
TENANT_UNIQUE_REWRITES = {
    "campaigns": [("campaigns_name_key", "name"), ("campaigns_alias_key", "alias")],
    "domains": [("domains_domain_key", "domain")],
    "settings": [("settings_name_key", "name")],
    "sources": [("sources_name_key", "name")],
    "affiliate_networks": [("affiliate_networks_name_key", "name")],
    "offers": [("offers_name_key", "name")],
    "landings": [("landings_folder_key", "folder"), ("landings_name_key", "name")],
    "domain_groups": [("domain_groups_name_key", "name")],
    "integration_connections": [("integration_connections_platform_key", "platform")],
    "ad_cost_daily": [("ad_cost_daily_platform_ad_account_id_platform_campaign_id_d_key",
                       "platform, ad_account_id, platform_campaign_id, date")],
}


def _ensure_tenant_schema(conn):
    """Idempotent multi-tenancy schema + backfill (phase 1).

    Orders the work so it is safe on a live install and on a fresh one:
    tenants/tenant_memberships -> tenant_id on every tenant-owned table
    (add nullable, backfill to 1, then NOT NULL DEFAULT 1) -> composite uniques
    (drop the global ones, add UNIQUE(tenant_id, <col>)) -> per-tenant indexes
    -> membership backfill.

    Every ALTER is guarded by to_regclass: the tables that this hook does not
    create (they belong to the tracking plane's/bootstrap migrations) may or may
    not exist depending on boot order.
    """
    from sqlalchemy import text
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS tenants (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            slug VARCHAR(255) NOT NULL UNIQUE,
            parent_tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            updated_at TIMESTAMP NOT NULL DEFAULT now()
        )"""))
    # Phase 2A plan fields: the seat limit now, retention/billing later.
    _mig(conn, "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free'")
    _mig(conn, "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS seats INTEGER")
    _mig(conn, "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS retention_days INTEGER")
    _mig(conn, "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "
               "features JSONB NOT NULL DEFAULT '{}'::jsonb")
    # Tenant #1 is this install.
    conn.execute(text("INSERT INTO tenants (id, name, slug) VALUES (1, 'Default', 'default') "
                      "ON CONFLICT DO NOTHING"))
    conn.execute(text("SELECT setval(pg_get_serial_sequence('tenants','id'), "
                      "GREATEST((SELECT COALESCE(MAX(id), 1) FROM tenants), 1))"))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_memberships (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            role VARCHAR(32) NOT NULL DEFAULT 'viewer',
            permissions JSONB,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            UNIQUE (user_id, tenant_id)
        )"""))
    # Phase 3: single-use, expiring workspace invitations. Only the SHA-256 hash
    # of the token is stored — the raw token exists in the creation response and
    # nowhere else (app_pages/invitations.py).
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS tenant_invitations (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            email VARCHAR(255),
            username VARCHAR(255),
            role VARCHAR(32) NOT NULL DEFAULT 'viewer',
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            invited_by VARCHAR(255),
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            expires_at TIMESTAMP NOT NULL,
            accepted_at TIMESTAMP,
            revoked_at TIMESTAMP
        )"""))

    existing = {r[0] for r in conn.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).fetchall()}

    for table in TENANT_TABLES:
        if table not in existing:
            continue
        _mig(conn, f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id INTEGER")
        _mig(conn, f"UPDATE {table} SET tenant_id = 1 WHERE tenant_id IS NULL")
        _mig(conn, f"ALTER TABLE {table} ALTER COLUMN tenant_id SET DEFAULT 1")
        _mig(conn, f"ALTER TABLE {table} ALTER COLUMN tenant_id SET NOT NULL")
        _mig(conn, f"CREATE INDEX IF NOT EXISTS {table}_tenant_id_idx ON {table} (tenant_id)")

    for table, rewrites in TENANT_UNIQUE_REWRITES.items():
        if table not in existing:
            continue
        for legacy, columns in rewrites:
            _mig(conn, f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {legacy}")
            idx = f"{table}_tenant_{columns.split(',')[0].strip()}_key"
            _mig(conn, f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} ON {table} "
                       f"(tenant_id, {columns})")

    for idx, table, column in [
        ("conversions_tenant_received_idx", "conversions_data", "received_at"),
        ("conversions_tenant_campaign_idx", "conversions_data", "campaign_id"),
        ("conversions_tenant_click_id_idx", "conversions_data", "click_id"),
        ("click_forward_logs_tenant_created_idx", "click_forward_logs", "created_at"),
        ("postback_logs_tenant_received_idx", "postback_logs", "received_at"),
        ("audit_log_tenant_at_idx", "audit_log", "at"),
    ]:
        if table in existing:
            _mig(conn, f"CREATE INDEX IF NOT EXISTS {idx} ON {table} (tenant_id, {column})")

    # Every user becomes a member of tenant 1: owners/admins for the is_admin
    # users, their install-global permissions copied onto the membership so
    # nobody's access changes. Re-run on every boot so a user created by raw SQL
    # (or before this feature existed) still lands somewhere.
    _mig(conn, """
        INSERT INTO tenant_memberships (user_id, tenant_id, role, permissions)
        SELECT u.id, 1,
               CASE WHEN u.is_admin AND u.id = (SELECT MIN(id) FROM users WHERE is_admin)
                         THEN 'owner'
                    WHEN u.is_admin THEN 'admin'
                    ELSE 'editor' END,
               u.permissions
        FROM users u
        -- Only users with NO membership at all: a user provisioned into a
        -- different workspace later must not be silently added to tenant 1.
        WHERE NOT EXISTS (SELECT 1 FROM tenant_memberships m
                          WHERE m.user_id = u.id)""")


@app.on_event("startup")
async def startup():
    # Lightweight schema migration for installs created before a column existed.
    # Idempotent — safe to run on every boot.
    from sqlalchemy import text
    from db import engine
    try:
        with engine.connect() as conn:
            _mig(conn, "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]'::jsonb")
            # Meta Ads cost auto-sync: the optional ad-platform campaign id
            # (primary match key; NULL until set in the campaign editor).
            _mig(conn, "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS ad_platform_campaign_id VARCHAR(64)")
            # G62/G63 — user security columns
            _mig(conn, "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret TEXT")
            _mig(conn, "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN NOT NULL DEFAULT false")
            _mig(conn, "ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_backup JSONB")
            _mig(conn, "ALTER TABLE users ADD COLUMN IF NOT EXISTS permissions JSONB")
            # G66 — soft-delete flags
            _mig(conn, "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT false")
            _mig(conn, "ALTER TABLE offers ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT false")
            # D1c — campaign ownership for the campaigns:'own' permission
            _mig(conn, "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS owner_id INTEGER")
            # Wave 19B — conversion approval lifecycle (reconciliation for
            # networks that approve conversions) and the dedupe-path flag
            # exposed as the conversions-log duplicate column.
            _mig(conn, "ALTER TABLE conversions_data ADD COLUMN IF NOT EXISTS "
                       "approval VARCHAR(16) NOT NULL DEFAULT 'pending'")
            _mig(conn, "ALTER TABLE conversions_data ADD COLUMN IF NOT EXISTS "
                       "is_duplicate BOOLEAN NOT NULL DEFAULT false")
            # G62 — one-shot TOTP token replay protection
            _mig(conn, """
                CREATE TABLE IF NOT EXISTS totp_token_used (
                    jti TEXT PRIMARY KEY,
                    at TIMESTAMP NOT NULL DEFAULT now()
                )""")
            # G69 — flow monitoring state
            _mig(conn, """
                CREATE TABLE IF NOT EXISTS monitor_state (
                    id BIGSERIAL PRIMARY KEY,
                    entity VARCHAR(32) NOT NULL DEFAULT 'offer',
                    entity_id INTEGER,
                    campaign_id INTEGER,
                    url TEXT NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'unknown',
                    checked_at TIMESTAMP NOT NULL DEFAULT now(),
                    fail_count INTEGER NOT NULL DEFAULT 0
                )""")
            _mig(conn, "CREATE INDEX IF NOT EXISTS monitor_state_url_idx ON monitor_state (url)")
            # G70 — auto rules
            _mig(conn, """
                CREATE TABLE IF NOT EXISTS auto_rules (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT true,
                    scope VARCHAR(32) NOT NULL DEFAULT 'campaign',
                    campaign_id INTEGER,
                    conditions JSONB NOT NULL DEFAULT '[]'::jsonb,
                    action VARCHAR(64) NOT NULL DEFAULT 'alert_telegram',
                    last_run TIMESTAMP,
                    last_result JSONB
                )""")
            # D2 — domain groups with per-user access grants
            _mig(conn, """
                CREATE TABLE IF NOT EXISTS domain_groups (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) UNIQUE NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT now(),
                    updated_at TIMESTAMP NOT NULL DEFAULT now()
                )""")
            _mig(conn, """
                CREATE TABLE IF NOT EXISTS domain_group_domains (
                    group_id INTEGER NOT NULL REFERENCES domain_groups(id) ON DELETE CASCADE,
                    domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
                    PRIMARY KEY (group_id, domain_id)
                )""")
            _mig(conn, """
                CREATE TABLE IF NOT EXISTS domain_group_users (
                    group_id INTEGER NOT NULL REFERENCES domain_groups(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    PRIMARY KEY (group_id, user_id)
                )""")
            # CAPI integration records: pixels, channel/offer bindings and
            # per-channel toggles. The tracking plane (frontend/app.py) creates
            # the same tables on its own startup so either service can boot first.
            _ensure_capi_tables(conn)
            # Logs area audit trails (postback_logs, click_forward_logs,
            # cost_update_logs) + meta_capi_log extension.
            _ensure_logs_tables(conn)
            # Saved library entities: filter presets, scripts, funnel templates.
            _ensure_wave18_tables(conn)
            # Meta Ads cost auto-sync raw audit table (ad_cost_daily).
            _ensure_meta_ads_tables(conn)
            # Ad-platform OAuth connections + single-use CSRF state.
            _ensure_integrations_tables(conn)
            # Multi-tenancy phase 1: tenants, memberships, tenant_id columns,
            # per-tenant uniques. Runs last so every table it touches exists
            # (the guards inside rely on that).
            _ensure_tenant_schema(conn)
            conn.commit()
    except Exception as e:
        print("startup migration:", e)

    # G65 — audit table (idempotent)
    from audit_logger import ensure_audit_table
    ensure_audit_table()

    # Fraud plane: clicks_data.fraud_score + PG honeypot_hits (both idempotent;
    # coordinates with the tracking-plane migration of the same columns).
    _fraud_ch = get_clickhouse_client()
    try:
        ensure_fraud_schema(_fraud_ch)
        # user_agent / os_version columns backing the report dimensions.
        ensure_report_dimensions_schema(_fraud_ch)
    finally:
        try:
            _fraud_ch.close()
        except Exception:
            pass

    import asyncio
    from email_reports import email_report_loop
    asyncio.create_task(email_report_loop())
    # G69 + G70 — monitoring and auto-rules loops (15 min each, staggered)
    from app_pages.monitor import monitor_loop
    from app_pages.rules import auto_rules_loop
    from app_pages.optimizer import optimizer_loop
    asyncio.create_task(monitor_loop())
    asyncio.create_task(auto_rules_loop())
    asyncio.create_task(optimizer_loop())
    # Meta Ads cost auto-sync loop (skipped internally when disabled).
    from app_pages.meta_ads import meta_ads_loop
    asyncio.create_task(meta_ads_loop())


@app.middleware("http")
async def ch_client_per_request(request: Request, call_next):
    """One ClickHouse client per request.

    Sync endpoints run in FastAPI's threadpool while async endpoints run on the
    event loop — a single shared client gets used from both simultaneously and
    clickhouse-connect rejects concurrent queries within one session.
    """
    path = request.scope.get("path", "")
    if not (path.startswith("/img") or path.startswith("/css") or path == "/favicon.ico"):
        request.state.ch = get_clickhouse_client()
        try:
            return await call_next(request)
        finally:
            try:
                request.state.ch.close()
            except Exception:
                pass
    return await call_next(request)

# Project base directory
BASE_DIR = Path(__file__).resolve().parent

# Theme settings
THEMES_DIR = BASE_DIR / "themes"
THEME_NAME = "default"
THEME_DIR = THEMES_DIR / THEME_NAME
CSS_DIR = THEME_DIR / "css"
# Serve static files (images, styles, scripts)
app.mount("/img", StaticFiles(directory=THEME_DIR / "img"), name="img")
app.mount("/css", StaticFiles(directory=CSS_DIR), name="css")

# Template setup (Jinja2)
templates = Jinja2Templates(directory=THEME_DIR)

###############################################
################### STATIC ####################
###############################################
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(THEME_DIR / "favicon.ico")


###############################################
################### PAGES #####################
###############################################


ALLOWED_PAGES = {"auth", "dashboard", "editor"}

# Sections of the dashboard shell that get their own URL — /backend/<section>
# serves the shell pre-focused on that section (deep-linkable, back-button friendly).
NAV_SECTIONS = {"dashboard", "campaigns", "landings", "affiliates", "offers",
                "sources", "reports", "domains", "settings", "users", "documentation",
                "fraud", "optimizer", "conversion-tracking", "logs", "scripts",
                "integrations", "capi-integrations", "bot-rules", "rules",
                "filter-presets", "fallback"}
# auth.PERMISSION_SECTIONS and auth.ADMIN_ONLY_SECTIONS (see how
# "conversion-tracking" is registered there). backend/auth.py is owned by
# another change right now, so until that lands the section gate below treats
# any NAV section absent from the permission map as admin-only.


from typing import Optional
from auth import is_authenticated, get_user_permissions, router as auth_router
from app_pages.domains import router as domains_router  # Import the router
from app_pages.settings import router as settings_router  # Import the router
from app_pages.users import router as users_router  # Import the router
from app_pages.sources import router as sources_router
from app_pages.affiliates import router as affiliate_router
from app_pages.offers import router as offers_router
from app_pages.campaigns import router as campaign_router
from app_pages.dashboard import router as dashboard_router
from app_pages.dashboard import public_router as dashboard_public_router  # G52: unauthenticated share access
from app_pages.reports import router as reports_router  # Import the router
from app_pages.archive import router as archive_router, audit_router  # G66 + G65 read API
from app_pages.monitor import router as monitor_router  # G69 flow monitoring
from app_pages.rules import router as rules_router  # G70 auto rules
from app_pages.fraud import router as fraud_router, ensure_fraud_schema  # fraud & cloaking
from app_pages.optimizer import router as optimizer_router  # G76 AI auto-optimizer
from app_pages.search import router as search_router  # G75 global search

# G63: every router is gated by its nav section's read permission; mutating
# methods additionally require the caller's write flag (non-admins).
app.include_router(dashboard_router, prefix="/api/dashboard", tags=["Dashboard"],
                   dependencies=[Depends(require_section_write("dashboard"))])
# Public shared reports (G52): no auth dependency — access is via unguessable token.
app.include_router(dashboard_public_router, prefix="/api/dashboard", tags=["Public"])
app.include_router(offers_router, prefix="/api/offers", tags=["Offers"],
                   dependencies=[Depends(require_section_write("offers"))])


# Include the router
app.include_router(auth_router, prefix="/api", tags=["Auth"])
app.include_router(domains_router, prefix="/api/domains", tags=["Domains"],
                   dependencies=[Depends(require_section_write("domains"))])
app.include_router(settings_router, prefix="/api/settings", tags=["Settings"],
                   dependencies=[Depends(require_section_write("settings"))])
app.include_router(users_router, prefix="/api/users", tags=["Users"],
                   dependencies=[Depends(require_section_write("users"))])
app.include_router(sources_router, prefix="/api/sources", tags=["Sources"],
                   dependencies=[Depends(require_section_write("sources"))])

app.include_router(affiliate_router, prefix="/api/affiliate-networks", tags=["Affiliate Networks"],
                   dependencies=[Depends(require_section_write("affiliates"))])

app.include_router(campaign_router, prefix="/api/campaigns", tags=["Campaigns"],
                   dependencies=[Depends(require_section_write("campaigns"))])

app.include_router(reports_router, prefix="/api/reports", tags=["Reports"],
                   dependencies=[Depends(require_section_write("reports"))])

# G66: archive/restore (soft-delete) — mutating: requires campaigns write;
# non-admins may only archive/restore campaigns they own (enforced in the router).
app.include_router(archive_router, prefix="/api/archive", tags=["Archive"],
                   dependencies=[Depends(require_section_write("campaigns"))])
# G65: audit log read API — settings is an admin-only section.
app.include_router(audit_router, prefix="/api/audit", tags=["Audit"],
                   dependencies=[Depends(require_section("settings"))])

# G69: flow monitoring — configured from Settings, admin-only section.
app.include_router(monitor_router, prefix="/api/monitor", tags=["Monitoring"],
                   dependencies=[Depends(require_section_write("settings")),
                                 Depends(require_tenant_feature("monitoring"))])
# G70: auto rules — same admin plane as monitoring.
app.include_router(rules_router, prefix="/api/rules", tags=["Auto Rules"],
                   dependencies=[Depends(require_section_write("settings"))])
# Fraud & cloaking dashboard — same admin plane as monitoring/rules.
app.include_router(fraud_router, prefix="/api/fraud", tags=["Fraud"],
                   dependencies=[Depends(require_section_write("settings"))])
# G76: AI auto-optimizer — same admin plane as monitoring/rules/fraud, plus the
# workspace's "optimizer" feature flag (a workspace can switch the optimizer off).
app.include_router(optimizer_router, prefix="/api/optimizer", tags=["Optimizer"],
                   dependencies=[Depends(require_section_write("settings")),
                                 Depends(require_tenant_feature("optimizer"))])
# G56: anomaly insights — same admin plane as monitoring/rules/fraud.
from app_pages.insights import router as insights_router
app.include_router(insights_router, prefix="/api/insights", tags=["Insights"],
                   dependencies=[Depends(require_section_write("settings"))])
# G76: MCP / AI-agent access — JSON-RPC 2.0 endpoint, Bearer API token or
# admin session; same admin plane as monitoring/rules/fraud. Mounted behind
# the WRITE gate: campaigns.set_status mutates campaign state.
from app_pages.mcp import router as mcp_router
app.include_router(mcp_router, prefix="/api/mcp", tags=["MCP"],
                   dependencies=[Depends(require_section_write("settings"))])
# G75: global search — any authenticated user; results filtered by permissions.
app.include_router(search_router, prefix="/api/search", tags=["Search"],
                   dependencies=[Depends(require_section("dashboard"))])
# G78: system status — admin-only section, like audit/monitoring.
from app_pages.status import router as status_router
app.include_router(status_router, prefix="/api/status", tags=["Status"],
                   dependencies=[Depends(require_section("settings"))])
# Retroactive cost update — same admin plane as monitoring/rules/fraud.
from app_pages.costs import router as costs_router
app.include_router(costs_router, prefix="/api/costs", tags=["Costs"],
                   dependencies=[Depends(require_section_write("settings"))])
# Meta Ads cost auto-sync — same admin plane as monitoring/rules/fraud.
from app_pages.meta_ads import router as meta_ads_router
app.include_router(meta_ads_router, prefix="/api/meta-ads", tags=["Meta Ads"],
                   dependencies=[Depends(require_section_write("settings"))])
# Ad-platform OAuth "Connect" flow (Meta first) — same admin plane.
from app_pages.integrations import router as integrations_router
app.include_router(integrations_router, prefix="/api/integrations", tags=["Integrations"],
                   dependencies=[Depends(require_section_write("settings"))])
# Logs area — admin-only audit surface. Gated by the "logs" section write flag
# (the section entry is pending in auth.py; see the NAV_SECTIONS TODO).
from app_pages.logs import router as logs_router
app.include_router(logs_router, prefix="/api/logs", tags=["Logs"],
                   dependencies=[Depends(require_section_write("logs"))])
# Saved filter presets — reachable from the Logs and Reports views, so the gate
# is the always-readable dashboard section rather than a single owning section.
from app_pages.filter_presets import router as filter_presets_router
app.include_router(filter_presets_router, prefix="/api/filter-presets", tags=["Filter presets"],
                   dependencies=[Depends(require_section_write("dashboard"))])
# Script library — same admin plane as the Logs section.
from app_pages.scripts import router as scripts_router
app.include_router(scripts_router, prefix="/api/scripts", tags=["Scripts"],
                   dependencies=[Depends(require_section_write("scripts"))])
# Funnel templates — owned by the campaign editor.
from app_pages.funnel_templates import router as funnel_templates_router
app.include_router(funnel_templates_router, prefix="/api/funnel-templates", tags=["Funnel templates"],
                   dependencies=[Depends(require_section_write("campaigns"))])
# Wave 19A: workspace display settings (number formatting / row colouring /
# default columns) — read by every data-table page, so gated by the
# always-readable dashboard section rather than the admin settings section.
from app_pages.workspace import router as workspace_router
app.include_router(workspace_router, prefix="/api/workspace", tags=["Workspace"],
                   dependencies=[Depends(require_section("dashboard"))])
# Multi-tenancy phase 1: workspace list / switching. Any authenticated user may
# read their own memberships and switch into one; creating a tenant is
# admin-checked inside the router.
from app_pages.tenants import router as tenants_router
app.include_router(tenants_router, prefix="/api/tenants", tags=["Tenants"],
                   dependencies=[Depends(require_section("dashboard"))])

# Multi-tenancy phase 2A: workspace member management. Authenticated only at
# the router level — each handler resolves authority from the caller's
# membership role in the target tenant (owners/admins manage, platforms may
# pass ?tenant_id=).
from app_pages.members import router as members_router
app.include_router(members_router, prefix="/api/members", tags=["Members"],
                   dependencies=[Depends(require_api_auth)])

# Multi-tenancy phase 3: workspace invitations. No router-level dependency on
# purpose — `lookup` and `accept` are public (the invitee has no account yet);
# the other three endpoints require authentication and resolve owner/admin
# authority inside the request's workspace in the handlers.
from app_pages.invitations import router as invitations_router
app.include_router(invitations_router, prefix="/api/invitations", tags=["Invitations"])


# G52: minimal public view for shared reports — shell-less, token in the query
# string; the page itself only exposes the shared report's breakdown data.
# Must be registered BEFORE the /{page} catch-all below.
@app.get("/public-report", response_class=HTMLResponse)
async def public_report_page(request: Request):
    return templates.TemplateResponse(request, "pages/public_report.html", {})


# Router
@app.get("/", response_class=HTMLResponse)
@app.get("/{page}", response_class=HTMLResponse)
async def serve_page(request: Request, page: Optional[str] = None):
    user_type = is_authenticated(request);
    if page is None:
        page = "auth"
    if page == "auth" and user_type:
        page = "dashboard"
    # Legacy slug: the docs page moved from /backend/about to /backend/documentation
    if page == "about":
        return RedirectResponse(url="/backend/documentation", status_code=307)
    section = None
    perms = None
    if page in NAV_SECTIONS:
        if not user_type:
            page = "auth"
        else:
            # G63: deep links to sections the user cannot read fall back to dashboard
            from auth import get_session_username
            perms = get_user_permissions(get_session_username(request))
            # A NAV section absent from the permission map (the logs section,
            # pending its auth.PERMISSION_SECTIONS entry) is admin-only.
            allowed = perms["sections"].get(page, False) or (
                page not in perms["sections"] and user_type == "admin")
            if not allowed:
                page = "dashboard"
            else:
                section = page
                page = "dashboard"
    if page not in ALLOWED_PAGES or not user_type:
        page = "auth"  # Or a 404 could be returned instead
    if user_type and perms is None:
        from auth import get_session_username
        perms = get_user_permissions(get_session_username(request))
    page_file = f"pages/{page}.html"
    # Workspace feature flags drive a couple of nav entries (Optimizer) so the
    # shell does not offer a section the workspace has switched off.
    tenant_features = {}
    if user_type:
        from tenant_context import current_tenant
        from tenant_settings import tenant_features as _tenant_features
        tenant_features = _tenant_features(current_tenant())
    return templates.TemplateResponse(request, "index.html", {
        "page_to_include": page_file,
        "page": page,
        "THEME_NAME": THEME_NAME,
        "is_authenticated_user_type": user_type,
        "initial_section": section or "dashboard",
        "permissions": perms if user_type else None,
        "tenant_features": tenant_features,
        "page_component": '<'+page+'-page-component></'+page+'-page-component>',
    })
