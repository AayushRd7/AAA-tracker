# app_pages/auth.py
"""Authentication, DB-backed sessions and workspace API tokens.

Security note on API tokens: a tenant's ``apiToken`` is stored **in plaintext**
inside its settings document, so anyone who can read the settings row (a DB
dump, a backup) holds a usable workspace credential. Moving to a stored hash is
the right end state, but hashing is only possible once the token is displayed
exactly once at creation/rotation — a show-once UX decision that is deliberately
out of scope here. ``rotate_api_token`` gives operators a way to replace a token.
"""
from fastapi import APIRouter, Request, Response, HTTPException, status, Depends, Header
from pydantic import BaseModel

from sqlalchemy.orm import Session
from jose import jwt, JWTError
from typing import Optional
from db import get_db, get_user, SessionLocal, POSTGRES_PASSWORD
from hashlib import md5
from datetime import datetime, timedelta, timezone
import secrets
import json
import re
import time
import os
import logging

import bcrypt
import pyotp
import hashlib
import hmac
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from models.user import UserORM
from models.settings import SettingsORM
from rate_limit import Throttle
from tenant_context import current_tenant, api_token_tenant as _api_token_tenant
# Aggregate metrics + derived rates a workspace may hide per user. Sourced from
# the one place the report engine defines them so the two can never drift.
from clickHouse import BASE_METRICS, FORMULA_METRIC_KEYS

router = APIRouter()

log = logging.getLogger(__name__)


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

# JWT configuration (kept for compatibility; sessions are now DB-backed).
# A known constant fallback would let anyone who reads the source forge TOTP
# pre-tokens, so it is gone. When JWT_SECRET is unset we derive a stable,
# per-deployment secret from POSTGRES_PASSWORD: deterministic across workers
# (TOTP pre-tokens minted by one worker must verify on another) yet not a
# value published in the repo. Set JWT_SECRET explicitly in production.
_JWT_SECRET_ENV = os.environ.get("JWT_SECRET")
if _JWT_SECRET_ENV:
    SECRET_KEY = _JWT_SECRET_ENV
else:
    SECRET_KEY = hashlib.sha256(
        ("aaa-jwt:" + (POSTGRES_PASSWORD or "")).encode()).hexdigest()
    log.warning(
        "JWT_SECRET is not set — deriving the JWT signing key from "
        "POSTGRES_PASSWORD. This is stable across workers but weaker than a "
        "dedicated secret; set JWT_SECRET explicitly.")
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


# Every place a password is set — platform create, admin set, self-service
# change, invitation accept, reset — goes through this one rule, so the minimum
# cannot drift between endpoints. The UI labels quote MIN_PASSWORD_LENGTH.
MIN_PASSWORD_LENGTH = 8


def validate_password(password: str) -> str:
    """Reject an empty or too-short password with a 400. Returns the password so
    callers can write `hash_password(validate_password(pw))`."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


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
    # Visitor location, sourced from Cloudflare's CF-* headers (see nginx
    # pass-through). Nullable on purpose: only Cloudflare-proxied requests carry
    # it, so a direct/IP-only install simply has no location.
    db.execute(text("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS "
                    "country VARCHAR(8)"))
    db.execute(text("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS "
                    "region VARCHAR(64)"))
    db.execute(text("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS "
                    "city VARCHAR(128)"))
    # Multi-tenancy phase 1 — the tenant this session is currently working in.
    # NULL means "never switched": the resolver treats it as tenant 1.
    db.execute(text("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS "
                    "current_tenant_id INTEGER"))
    db.commit()
    db.close()
    _sessions_table_ready = True


def _hash_session_token(token: str) -> str:
    """The value stored in auth_sessions.token for a raw cookie token."""
    return hashlib.sha256((token or "").encode()).hexdigest()


def _fetch_session(db: Session, token: str, columns: str):
    """Find a session row by its raw cookie token.

    New rows store ``sha256(token)``; a row written before that change still
    holds the raw token. A hash lookup is tried first, then a legacy plaintext
    lookup — on a plaintext hit the row is rewritten to the hash
    (migrate-on-read) so existing sessions survive the transition. ``columns``
    is a fixed, internal column list, never caller input.
    """
    hashed = _hash_session_token(token)
    row = db.execute(text(f"SELECT {columns} FROM auth_sessions WHERE token = :t"),
                     {"t": hashed}).fetchone()
    if row is not None:
        return row
    row = db.execute(text(f"SELECT {columns} FROM auth_sessions WHERE token = :t"),
                     {"t": token}).fetchone()
    if row is None:
        return None
    try:
        db.execute(text("UPDATE auth_sessions SET token = :h WHERE token = :t"),
                   {"h": hashed, "t": token})
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    return row


def create_session(username: str, ip: str = "", user_agent: str = "",
                   country: Optional[str] = None, region: Optional[str] = None,
                   city: Optional[str] = None) -> str:
    """Create a session and return the RAW token for the cookie; only its
    sha256 digest is persisted."""
    ensure_sessions_table()
    token = secrets.token_urlsafe(32)
    db = SessionLocal()
    db.execute(text(
        "INSERT INTO auth_sessions (token, username, created_at, last_seen, "
        "expires_at, ip, user_agent, country, region, city) "
        "VALUES (:t, :u, now(), now(), now() + make_interval(days => :days), "
        ":ip, :ua, :cc, :rg, :ct)"),
        {"t": _hash_session_token(token), "u": username, "days": SESSION_TTL_DAYS,
         "ip": (ip or "")[:64], "ua": (user_agent or "")[:512],
         "cc": country, "rg": region, "ct": city})
    db.commit()
    db.close()
    return token


def revoke_session(token: str):
    ensure_sessions_table()
    db = SessionLocal()
    # Match either the stored hash or a legacy plaintext row.
    db.execute(text("DELETE FROM auth_sessions WHERE token = :h OR token = :t"),
               {"h": _hash_session_token(token), "t": token})
    db.commit()
    db.close()


def session_token(request: Request) -> str:
    """The raw session cookie for the current request ('' when absent)."""
    return request.cookies.get("session_token") or ""


def user_agent(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:512]


# CF-IPCountry is a 2-letter ISO code; "XX" is Cloudflare's "unknown".
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_LOCATION_RE = re.compile(r"^[\w .,'()\-]{1,128}$")


def _clean_country(value) -> Optional[str]:
    cc = (value or "").strip().upper()
    if not _COUNTRY_RE.match(cc) or cc == "XX":
        return None
    return cc


def _clean_location(value) -> Optional[str]:
    v = (value or "").strip()
    # Only a plausible region/city string is stored; anything else (empty,
    # over-long, punctuation-heavy) is dropped rather than persisted.
    return v if _LOCATION_RE.match(v) else None


def client_location(request: Request) -> tuple:
    """(country, region, city) from Cloudflare's CF-* headers, validated.

    nginx forwards these only from a trusted Cloudflare peer (see
    _realip_cloudflare.conf), so a direct client cannot forge them. Validation
    here is belt-and-braces: a missing or garbage header yields None and must
    never break session creation.
    """
    return (_clean_country(request.headers.get("cf-ipcountry")),
            _clean_location(request.headers.get("cf-region")),
            _clean_location(request.headers.get("cf-ipcity")))


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
    """One session row shaped for the UI (never includes the full token).

    The stored token is a sha256 digest, so a session's ``id``/``token_prefix``
    is the digest's first 12 chars — that is exactly what revoke-by-prefix
    matches against. The caller's *own* row is the exception: it is labelled
    with the raw cookie's first 12 chars, which keeps the "use log out to end
    the current session" guard in the users page working (it compares the id to
    the cookie prefix). ``current`` also recognises a legacy plaintext row.
    """
    token = row[0] or ""
    ua = row[4] or ""
    is_current = bool(current_token and (
        token == _hash_session_token(current_token) or token == current_token))
    prefix = (current_token[:12] if is_current and current_token
              else token[:12])
    return {
        "id": prefix,
        "token_prefix": prefix,
        "created_at": row[1].isoformat() if row[1] else None,
        "last_seen": row[2].isoformat() if row[2] else None,
        "ip": row[3] or "",
        "user_agent": ua,
        # Additive: older sessions have NULL location, returned as empty strings.
        "country": row[5] or "",
        "region": row[6] or "",
        "city": row[7] or "",
        "current": is_current,
        **parse_user_agent(ua),
    }


def list_sessions(db: Session, username: str, current_token: str = "") -> list:
    """Active (non-revoked, unexpired) sessions for a username, newest first."""
    rows = db.execute(text(
        "SELECT token, created_at, last_seen, ip, user_agent, "
        "country, region, city FROM auth_sessions "
        "WHERE username = :u AND revoked = false "
        "AND (expires_at IS NULL OR expires_at >= now()) "
        "ORDER BY created_at DESC NULLS LAST"),
        {"u": username}).fetchall()
    return [_session_public(r, current_token) for r in rows]


def revoke_session_prefix(db: Session, username: str, prefix: str) -> int:
    """Delete the given user's session whose stored token starts with `prefix`."""
    prefix = (prefix or "").strip()
    if not prefix:
        return 0
    # Escape LIKE metacharacters: a prefix of "%" would otherwise wipe every
    # session the user has, not just the one that starts with "%".
    escaped = (prefix.replace("\\", "\\\\")
                     .replace("%", "\\%")
                     .replace("_", "\\_"))
    res = db.execute(text(
        "DELETE FROM auth_sessions WHERE username = :u AND token LIKE :p ESCAPE '\\'"),
        {"u": username, "p": escaped + "%"})
    db.commit()
    return res.rowcount or 0


def revoke_other_sessions(db: Session, username: str, current_token: str) -> int:
    """Delete every session of `username` except the caller's current one.

    The stored token is a hash, so the caller's raw cookie is hashed before the
    comparison; a legacy plaintext row is also spared by matching the raw value.
    """
    res = db.execute(text(
        "DELETE FROM auth_sessions WHERE username = :u "
        "AND token != :h AND token != :t"),
        {"u": username, "h": _hash_session_token(current_token or ""),
         "t": current_token or ""})
    db.commit()
    return res.rowcount or 0


# ====== Workspace API tokens ======
# A token lives in its tenant's settings document as ``apiToken``. Resolving a
# Bearer used to JSON-parse every tenant's settings row on every request; the
# parsed map is now cached in-process for a short TTL and refreshed on a miss
# (so a freshly written token is picked up without waiting out the TTL).
#
# Optional per-token fields, all additive (absent = today's full-workspace
# behaviour):
#   apiTokenScopes         list of section names the token may touch
#   apiTokenExpiresAt      ISO-8601 instant after which the token is dead
#   apiTokenHiddenMetrics  metric keys hidden from this token's responses
_API_TOKEN_CACHE_TTL = 30.0
_api_token_map: Optional[dict] = None
_api_token_map_at = 0.0


def _bearer_token(authorization: Optional[str]) -> str:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _parse_api_token_expiry(value) -> Optional[datetime]:
    """Timezone-aware expiry from an ISO-8601 string (or epoch seconds), or None."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text_value = str(value).strip()
    if text_value.endswith("Z"):
        text_value = text_value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize_api_token_scopes(value) -> Optional[list]:
    """A non-empty list of section names, or None for full-workspace access."""
    if not isinstance(value, (list, tuple)):
        return None
    scopes = [str(s).strip() for s in value if str(s).strip()]
    return scopes or None


def _normalize_api_token_hidden(value) -> frozenset:
    """Known hidden-metric keys from the settings doc, unknown entries dropped.

    Unlike the membership path (which rejects a bad key outright), a typo in an
    ops-authored settings doc must not silently widen the token's view: the
    valid restrictions are kept."""
    if not isinstance(value, (list, tuple)):
        return frozenset()
    out = []
    for raw in value:
        key = str(raw or "").strip()
        if key in HIDDEN_METRIC_KEYS and key not in out:
            out.append(key)
    return frozenset(out)


def _build_api_token_map() -> dict:
    """Parse every tenant's apiToken (plus optional scope/expiry fields).

    Returns {token: entry}. A token configured for more than one tenant is
    ambiguous and deliberately excluded, with an error log. Expired tokens are
    kept in the map with their expiry so the check stays precise within the
    cache TTL.
    """
    db = SessionLocal()
    try:
        rows = db.execute(text(
            "SELECT tenant_id, value FROM settings WHERE name = 'settings' "
            "ORDER BY tenant_id ASC")).fetchall()
    finally:
        db.close()
    seen: dict = {}
    info: dict = {}
    collisions = set()
    for tenant_id, value in rows:
        if not value:
            continue
        try:
            doc = json.loads(value)
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        token = (doc.get("apiToken") or "").strip()
        if not token:
            continue
        tid = int(tenant_id)
        if token in seen and seen[token] != tid:
            collisions.add(token)
        seen[token] = tid
        info[token] = doc
    out = {}
    for token, doc in info.items():
        if token in collisions:
            log.error("API token collision: a token is configured for multiple "
                      "tenants; it will not resolve to any workspace")
            continue
        out[token] = {
            "tenant": int(seen[token]),
            "scopes": _normalize_api_token_scopes(doc.get("apiTokenScopes")),
            "hidden": _normalize_api_token_hidden(doc.get("apiTokenHiddenMetrics")),
            "expires_at": _parse_api_token_expiry(doc.get("apiTokenExpiresAt")),
        }
    return out


def _refresh_api_token_map() -> dict:
    """Rebuild and cache the token map. A DB error keeps the previous map."""
    global _api_token_map, _api_token_map_at
    try:
        _api_token_map = _build_api_token_map()
        _api_token_map_at = time.time()
    except Exception as e:  # noqa: BLE001 — a bad DB must not 500 the request
        log.warning("could not refresh API token cache: %s", e)
        if _api_token_map is None:
            _api_token_map = {}
    return _api_token_map


def _invalidate_api_token_cache() -> None:
    global _api_token_map_at
    _api_token_map_at = 0.0


def _match_api_token(provided: str) -> Optional[dict]:
    """The cache entry for ``provided``, using a constant-time compare.

    The map is short (one entry per workspace), so a linear scan of
    ``secrets.compare_digest`` calls is cheap and avoids leaking token bytes
    through a plain dict lookup's early exit. A miss triggers one refresh so a
    just-created token resolves immediately.
    """
    provided = (provided or "").strip()
    if not provided:
        return None
    global _api_token_map
    if _api_token_map is None or (time.time() - _api_token_map_at) >= _API_TOKEN_CACHE_TTL:
        _refresh_api_token_map()
    entry = _find_in_map(_api_token_map, provided)
    if entry is None:
        entry = _find_in_map(_refresh_api_token_map(), provided)
    if entry is None:
        return None
    expires_at = entry.get("expires_at")
    if expires_at is not None and datetime.now(timezone.utc) > expires_at:
        return None
    return entry


def _find_in_map(token_map: dict, provided: str) -> Optional[dict]:
    for token, entry in (token_map or {}).items():
        if token and len(token) == len(provided) \
                and secrets.compare_digest(token, provided):
            return entry
    return None


def resolve_api_token(provided: str) -> Optional[int]:
    """The tenant whose settings document holds ``provided`` as its apiToken.

    Returns None for an empty/unknown token, for a token held by more than one
    workspace, and for a token past its optional ``apiTokenExpiresAt``. An API
    token is a *workspace-scoped* credential (never install-wide); a collision
    cannot be attributed to one tenant, so it is refused rather than silently
    resolving to the lowest tenant id.
    """
    entry = _match_api_token(provided)
    return entry["tenant"] if entry else None


def api_token_scopes(provided: str) -> Optional[list]:
    """Section scopes configured on ``provided``, or None for full access.

    An empty/absent ``apiTokenScopes`` list is None, i.e. today's full-workspace
    token behaviour."""
    entry = _match_api_token(provided)
    return entry["scopes"] if entry else None


def api_token_hidden_metrics(provided: str) -> frozenset:
    """Metric keys hidden from ``provided`` (``apiTokenHiddenMetrics``)."""
    entry = _match_api_token(provided)
    return entry["hidden"] if entry else frozenset()


def rotate_api_token(db: Session, tenant_id: int) -> str:
    """Mint a fresh ``apiToken`` for ``tenant_id`` and return it (raw).

    Clears the optional apiTokenExpiresAt/Scopes/HiddenMetrics so the new token
    starts unexpired with full-workspace access. The in-process cache is
    invalidated so the change is visible immediately. The token is only returned
    here — it is stored plaintext at rest (see the module docstring)."""
    tid = int(tenant_id)
    token = secrets.token_urlsafe(24)
    row = db.execute(text(
        "SELECT value FROM settings WHERE name = 'settings' AND tenant_id = :t"),
        {"t": tid}).fetchone()
    doc = {}
    if row and row[0]:
        try:
            parsed = json.loads(row[0])
            if isinstance(parsed, dict):
                doc = parsed
        except Exception:
            doc = {}
    doc["apiToken"] = token
    for key in ("apiTokenExpiresAt", "apiTokenScopes", "apiTokenHiddenMetrics"):
        doc.pop(key, None)
    if row:
        db.execute(text("UPDATE settings SET value = :v WHERE name = 'settings' "
                        "AND tenant_id = :t"), {"v": json.dumps(doc), "t": tid})
    else:
        db.execute(text("INSERT INTO settings (name, value, tenant_id) "
                        "VALUES ('settings', :v, :t)"),
                   {"v": json.dumps(doc), "t": tid})
    db.commit()
    _invalidate_api_token_cache()
    return token


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
        row = _fetch_session(
            db, token,
            "username, revoked, "
            "(expires_at IS NOT NULL AND expires_at < now()) AS expired, "
            "(last_seen IS NULL OR last_seen < now() - interval '60 seconds') AS stale")
        if not row or row.revoked or row.expired:
            db.close()
            return False

        # Refresh last_seen, throttled to at most once per ~60s per session so
        # the hot auth path stays cheap. Never fatal — a failed touch must not
        # break the request.
        try:
            if row.stale:
                db.execute(text("UPDATE auth_sessions SET last_seen = now() "
                                "WHERE token = :t"), {"t": _hash_session_token(token)})
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
        row = _fetch_session(
            db, token,
            "username, revoked, "
            "(expires_at IS NOT NULL AND expires_at < now()) AS expired")
        db.close()
        if not row or row.revoked or row.expired:
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
        row = _fetch_session(db, token, "current_tenant_id")
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
            row = _fetch_session(
                db, token,
                "username, revoked, "
                "(expires_at IS NOT NULL AND expires_at < now()) AS expired, "
                "current_tenant_id")
            if not row or row.revoked or row.expired:
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
                                "WHERE token = :t"),
                           {"tid": tenant_id, "t": _hash_session_token(token)})
                db.commit()
            return tenant_id, True
        finally:
            db.close()
    except Exception:
        # Never let tenant resolution break a request: fall back to tenant 1.
        return None, True


def set_session_tenant(db: Session, token: str, tenant_id: int) -> None:
    db.execute(text("UPDATE auth_sessions SET current_tenant_id = :tid WHERE token = :t"),
               {"tid": int(tenant_id), "t": _hash_session_token(token)})
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
                       "integrations", "capi-integrations", "bot-rules", "rules",
                       "filter-presets", "fallback", "funnels",
                       "acquisition", "creative-analytics", "copilot"]
ADMIN_ONLY_SECTIONS = {"users", "settings", "domains", "fraud", "optimizer",
                       "conversion-tracking", "logs", "scripts",
                       "integrations", "capi-integrations", "bot-rules", "rules",
                       "fallback", "copilot"}

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


# Sections that support a per-resource 'own' scope. The value lives as a
# top-level key on the permissions blob ({"offers": "own"}), the shape
# campaigns already uses; `resolve_membership_permissions` carries it through
# untouched. An absent key means full access, so existing blobs are unaffected.
OWN_SCOPE_SECTIONS = ("campaigns", "offers", "sources", "affiliates", "domains")


def validate_permission_scopes(raw: Optional[dict]) -> Optional[dict]:
    """Reject an unknown owner-scope value on a permissions write.

    A scope key, when present, must be exactly 'own' — anything else ('all',
    a boolean, a typo) is refused so a caller can never silently widen or
    half-apply a scope. Absent keys mean full access. Returns the input
    unchanged; raises ValueError so the endpoint answers with a clear 400."""
    if not isinstance(raw, dict):
        return raw
    for section in OWN_SCOPE_SECTIONS:
        if section in raw and raw[section] != "own":
            raise ValueError(
                f"{section} scope must be 'own' when set (got {raw[section]!r})")
    return raw


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


# ====== Per-user metric restrictions ======
# Where the hidden list lives: on the *membership* record
# (tenant_memberships.permissions), stored as the extra `hidden_metrics` key
# alongside sections/write. The membership is the right home because phase 2A
# resolves authority from it (effective_permissions/membership_for), the
# restriction is scoped to ONE workspace, and resolve_membership_permissions
# already carries unknown extra keys (like campaigns:'own') through. The
# users.permissions mirror is written by the same endpoints so the Users editor
# can render the value, but enforcement never reads it.

# The full set of hideable keys: aggregate metrics + derived rates. Kept in a
# stable order (base metrics first, then the extras). The Users-page picker in
# users.html mirrors this set (key + label pairs).
HIDDEN_METRIC_KEYS = tuple(dict.fromkeys(list(BASE_METRICS) + sorted(FORMULA_METRIC_KEYS)))

HIDDEN_METRICS_MAX = len(HIDDEN_METRIC_KEYS)

# A quantity can also appear under a source-column name: a conversion's `payout`
# is the same figure as its `revenue`, so hiding revenue must hide payout too or
# the value leaks through the other column.
METRIC_KEY_ALIASES = {
    "revenue": ("payout",),
}


def normalize_hidden_metrics(value) -> list:
    """Validate a hidden-metrics list: deduped, order-preserving, known metric
    keys only, capped at HIDDEN_METRICS_MAX. Raises ValueError on anything else
    so the API can answer with a clear 400. None / [] normalize to []."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("hidden_metrics must be a list of metric keys")
    if len(value) > HIDDEN_METRICS_MAX:
        raise ValueError(f"hidden_metrics may contain at most {HIDDEN_METRICS_MAX} keys")
    cleaned = []
    for raw in value:
        key = str(raw or "").strip()
        if key not in HIDDEN_METRIC_KEYS:
            raise ValueError(f"Unknown metric key: {key!r}")
        if key not in cleaned:
            cleaned.append(key)
    return cleaned


def _expand_metric_aliases(keys) -> set:
    """Hidden keys plus any source-column aliases they cover."""
    out = set(keys or ())
    for k in list(out):
        out.update(METRIC_KEY_ALIASES.get(k, ()))
    return out


def strip_hidden_metrics(row, hidden):
    """Remove hidden metric keys (and their column aliases) from a dict row.
    Returns the row so callers can chain; a no-op when nothing is hidden."""
    if not hidden or not isinstance(row, dict):
        return row
    for key in _expand_metric_aliases(hidden):
        row.pop(key, None)
    return row


def strip_hidden_metrics_rows(rows, hidden):
    """strip_hidden_metrics over every dict in a list (returns the list)."""
    if isinstance(rows, list):
        for row in rows:
            strip_hidden_metrics(row, hidden)
    return rows


def filter_hidden_fields(fields, hidden):
    """The subset of an ordered export-field list that is safe to emit —
    drops hidden metric keys and their aliases. Used by the CSV exports so the
    header and every row lose the column together."""
    blocked = _expand_metric_aliases(hidden)
    return [f for f in fields if f not in blocked]


def hidden_metrics_for(db: Session, username: Optional[str],
                       tenant_id: Optional[int] = None) -> frozenset:
    """The metric keys hidden from `username` in `tenant_id` (default: the
    request's current tenant).

    Admins and owners are never restricted: the platform-admin flag and the
    manager roles (owner/admin) short-circuit to an empty set, so a stored list
    can never blank out a manager's own numbers. Unknown/stale keys are dropped
    defensively instead of trusted."""
    if not username:
        return frozenset()
    user = get_user(db, username)
    if user is not None and user.is_admin:
        return frozenset()
    tid = current_tenant() if tenant_id is None else int(tenant_id)
    if effective_tenant_role(db, username, tid) in MANAGER_ROLES:
        return frozenset()
    perms = effective_permissions(db, username, tid)
    if not perms:
        return frozenset()
    raw = perms.get("hidden_metrics")
    if not raw:
        return frozenset()
    return frozenset(
        str(k).strip() for k in raw if str(k).strip() in HIDDEN_METRIC_KEYS)


def request_hidden_metrics(request: Request, db: Optional[Session] = None) -> frozenset:
    """hidden_metrics_for the current caller. Pass the endpoint's own `db` to
    avoid a second pool checkout.

    A Bearer API token has no user membership, so its restrictions come from the
    tenant's optional ``apiTokenHiddenMetrics`` list instead (absent = none)."""
    if _api_token_tenant() is not None:
        return api_token_hidden_metrics(
            _bearer_token(request.headers.get("authorization")))
    username = get_session_username(request)
    if not username:
        return frozenset()
    own = db is None
    db = db or SessionLocal()
    try:
        return hidden_metrics_for(db, username)
    finally:
        if own:
            db.close()


def _is_self_service(request: Request) -> bool:
    """True for the account self-service endpoints under /api/users/me[...]."""
    path = request.scope.get("path", "")
    return path.rstrip("/").endswith("/me") or "/me/" in path


def require_section(section: str):
    """Dependency factory: caller must be authenticated AND, through their
    membership in the request's tenant, able to read `section`.

    A Bearer API token acts as an owner of its own workspace, but when the
    tenant configured ``apiTokenScopes`` the token may only touch the listed
    sections. An empty/absent scope list keeps the full-workspace behaviour
    existing tokens rely on."""
    async def checker(request: Request, authorization: Optional[str] = Header(None)):
        principal = require_api_auth(request, authorization)
        if principal == "api_token":
            scopes = api_token_scopes(_bearer_token(authorization))
            if scopes and section not in scopes:
                raise HTTPException(
                    status_code=403,
                    detail=f"API token is not scoped for section '{section}'")
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
    write=true for mutating methods. A scoped Bearer keeps read+write on its
    listed sections (see ``require_section``); an unscoped one is install-wide."""
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


def owner_scope_query(request: Request, db: Session, query, section: str, model):
    """Apply the per-resource 'own' scope to an ORM query: a caller whose
    membership carries e.g. offers:'own' only sees rows they own. Admins, API
    tokens, a caller with no membership, and anyone without the scope key are
    unaffected — so an existing install sees everything exactly as before."""
    username, is_admin = get_caller(request)
    if is_admin or not username:
        return query
    member = membership_for(db, username)
    raw = member[2] if member and isinstance(member[2], dict) else {}
    if member and raw.get(section) == "own":
        query = query.filter(model.owner_id == member[0])
    return query


def require_owned_mutation(request: Request, db: Session, section: str, rows, label: str) -> None:
    """Refuse mutating a row the caller does not own when their membership
    carries `section:'own'` — the campaigns idiom, enforced on every mutation
    path. Raises 403 (never a silent no-op); admins and unscoped callers pass."""
    username, is_admin = get_caller(request)
    if is_admin or not username:
        return
    member = membership_for(db, username)
    raw = member[2] if member and isinstance(member[2], dict) else {}
    if not member or raw.get(section) != "own":
        return
    for row in rows:
        if row is not None and row.owner_id != member[0]:
            raise HTTPException(status_code=403,
                                detail=f"You can only modify your own {label}")


def owner_id_for_create(request: Request, db: Session, section: str) -> Optional[int]:
    """The owner_id to stamp on a newly created row: the caller's user id when
    they hold `section:'own'`, otherwise None (unassigned, visible to managers
    only)."""
    username, is_admin = get_caller(request)
    if is_admin or not username:
        return None
    member = membership_for(db, username)
    raw = member[2] if member and isinstance(member[2], dict) else {}
    if member and raw.get(section) == "own":
        return member[0]
    return None


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
# Max 5 failed attempts per IP+username per rolling 60s, shared across uvicorn
# workers via the auth_throttle table (rate_limit.Throttle falls back to an
# in-process dict, with stale-key eviction, if the DB is unreachable).
LOGIN_MAX_FAILED = 5
LOGIN_WINDOW_SECONDS = 60
# Cap on the (attacker-chosen) failure map; above this we evict stale keys.
_LOGIN_FAILURES_MAX_KEYS = 10_000
_login_limiter = Throttle("login", LOGIN_WINDOW_SECONDS, LOGIN_MAX_FAILED,
                          max_keys=_LOGIN_FAILURES_MAX_KEYS)


def _record_failed_login(key: str):
    _login_limiter.record(key)


def _login_limited(key: str) -> bool:
    return _login_limiter.limited(key)


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
        # Same message as the unknown-user branch: a distinct one is an oracle
        # that tells an attacker which usernames exist.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

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

    country, region, city = client_location(request)
    token = create_session(user.username, client_ip, user_agent(request),
                           country, region, city)
    _login_limiter.clear(limit_key)
    audit_event(user.username, "login_success", "user", user.username, ip=client_ip)

    # Secure only when the request actually arrived over https, so http installs work.
    response.set_cookie(key="session_token", value=token, httponly=True,
                        samesite="lax", secure=request_is_https(request))

    return {"message": "Login successful"}


# ====== POST /login/totp (G62) ======
TOTP_TOKEN_TTL_MINUTES = 5
TOTP_MAX_TRIES = 3
TOTP_WINDOW_SECONDS = 600
# Shared across workers, same as the login limiter.
_totp_limiter = Throttle("totp", TOTP_WINDOW_SECONDS, TOTP_MAX_TRIES)
BACKUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I


class TotpLoginRequest(BaseModel):
    totp_token: str
    code: str


def _record_totp_failure(key: str):
    _totp_limiter.record(key)


def _totp_limited(key: str) -> bool:
    return _totp_limiter.limited(key)


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

    country, region, city = client_location(request)
    token = create_session(user.username, client_ip, user_agent(request),
                           country, region, city)
    _totp_limiter.clear(limit_key)
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


# ====== Password reset ======
# The raw token is generated at request time and handed to nobody: only its
# SHA-256 hash is stored (mirrors tenant_invitations), so a database dump cannot
# be replayed as a credential. `forgot-password` answers identically whether or
# not the account exists, so it cannot be used to enumerate accounts. A reset
# spends every outstanding token for that user and revokes their sessions.
_resets_table_ready = False
RESET_TTL_MINUTES = 60


def ensure_password_resets_table():
    global _resets_table_ready
    if _resets_table_ready:
        return
    db = SessionLocal()
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP
        )
    """))
    db.execute(text("CREATE INDEX IF NOT EXISTS password_resets_hash_idx "
                    "ON password_resets (token_hash)"))
    db.commit()
    db.close()
    _resets_table_ready = True


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def _reset_url(request: Request, token: str) -> str:
    """The sign-in page carrying the reset token — same origin resolution as the
    invitation accept URL (configured public origin, else the proxied request)."""
    query = f"/auth?reset={token}"
    base = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base:
        return f"{base}/backend{query}"
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if not proto:
        proto = request.url.scheme
    host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if not host:
        host = (request.headers.get("host") or request.url.netloc or "").strip()
    root = (request.scope.get("root_path") or "").rstrip("/") or "/backend"
    return f"{proto}://{host}{root}{query}"


class ForgotPasswordRequest(BaseModel):
    email: Optional[str] = None
    username: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


@router.post("/forgot-password")
def forgot_password(data: ForgotPasswordRequest, request: Request):
    """PUBLIC. Always answers the same way, whether or not the account exists."""
    ensure_password_resets_table()
    ident = (data.email or data.username or "").strip()
    generic = {"ok": True,
               "message": "If that account exists, a reset link has been sent."}
    if not ident:
        return generic
    db = SessionLocal()
    reset_token = None
    recipient = None
    try:
        row = db.execute(text(
            "SELECT id, username, email FROM users WHERE "
            "(lower(coalesce(email, '')) = lower(:i)) OR (username = :i) LIMIT 1"),
            {"i": ident}).fetchone()
        if row and row[2] and row[2].strip():
            reset_token = secrets.token_urlsafe(32)
            recipient = row[2].strip()
            db.execute(text(
                "INSERT INTO password_resets (user_id, token_hash, expires_at) "
                "VALUES (:u, :h, now() + make_interval(mins => :m))"),
                {"u": int(row[0]), "h": _hash_reset_token(reset_token),
                 "m": RESET_TTL_MINUTES})
            db.commit()
    except Exception:
        log.warning("password reset request failed", exc_info=True)
        return generic
    finally:
        db.close()
    if reset_token and recipient:
        try:
            from email_reports import send_email
            from env_config import (account_email_from_address, email_configured,
                                    resolve_email_config)
            cfg = resolve_email_config({})
            if email_configured(cfg):
                link = _reset_url(request, reset_token)
                send_email(
                    cfg, "Reset your AAA Tracker password",
                    "<p>Use the link below to choose a new password. It expires in "
                    f"{RESET_TTL_MINUTES} minutes and can be used once.</p>"
                    f'<p><a href="{link}">Reset my password</a></p>'
                    '<p style="color:#666;font-size:12px">If you did not request this, '
                    "you can safely ignore this email.</p>",
                    [recipient],
                    from_email=account_email_from_address())
        except Exception:
            log.warning("password reset email failed", exc_info=True)
    return generic


@router.post("/reset-password")
def reset_password(data: ResetPasswordRequest):
    """PUBLIC. Consume a reset token and set a new password."""
    ensure_password_resets_table()
    token = (data.token or "").strip()
    password = data.password or ""
    if not token:
        raise HTTPException(status_code=400, detail="token is required")
    validate_password(password)
    db = SessionLocal()
    try:
        row = db.execute(text(
            "SELECT id, user_id FROM password_resets WHERE token_hash = :h "
            "AND used_at IS NULL AND expires_at > now()"),
            {"h": _hash_reset_token(token)}).fetchone()
        if not row:
            raise HTTPException(status_code=410,
                                detail="This reset link is invalid or has expired")
        user_id = int(row[1])
        db.execute(text("UPDATE users SET password_hash = :p WHERE id = :u"),
                   {"p": hash_password(password), "u": user_id})
        # Spend every outstanding reset for this user, not just the one presented.
        db.execute(text("UPDATE password_resets SET used_at = now() "
                        "WHERE user_id = :u AND used_at IS NULL"), {"u": user_id})
        # A password change invalidates every existing session for the account.
        try:
            ensure_sessions_table()
            db.execute(text(
                "UPDATE auth_sessions SET revoked = true WHERE username = "
                "(SELECT username FROM users WHERE id = :u)"), {"u": user_id})
        except Exception:
            log.warning("could not revoke sessions after password reset", exc_info=True)
        db.commit()
    finally:
        db.close()
    return {"ok": True, "message": "Password updated — you can sign in now."}
