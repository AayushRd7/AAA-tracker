# app_pages/auth.py
from fastapi import APIRouter, Request, Response, HTTPException, status, Depends, Header
from pydantic import BaseModel

from sqlalchemy.orm import Session
from jose import jwt, JWTError
from typing import Optional
from db import get_db, get_user, SessionLocal
from hashlib import md5
from datetime import datetime, timedelta
import secrets
import json
import re
import time
import os

import bcrypt
import pyotp
import hashlib
import hmac
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from models.user import UserORM
from models.settings import SettingsORM
from tenant_context import current_tenant, api_token_tenant as _api_token_tenant

router = APIRouter()


# ====== Admin login IP whitelist (G90) ======
# Settings-managed list of allowed CIDRs for /login and the TOTP step, stored
# at settings.login_security.ip_whitelist. Empty list = current behavior.
def _client_ip(request: Request) -> str:
    """Real client IP — X-Real-IP (set by nginx, unforgeable) first, then the
    peer address (uvicorn --proxy-headers resolves X-Forwarded-For)."""
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    return request.client.host if request.client else "unknown"


def _login_whitelist(db: Session) -> str:
    try:
        row = db.query(SettingsORM).filter_by(name="settings").first()
        cfg = json.loads(row.value) if row and row.value else {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        return ""
    sec = cfg.get("login_security") or {}
    if not isinstance(sec, dict):
        return ""
    return str(sec.get("ip_whitelist") or "").strip()


def login_ip_allowed(db: Session, client_ip: str) -> tuple[bool, str]:
    """(allowed, reason). Empty whitelist allows everyone; a non-empty list
    requires the client IP to fall inside one of its CIDRs/IPs."""
    whitelist = _login_whitelist(db)
    if not whitelist:
        return True, ""
    import ipaddress
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False, f"Login not allowed: '{client_ip}' is not a valid IP address"
    for raw in re.split(r"[,\s]+", whitelist):
        raw = raw.strip()
        if not raw:
            continue
        try:
            if "/" in raw:
                if addr in ipaddress.ip_network(raw, strict=False):
                    return True, ""
            elif addr == ipaddress.ip_address(raw):
                return True, ""
        except ValueError:
            continue
    return False, f"Login not allowed from {client_ip} — not in the admin IP whitelist"

# JWT configuration (kept for compatibility; sessions are now DB-backed)
SECRET_KEY = os.environ.get("JWT_SECRET", "your-super-secret-key-for-jwt")
ALGORITHM = "HS256"
pass_salt = 'akm_'

SESSION_TTL_DAYS = 30


# ====== Spent TOTP token jtis (replay protection) ======
_totp_token_table_ready = False


def ensure_totp_token_table():
    """One-shot TOTP tokens: a spent jti is persisted so the token cannot be
    replayed to mint multiple sessions within its 5-minute TTL."""
    global _totp_token_table_ready
    if _totp_token_table_ready:
        return
    db = SessionLocal()
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS totp_token_used (
            jti TEXT PRIMARY KEY,
            at TIMESTAMP NOT NULL DEFAULT now()
        )
    """))
    db.commit()
    db.close()
    _totp_token_table_ready = True


def _spend_totp_jti(db: Session, jti: str) -> bool:
    """Persist the jti before verifying the code. A unique violation means the
    token was already spent — reject the replay. Returns True when spent."""
    if not jti:
        return True
    try:
        db.execute(text("INSERT INTO totp_token_used (jti) VALUES (:j)"), {"j": jti})
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


# ====== Password hashing (bcrypt, with legacy md5 auto-upgrade) ======
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def is_bcrypt_hash(stored: str) -> bool:
    return (stored or "").startswith(("$2a$", "$2b$", "$2y$"))


def verify_password(plain: str, stored: str) -> bool:
    stored = stored or ""
    if is_bcrypt_hash(stored):
        try:
            return bcrypt.checkpw(plain.encode(), stored.encode())
        except ValueError:
            return False
    # Legacy md5(akm_ + password) — verified, then upgraded on login
    return md5((pass_salt + plain).encode()).hexdigest() == stored


# ====== DB-backed sessions (revocable, survive restarts) ======
_sessions_table_ready = False


def ensure_sessions_table():
    global _sessions_table_ready
    if _sessions_table_ready:
        return
    db = SessionLocal()
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token TEXT PRIMARY KEY,
            username VARCHAR(255) NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            expires_at TIMESTAMP NOT NULL,
            revoked BOOLEAN NOT NULL DEFAULT false,
            current_tenant_id INTEGER
        )
    """))
    # Session device/visibility columns (added after the original table).
    # created_at predates this block, so the ALTER is a no-op on fresh installs.
    db.execute(text("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS "
                    "created_at TIMESTAMP DEFAULT now()"))
    db.execute(text("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS "
                    "last_seen TIMESTAMP"))
    db.execute(text("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS "
                    "ip VARCHAR(64) DEFAULT ''"))
    db.execute(text("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS "
                    "user_agent TEXT DEFAULT ''"))
    # Multi-tenancy phase 1 — the tenant this session is currently working in.
    # NULL means "never switched": the resolver treats it as tenant 1.
    db.execute(text("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS "
                    "current_tenant_id INTEGER"))
    db.commit()
    db.close()
    _sessions_table_ready = True


def create_session(username: str, ip: str = "", user_agent: str = "") -> str:
    ensure_sessions_table()
    token = secrets.token_urlsafe(32)
    db = SessionLocal()
    db.execute(text(
        "INSERT INTO auth_sessions (token, username, created_at, last_seen, "
        "expires_at, ip, user_agent) "
        "VALUES (:t, :u, now(), now(), :e, :ip, :ua)"),
        {"t": token, "u": username,
         "e": datetime.utcnow() + timedelta(days=SESSION_TTL_DAYS),
         "ip": (ip or "")[:64], "ua": (user_agent or "")[:512]})
    db.commit()
    db.close()
    return token


def revoke_session(token: str):
    ensure_sessions_table()
    db = SessionLocal()
    db.execute(text("DELETE FROM auth_sessions WHERE token = :t"), {"t": token})
    db.commit()
    db.close()


def session_token(request: Request) -> str:
    """The raw session cookie for the current request ('' when absent)."""
    return request.cookies.get("session_token") or ""


def user_agent(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:512]


def parse_user_agent(ua: str) -> dict:
    """Best-effort {device, browser, os} from a User-Agent string.

    Not a full UA database — just enough to label a session row for a human.
    """
    ua = ua or ""
    low = ua.lower()
    os_name = ""
    if "windows nt" in low:
        os_name = "Windows"
    elif "iphone" in low or "ipad" in low:
        os_name = "iOS"
    elif "android" in low:
        os_name = "Android"
    elif "mac os x" in low or "macintosh" in low:
        os_name = "macOS"
    elif "cros" in low:
        os_name = "ChromeOS"
    elif "linux" in low:
        os_name = "Linux"

    browser = ""
    if "edg/" in low or "edge/" in low:
        browser = "Edge"
    elif "opr/" in low or "opera" in low:
        browser = "Opera"
    elif "chrome/" in low or "chromium/" in low:
        browser = "Chrome"
    elif "firefox/" in low:
        browser = "Firefox"
    elif "safari/" in low:
        browser = "Safari"
    elif "curl/" in low or "python-requests" in low or "wget/" in low:
        browser = "CLI"

    if not ua:
        device = "Unknown"
    elif "bot" in low or "spider" in low or "crawler" in low or "curl/" in low \
            or "python-requests" in low:
        device = "Bot/CLI"
    elif "iphone" in low:
        device = "iPhone"
    elif "ipad" in low:
        device = "iPad"
    elif "android" in low and "mobile" in low:
        device = "Android phone"
    elif "android" in low:
        device = "Android tablet"
    elif "mobile" in low:
        device = "Mobile"
    elif os_name in ("Windows", "macOS", "Linux", "ChromeOS"):
        device = "Desktop"
    else:
        device = "Unknown"
    return {"device": device, "browser": browser or "Unknown", "os": os_name or "Unknown"}


def _session_public(row, current_token: str = "") -> dict:
    """One session row shaped for the UI (never includes the full token)."""
    token = row[0] or ""
    ua = row[4] or ""
    return {
        "id": token[:12],
        "token_prefix": token[:12],
        "created_at": row[1].isoformat() if row[1] else None,
        "last_seen": row[2].isoformat() if row[2] else None,
        "ip": row[3] or "",
        "user_agent": ua,
        "current": bool(current_token and token == current_token),
        **parse_user_agent(ua),
    }


def list_sessions(db: Session, username: str, current_token: str = "") -> list:
    """Active (non-revoked, unexpired) sessions for a username, newest first."""
    rows = db.execute(text(
        "SELECT token, created_at, last_seen, ip, user_agent FROM auth_sessions "
        "WHERE username = :u AND revoked = false "
        "AND (expires_at IS NULL OR expires_at >= now()) "
        "ORDER BY created_at DESC NULLS LAST"),
        {"u": username}).fetchall()
    return [_session_public(r, current_token) for r in rows]


def revoke_session_prefix(db: Session, username: str, prefix: str) -> int:
    """Delete the given user's session whose token starts with `prefix`."""
    prefix = (prefix or "").strip()
    if not prefix:
        return 0
    res = db.execute(text(
        "DELETE FROM auth_sessions WHERE username = :u AND token LIKE :p"),
        {"u": username, "p": prefix + "%"})
    db.commit()
    return res.rowcount or 0


def revoke_other_sessions(db: Session, username: str, current_token: str) -> int:
    """Delete every session of `username` except the caller's current one."""
    res = db.execute(text(
        "DELETE FROM auth_sessions WHERE username = :u AND token != :t"),
        {"u": username, "t": current_token or ""})
    db.commit()
    return res.rowcount or 0


def load_api_token() -> str:
    """Tenant 1's API token (the credential the Settings page shows).

    Kept for the install-wide default; a *request* is resolved with
    ``resolve_api_token``, which finds the workspace that owns the presented
    token. Tokens live in each tenant's own settings document.
    """
    try:
        db = SessionLocal()
        row = db.execute(text("SELECT value FROM settings "
                              "WHERE name = 'settings' AND tenant_id = 1")).fetchone()
        db.close()
        if row and row[0]:
            return (json.loads(row[0]).get("apiToken") or "").strip()
    except Exception:
        pass
    return ""


def resolve_api_token(provided: str) -> Optional[int]:
    """The tenant whose settings document holds ``provided`` as its apiToken.

    Returns None for an empty/unknown token. One settings row per tenant, so
    this scans the (small) tenant set; the lowest matching tenant wins when two
    workspaces somehow share a token. Called by the request middleware, which
    then runs the whole request in that tenant — a token is therefore a
    *workspace-scoped* credential, never install-wide.
    """
    provided = (provided or "").strip()
    if not provided:
        return None
    try:
        db = SessionLocal()
        try:
            rows = db.execute(text(
                "SELECT tenant_id, value FROM settings WHERE name = 'settings' "
                "ORDER BY tenant_id ASC")).fetchall()
        finally:
            db.close()
    except Exception:
        return None
    match = None
    for tenant_id, value in rows:
        if not value:
            continue
        try:
            token = (json.loads(value).get("apiToken") or "").strip()
        except Exception:
            continue
        if token and len(token) == len(provided) \
                and secrets.compare_digest(token, provided):
            match = int(tenant_id) if match is None else min(match, int(tenant_id))
    return match


def is_platform_operator(request: Request) -> bool:
    """True only for a session user whose ``users.is_admin`` flag is set.

    A Bearer API token is a workspace-scoped credential (phase 2B): it acts as
    the owner of the workspace it belongs to and is deliberately NOT a platform
    operator, so it cannot reach the install-global user plane or target
    another workspace through the platform-only escape hatches.
    """
    if _api_token_tenant() is not None:
        return False
    _, is_admin = get_caller(request)
    return is_admin


# Model for passing the login and password
class LoginRequest(BaseModel):
    username: str
    password: str


# ====== Authorization check ======
def is_authenticated(request: Request) -> any:
    """Resolve the session cookie to 'admin' | 'user' | False (DB-backed)."""
    token = request.cookies.get("session_token")
    if not token:
        return False

    try:
        ensure_sessions_table()
        db: Session = SessionLocal()
        row = db.execute(text(
            "SELECT username, expires_at, revoked, last_seen "
            "FROM auth_sessions WHERE token = :t"),
            {"t": token}).fetchone()
        if not row or row.revoked:
            db.close()
            return False
        if row.expires_at and row.expires_at < datetime.utcnow():
            db.close()
            return False

        # Refresh last_seen, throttled to at most once per ~60s per session so
        # the hot auth path stays cheap. Never fatal — a failed touch must not
        # break the request.
        try:
            if row.last_seen is None or \
                    (datetime.utcnow() - row.last_seen).total_seconds() > 60:
                db.execute(text("UPDATE auth_sessions SET last_seen = now() "
                                "WHERE token = :t"), {"t": token})
                db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass

        user = get_user(db, row.username)
        db.close()
        if not user or not user.active:
            return False
        return "admin" if user.is_admin else "user"
    except Exception:
        return False


def get_session_username(request: Request) -> Optional[str]:
    """Username for the current session cookie, or None."""
    token = request.cookies.get("session_token")
    if not token:
        return None
    try:
        ensure_sessions_table()
        db: Session = SessionLocal()
        row = db.execute(text(
            "SELECT username, expires_at, revoked FROM auth_sessions WHERE token = :t"),
            {"t": token}).fetchone()
        db.close()
        if not row or row.revoked:
            return None
        if row.expires_at and row.expires_at < datetime.utcnow():
            return None
        return row.username
    except Exception:
        return None


# ====== Multi-tenancy phase 1: current-tenant resolution ======
# The tenant of a request is the *session's* current tenant, validated against
# tenant_memberships on every request. No endpoint accepts a tenant id from a
# query parameter or header (a caller cannot spoof a workspace), and a stale
# session value (membership revoked) falls back to the user's first membership.
TENANT_ROLES = ("owner", "admin", "editor", "viewer")


def session_tenant_id(token: str) -> Optional[int]:
    """The raw current_tenant_id stored on the session row (None when unset)."""
    if not token:
        return None
    try:
        ensure_sessions_table()
        db = SessionLocal()
        row = db.execute(text("SELECT current_tenant_id FROM auth_sessions WHERE token = :t"),
                         {"t": token}).fetchone()
        db.close()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def list_user_memberships(db: Session, username: str) -> list:
    """[{tenant_id, role, name, slug, status}] — tenant_id ascending. Uses raw SQL
    because tenant_memberships/tenants are deliberately install-global (no
    TenantMixin), so the ORM scoping must not apply here."""
    rows = db.execute(text(
        "SELECT m.tenant_id, m.role, t.name, t.slug, t.status "
        "FROM tenant_memberships m JOIN tenants t ON t.id = m.tenant_id "
        "JOIN users u ON u.id = m.user_id "
        "WHERE u.username = :u ORDER BY m.tenant_id ASC"),
        {"u": username}).fetchall()
    return [{"tenant_id": int(r[0]), "role": r[1] or "viewer",
             "name": r[2] or f"Tenant {r[0]}", "slug": r[3] or "", "status": r[4] or "active"}
            for r in rows]


def resolve_session_tenant(token: str) -> tuple[Optional[int], bool]:
    """(tenant_id, has_membership) for a session token.

    * no/expired/revoked session -> (None, True): caller is unauthenticated; the
      middleware leaves the default tenant 1 in place (every data endpoint
      requires auth anyway).
    * authenticated with memberships -> the stored tenant when it is still a
      membership OR a descendant of a workspace the user manages (owner/admin —
      the phase-3 traversal), else the lowest tenant_id the user belongs to
      (persisted back onto the session). This is the "membership revoked
      mid-session" fallback.
    * authenticated with no memberships -> (None, False): the caller gets 403
      from require_api_auth.
    """
    if not token:
        return None, True
    try:
        ensure_sessions_table()
        db = SessionLocal()
        try:
            row = db.execute(text(
                "SELECT username, expires_at, revoked, current_tenant_id "
                "FROM auth_sessions WHERE token = :t"), {"t": token}).fetchone()
            if not row or row.revoked:
                return None, True
            if row.expires_at and row.expires_at < datetime.utcnow():
                return None, True
            memberships = list_user_memberships(db, row.username)
            if not memberships:
                return None, False
            member_ids = [m["tenant_id"] for m in memberships]
            stored = int(row.current_tenant_id) if row.current_tenant_id is not None else None
            tenant_id = stored if stored in member_ids else None
            if tenant_id is None and stored is not None \
                    and managed_ancestor_role(db, row.username, stored):
                # The session is parked in a DESCENDANT of a workspace the user
                # manages (see POST /api/tenants/switch): keep it there. Any
                # other non-membership tenant falls back to the first membership.
                tenant_id = stored
            if tenant_id is None:
                tenant_id = member_ids[0]
            if stored != tenant_id:
                db.execute(text("UPDATE auth_sessions SET current_tenant_id = :tid "
                                "WHERE token = :t"), {"tid": tenant_id, "t": token})
                db.commit()
            return tenant_id, True
        finally:
            db.close()
    except Exception:
        # Never let tenant resolution break a request: fall back to tenant 1.
        return None, True


def set_session_tenant(db: Session, token: str, tenant_id: int) -> None:
    db.execute(text("UPDATE auth_sessions SET current_tenant_id = :tid WHERE token = :t"),
               {"tid": int(tenant_id), "t": token})
    db.commit()


def caller_role_in_tenant(db: Session, username: str, tenant_id: int) -> Optional[str]:
    """The caller's role in a tenant, or None when they are not a member."""
    row = db.execute(text(
        "SELECT m.role FROM tenant_memberships m JOIN users u ON u.id = m.user_id "
        "WHERE u.username = :u AND m.tenant_id = :t"),
        {"u": username, "t": int(tenant_id)}).fetchone()
    return row[0] if row else None


# Manager roles that traverse the workspace hierarchy (phase 3): an owner or
# admin of a workspace also acts in that workspace's descendants.
MANAGER_ROLES = ("owner", "admin")


def managed_ancestor_role(db: Session, username: Optional[str],
                          tenant_id: int) -> Optional[str]:
    """The owner/admin role the caller holds in ``tenant_id`` or one of its
    ANCESTORS, or None.

    Walks the ``parent_tenant_id`` chain upward from the target workspace and
    returns the first owner/admin role found. Access therefore only ever flows
    *down* the tree: a manager of a parent reaches every descendant, a manager
    of a child never reaches its parent, and a sibling or cousin of a managed
    workspace stays out of reach. The visited set guards against a malformed
    ``parent_tenant_id`` cycle.
    """
    if not username:
        return None
    seen = set()
    current = int(tenant_id)
    while current is not None and current not in seen:
        seen.add(current)
        role = caller_role_in_tenant(db, username, current)
        if role in MANAGER_ROLES:
            return role
        row = db.execute(text("SELECT parent_tenant_id FROM tenants WHERE id = :t"),
                         {"t": current}).fetchone()
        if not row or row[0] is None:
            return None
        current = int(row[0])
    return None


def effective_tenant_role(db: Session, username: Optional[str],
                          tenant_id: int) -> Optional[str]:
    """The role the caller exercises in ``tenant_id``: a direct membership, or
    the role inherited from a managed ancestor workspace (hierarchy traversal)."""
    role = caller_role_in_tenant(db, username, tenant_id)
    if role:
        return role
    return managed_ancestor_role(db, username, tenant_id)


def require_api_auth(request: Request, authorization: Optional[str] = Header(None)):
    """Dependency for API routers: accepts a session cookie or a Bearer API token.

    A Bearer token is resolved by the middleware to the tenant that owns it
    (``tenant_context.api_token_tenant``): the request then runs inside that
    workspace and the token can never reach another one.
    """
    if authorization and authorization.lower().startswith("bearer "):
        if _api_token_tenant() is not None:
            return "api_token"
        raise HTTPException(status_code=401, detail="Invalid API token")

    user_type = is_authenticated(request)
    if not user_type:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Multi-tenancy: an authenticated user with no tenant_memberships row has no
    # workspace to act in. The middleware resolves this; every data endpoint is
    # behind this dependency.
    from tenant_context import tenant_missing
    if tenant_missing():
        raise HTTPException(status_code=403, detail="No workspace membership for this account")
    return user_type


# ====== G63 / phase 2A: per-resource permissions ======
# Sections a workspace can grant. Admin-only sections are the workspace's
# management plane; the rest are content. Phase 2A resolves authority from the
# caller's *membership in the request's tenant*, never from the users row.
PERMISSION_SECTIONS = ["dashboard", "campaigns", "landings", "affiliates", "offers",
                       "sources", "reports", "domains", "settings", "users", "documentation",
                       "fraud", "optimizer", "conversion-tracking", "logs", "scripts",
                       "integrations", "capi-integrations"]
ADMIN_ONLY_SECTIONS = {"users", "settings", "domains", "fraud", "optimizer",
                       "conversion-tracking", "logs", "scripts",
                       "integrations", "capi-integrations"}

# Role -> default permission map. This is the documented matrix:
#   owner  — everything in the tenant, including ownership transfer.
#   admin  — everything in the tenant except changing the owner.
#   editor — read+write on content sections; no workspace/user/settings plane.
#   viewer — read-only on content sections.
# A membership's explicit `permissions` JSONB (copied from users.permissions by
# the phase-1 backfill, editable via /api/members) is layered on top per section.
ROLE_DEFAULT_WRITE = {"owner": True, "admin": True, "editor": True, "viewer": False}
ROLE_HAS_ADMIN_SECTIONS = {"owner": True, "admin": True, "editor": False, "viewer": False}


def role_default_permissions(role: Optional[str]) -> dict:
    role = (role or "viewer").lower()
    admin_ok = ROLE_HAS_ADMIN_SECTIONS.get(role, False)
    sections = {s: (True if s not in ADMIN_ONLY_SECTIONS else admin_ok)
                for s in PERMISSION_SECTIONS}
    return {"sections": sections, "write": bool(ROLE_DEFAULT_WRITE.get(role, False))}


def resolve_membership_permissions(raw: Optional[dict], role: Optional[str]) -> dict:
    """Effective map = role defaults layered under the membership's explicit
    per-section overrides (and the optional global write flag). Extra keys in
    the raw blob (e.g. campaigns:'own') are carried through for callers that
    consume them."""
    perms = role_default_permissions(role)
    raw = raw if isinstance(raw, dict) else {}
    raw_sections = raw.get("sections")
    if isinstance(raw_sections, dict):
        for s in PERMISSION_SECTIONS:
            if s in raw_sections:
                perms["sections"][s] = bool(raw_sections[s])
    if "write" in raw:
        perms["write"] = bool(raw["write"])
    for k, v in raw.items():
        if k not in ("sections", "write"):
            perms[k] = v
    return perms


def denied_permissions() -> dict:
    """A permission map that grants nothing — used when the caller holds no
    membership for the request's tenant (deny, never fall back to the user row)."""
    return {"sections": {s: False for s in PERMISSION_SECTIONS}, "write": False}


def membership_for(db: Session, username: Optional[str],
                   tenant_id: Optional[int] = None):
    """(user_id, role, raw_permissions_dict) for `username` in `tenant_id`
    (default: the request's current tenant), or None when they hold no authority
    there.

    A direct membership wins. Otherwise the caller may still act in `tenant_id`
    through hierarchy traversal: if they hold owner/admin in one of its
    ANCESTORS, they act there with that role (phase 3 — access flows down the
    tree only, never up). Raw SQL: tenant_memberships/users/tenants are
    install-global tables."""
    if not username:
        return None
    tid = current_tenant() if tenant_id is None else int(tenant_id)
    row = db.execute(text(
        "SELECT u.id, m.role, m.permissions FROM tenant_memberships m "
        "JOIN users u ON u.id = m.user_id "
        "WHERE u.username = :u AND m.tenant_id = :t"),
        {"u": username, "t": tid}).fetchone()
    if not row:
        role = managed_ancestor_role(db, username, tid)
        if not role:
            return None
        user_row = db.execute(text("SELECT id FROM users WHERE username = :u"),
                              {"u": username}).fetchone()
        if not user_row:
            return None
        return int(user_row[0]), role, {}
    return int(row[0]), (row[1] or "viewer"), (row[2] or {})


def effective_permissions(db: Session, username: Optional[str],
                          tenant_id: Optional[int] = None) -> Optional[dict]:
    """The caller's effective permission map for `tenant_id`, or None when they
    hold no membership there."""
    m = membership_for(db, username, tenant_id)
    if not m:
        return None
    return resolve_membership_permissions(m[2], m[1])


def get_user_permissions(username: Optional[str]) -> dict:
    """Effective permissions for the request's current tenant. A missing
    membership (or an unauthenticated caller) grants nothing."""
    if not username:
        return denied_permissions()
    db = SessionLocal()
    try:
        return effective_permissions(db, username) or denied_permissions()
    finally:
        db.close()


def _is_self_service(request: Request) -> bool:
    """True for the account self-service endpoints under /api/users/me[...]."""
    path = request.scope.get("path", "")
    return path.rstrip("/").endswith("/me") or "/me/" in path


def require_section(section: str):
    """Dependency factory: caller must be authenticated AND, through their
    membership in the request's tenant, able to read `section`. The Bearer
    api_token principal is install-wide and skips this gate."""
    async def checker(request: Request, authorization: Optional[str] = Header(None)):
        principal = require_api_auth(request, authorization)
        if principal == "api_token":
            return principal
        # Self-service account endpoints (change password, 2FA) are for every user
        if section == "users" and _is_self_service(request):
            return principal
        perms = get_user_permissions(get_session_username(request))
        if not perms["sections"].get(section, False):
            raise HTTPException(status_code=403, detail=f"No access to section '{section}'")
        return principal
    return checker


def require_section_write(section: str):
    """Dependency factory: on top of section read access, the membership needs
    write=true for mutating methods. The api_token principal is install-wide."""
    async def checker(request: Request, authorization: Optional[str] = Header(None)):
        principal = await require_section(section)(request, authorization)
        if principal == "api_token":
            return principal
        if section == "users" and _is_self_service(request):
            return principal
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            perms = get_user_permissions(get_session_username(request))
            if not perms["write"]:
                raise HTTPException(status_code=403, detail="Write access denied")
        return principal
    return checker


def require_tenant_feature(name: str):
    """Dependency factory: deny when the current workspace switched a feature off.

    ``tenants.features`` is an opt-out map — an absent key means enabled, so
    every workspace that predates feature flags keeps working. A false flag
    makes the whole capability (API included) unavailable in that workspace.
    """
    async def checker(request: Request, authorization: Optional[str] = Header(None),
                      db: Session = Depends(get_db)):
        principal = require_api_auth(request, authorization)
        from tenant_settings import tenant_feature
        if not tenant_feature(db, current_tenant(), name):
            raise HTTPException(
                status_code=403,
                detail=f"'{name}' is disabled for this workspace")
        return principal
    return checker


def get_caller(request: Request, authorization: Optional[str] = None):
    """(username, is_admin) for the current request.

    A valid Bearer API token counts as an admin *of its own workspace* (it has
    no username). Platform-only gates must use ``is_platform_operator`` — the
    token is never a platform operator.
    """
    if _api_token_tenant() is not None:
        return None, True
    username = get_session_username(request)
    if not username:
        return None, False
    db = SessionLocal()
    try:
        user = get_user(db, username)
        return username, bool(user and user.is_admin)
    finally:
        db.close()


def request_is_https(request: Request) -> bool:
    """True when the client reached us over HTTPS (directly or via nginx).

    The session cookie must NOT be marked Secure on a plain-HTTP install: browsers
    silently drop Secure cookies over http, so the login would appear to succeed
    while every following request stays unauthenticated.
    """
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return request.url.scheme == "https"


# ====== POST /login ======
# In-process rate limiter: max 5 failed attempts per IP+username per rolling 60s
LOGIN_MAX_FAILED = 5
LOGIN_WINDOW_SECONDS = 60
_login_failures: dict = {}


def _record_failed_login(key: str):
    now = time.time()
    cutoff = now - LOGIN_WINDOW_SECONDS
    attempts = [t for t in _login_failures.get(key, []) if t > cutoff]
    attempts.append(now)
    _login_failures[key] = attempts


def _login_limited(key: str) -> bool:
    cutoff = time.time() - LOGIN_WINDOW_SECONDS
    return len([t for t in _login_failures.get(key, []) if t > cutoff]) >= LOGIN_MAX_FAILED


@router.post("/login")
async def login(request: Request, response: Response, login_data: LoginRequest, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    client_ip = _client_ip(request)
    allowed, reason = login_ip_allowed(db, client_ip)
    if not allowed:
        audit_event(login_data.username, "login_blocked_ip", "user", login_data.username,
                    {"reason": reason}, client_ip)
        raise HTTPException(status_code=403, detail=reason)
    limit_key = f"{client_ip}:{login_data.username}"
    if _login_limited(limit_key):
        raise HTTPException(status_code=429, detail="Too many failed login attempts — try again in a minute.")

    user = get_user(db, login_data.username)
    if not user:
        _record_failed_login(limit_key)
        audit_event(login_data.username, "login_failed", "user", login_data.username,
                    {"reason": "unknown_user"}, client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not verify_password(login_data.password, user.password_hash):
        _record_failed_login(limit_key)
        audit_event(login_data.username, "login_failed", "user", login_data.username,
                    {"reason": "bad_password"}, client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials 2")

    # Transparent upgrade from the legacy md5 hash to bcrypt
    if not is_bcrypt_hash(user.password_hash or ""):
        user.password_hash = hash_password(login_data.password)
        db.commit()

    # G62 — second factor required: hand back a short-lived TOTP token, NOT a session
    if user.totp_enabled and user.totp_secret:
        audit_event(user.username, "login_totp_required", "user", user.username, ip=client_ip)
        totp_token = jwt.encode(
            {"sub": user.username, "type": "totp", "jti": secrets.token_urlsafe(8),
             "exp": datetime.utcnow() + timedelta(minutes=TOTP_TOKEN_TTL_MINUTES)},
            SECRET_KEY, algorithm=ALGORITHM)
        return {"requires_totp": True, "totp_token": totp_token}

    token = create_session(user.username, client_ip, user_agent(request))
    _login_failures.pop(limit_key, None)
    audit_event(user.username, "login_success", "user", user.username, ip=client_ip)

    # Secure only when the request actually arrived over https, so http installs work.
    response.set_cookie(key="session_token", value=token, httponly=True,
                        samesite="lax", secure=request_is_https(request))

    return {"message": "Login successful"}


# ====== POST /login/totp (G62) ======
TOTP_TOKEN_TTL_MINUTES = 5
TOTP_MAX_TRIES = 3
TOTP_WINDOW_SECONDS = 600
_totp_failures: dict = {}
BACKUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


class TotpLoginRequest(BaseModel):
    totp_token: str
    code: str


def _record_totp_failure(key: str):
    now = time.time()
    cutoff = now - TOTP_WINDOW_SECONDS
    attempts = [t for t in _totp_failures.get(key, []) if t > cutoff]
    attempts.append(now)
    _totp_failures[key] = attempts


def _totp_limited(key: str) -> bool:
    cutoff = time.time() - TOTP_WINDOW_SECONDS
    return len([t for t in _totp_failures.get(key, []) if t > cutoff]) >= TOTP_MAX_TRIES


def _hash_backup_code(code: str) -> str:
    return hashlib.sha256(code.upper().encode()).hexdigest()


def _verify_totp_code(db: Session, user, code: str) -> bool:
    """Accepts a 6-digit TOTP code or a single-use 8-char backup code.

    Backup-code redemption takes a row lock on the user first, so two
    concurrent attempts with the same code serialize: the second sees
    used=true and is rejected (single-use, no double-spend race)."""
    code = (code or "").strip()
    if not code:
        return False
    # Backup codes: 8 chars, single-use (hash checked first — a 6-digit TOTP
    # code will never collide with a stored backup-code hash)
    locked = db.execute(
        text("SELECT totp_backup FROM users WHERE id = :id FOR UPDATE"),
        {"id": user.id}).fetchone()
    entries = [dict(e) for e in ((locked[0] if locked else None) or [])]
    digest = _hash_backup_code(code)
    for e in entries:
        if not e.get("used") and hmac.compare_digest(e.get("h") or "", digest):
            e["used"] = True
            db.execute(text("UPDATE users SET totp_backup = CAST(:b AS JSONB) "
                            "WHERE id = :id"),
                       {"b": json.dumps(entries), "id": user.id})
            db.commit()
            return True
    if len(code) == 8:
        return False  # shaped like a backup code but unknown/used
    # Standard TOTP: allow a one-step clock drift
    if not code.isdigit():
        return False
    return pyotp.TOTP(user.totp_secret).verify(code, valid_window=1)


@router.post("/login/totp")
async def login_totp(request: Request, response: Response, data: TotpLoginRequest, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    client_ip = _client_ip(request)
    allowed, reason = login_ip_allowed(db, client_ip)
    if not allowed:
        audit_event("", "login_blocked_ip", "user", "", {"reason": reason, "phase": "totp"},
                    client_ip)
        raise HTTPException(status_code=403, detail=reason)

    try:
        payload = jwt.decode(data.totp_token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired TOTP token")
    if payload.get("type") != "totp" or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid TOTP token")

    # Brute-force limiter keyed on username + client IP — NOT on the token.
    # A token is attacker-refreshable (a valid password mints a fresh one and
    # each token had its own 3-try budget before); this key survives every
    # token refresh for the full lockout window, so the 6-digit code keeps a
    # hard 3-try budget per user/IP. The per-token jti stays one-shot below.
    limit_key = f"{payload['sub']}:{client_ip}"
    if _totp_limited(limit_key):
        raise HTTPException(status_code=429, detail="Too many TOTP attempts — request a new code.")

    user = get_user(db, payload["sub"])
    if not user or not user.active or not user.totp_enabled or not user.totp_secret:
        raise HTTPException(status_code=401, detail="Invalid TOTP token")

    # Spend the one-shot jti BEFORE verifying the code: a replayed token is
    # rejected even if the attacker has a valid TOTP code.
    ensure_totp_token_table()
    if not _spend_totp_jti(db, payload.get("jti")):
        _record_totp_failure(limit_key)
        audit_event(user.username, "totp_replay", "user", user.username, ip=client_ip)
        raise HTTPException(status_code=401, detail="TOTP token already used")

    if not _verify_totp_code(db, user, data.code):
        _record_totp_failure(limit_key)
        audit_event(user.username, "totp_failed", "user", user.username, ip=client_ip)
        raise HTTPException(status_code=401, detail="Invalid code")

    token = create_session(user.username, client_ip, user_agent(request))
    _totp_failures.pop(limit_key, None)
    audit_event(user.username, "login_success", "user", user.username, {"totp": True}, client_ip)

    response.set_cookie(key="session_token", value=token, httponly=True,
                        samesite="lax", secure=request_is_https(request))
    return {"message": "Login successful"}


# ====== POST /logout ======
@router.post("/logout")
async def logout(request: Request, response: Response):
    from audit_logger import audit_event
    token = request.cookies.get("session_token")
    if token:
        try:
            revoke_session(token)
        except Exception:
            pass
    username = get_session_username(request)
    if username:
        audit_event(username, "logout", "user", username,
                    ip=request.client.host if request.client else "")
    response.delete_cookie(key="session_token")
    return {"message": "Logged out"}


# ====== GET /auth-status ======
# Renamed from /status: the G78 system-status API now owns /api/status
# (admin-gated). Nothing in the UI consumed this endpoint; it stays available
# under its explicit name for external session checks.
@router.get("/auth-status")
async def auth_status(request: Request):
    if is_authenticated(request):
        return {"authenticated": True}
    return {"authenticated": False}
