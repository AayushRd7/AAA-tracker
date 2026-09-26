from fastapi import FastAPI, Request, Depends
from auth import require_section, require_section_write
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from fastapi.responses import FileResponse
from clickHouse import get_clickhouse_client
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


@app.on_event("startup")
async def startup():
    # Lightweight schema migration for installs created before a column existed.
    # Idempotent — safe to run on every boot.
    from sqlalchemy import text
    from db import engine
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]'::jsonb"))
            # G62/G63 — user security columns
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret TEXT"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN NOT NULL DEFAULT false"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_backup JSONB"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS permissions JSONB"))
            # G66 — soft-delete flags
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT false"))
            conn.execute(text("ALTER TABLE offers ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT false"))
            # D1c — campaign ownership for the campaigns:'own' permission
            conn.execute(text("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS owner_id INTEGER"))
            # G62 — one-shot TOTP token replay protection
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS totp_token_used (
                    jti TEXT PRIMARY KEY,
                    at TIMESTAMP NOT NULL DEFAULT now()
                )"""))
            # G69 — flow monitoring state
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS monitor_state (
                    id BIGSERIAL PRIMARY KEY,
                    entity VARCHAR(32) NOT NULL DEFAULT 'offer',
                    entity_id INTEGER,
                    campaign_id INTEGER,
                    url TEXT NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'unknown',
                    checked_at TIMESTAMP NOT NULL DEFAULT now(),
                    fail_count INTEGER NOT NULL DEFAULT 0
                )"""))
            conn.execute(text("CREATE INDEX IF NOT EXISTS monitor_state_url_idx ON monitor_state (url)"))
            # G70 — auto rules
            conn.execute(text("""
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
                )"""))
            conn.commit()
    except Exception as e:
        print("startup migration:", e)

    # G65 — audit table (idempotent)
    from audit_logger import ensure_audit_table
    ensure_audit_table()

    import asyncio
    from email_reports import email_report_loop
    asyncio.create_task(email_report_loop())
    # G69 + G70 — monitoring and auto-rules loops (15 min each, staggered)
    from app_pages.monitor import monitor_loop
    from app_pages.rules import auto_rules_loop
    asyncio.create_task(monitor_loop())
    asyncio.create_task(auto_rules_loop())


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
                "sources", "reports", "domains", "settings", "users", "about"}


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
                   dependencies=[Depends(require_section_write("settings"))])
# G70: auto rules — same admin plane as monitoring.
app.include_router(rules_router, prefix="/api/rules", tags=["Auto Rules"],
                   dependencies=[Depends(require_section_write("settings"))])
# G75: global search — any authenticated user; results filtered by permissions.
app.include_router(search_router, prefix="/api/search", tags=["Search"],
                   dependencies=[Depends(require_section("dashboard"))])


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
    section = None
    perms = None
    if page in NAV_SECTIONS:
        if not user_type:
            page = "auth"
        else:
            # G63: deep links to sections the user cannot read fall back to dashboard
            from auth import get_session_username
            perms = get_user_permissions(get_session_username(request))
            if not perms["sections"].get(page, False):
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
    return templates.TemplateResponse(request, "index.html", {
        "page_to_include": page_file,
        "page": page,
        "THEME_NAME": THEME_NAME,
        "is_authenticated_user_type": user_type,
        "initial_section": section or "dashboard",
        "permissions": perms if user_type else None,
        "page_component": '<'+page+'-page-component></'+page+'-page-component>',
    })
