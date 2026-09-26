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
import time
import os

import bcrypt
import pyotp
import hashlib
import hmac
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from models.user import UserORM

router = APIRouter()

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
            revoked BOOLEAN NOT NULL DEFAULT false
        )
    """))
    db.commit()
    db.close()
    _sessions_table_ready = True


def create_session(username: str) -> str:
    ensure_sessions_table()
    token = secrets.token_urlsafe(32)
    db = SessionLocal()
    db.execute(text(
        "INSERT INTO auth_sessions (token, username, expires_at) "
        "VALUES (:t, :u, :e)"),
        {"t": token, "u": username,
         "e": datetime.utcnow() + timedelta(days=SESSION_TTL_DAYS)})
    db.commit()
    db.close()
    return token


def revoke_session(token: str):
    ensure_sessions_table()
    db = SessionLocal()
    db.execute(text("DELETE FROM auth_sessions WHERE token = :t"), {"t": token})
    db.commit()
    db.close()


def load_api_token() -> str:
    """The API token from the settings row (used for Bearer auth by external tools)."""
    try:
        db = SessionLocal()
        row = db.execute(text("SELECT value FROM settings WHERE name = 'settings'")).fetchone()
        db.close()
        if row and row[0]:
            return (json.loads(row[0]).get("apiToken") or "").strip()
    except Exception:
        pass
    return ""


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
            "SELECT username, expires_at, revoked FROM auth_sessions WHERE token = :t"),
            {"t": token}).fetchone()
        if not row or row.revoked:
            db.close()
            return False
        if row.expires_at and row.expires_at < datetime.utcnow():
            db.close()
            return False

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


def require_api_auth(request: Request, authorization: Optional[str] = Header(None)):
    """Dependency for API routers: accepts a session cookie or a Bearer API token."""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization.split(" ", 1)[1].strip()
        api_token = load_api_token()
        if api_token and len(provided) == len(api_token) \
                and secrets.compare_digest(provided, api_token):
            return "api_token"
        raise HTTPException(status_code=401, detail="Invalid API token")

    user_type = is_authenticated(request)
    if not user_type:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_type


# ====== G63: per-resource permissions ======
# Sections a non-admin user can read by default (today's behavior). The admin
# sections were already admin-only in the UI; the API now enforces the same.
PERMISSION_SECTIONS = ["dashboard", "campaigns", "landings", "affiliates", "offers",
                       "sources", "reports", "domains", "settings", "users", "about"]
ADMIN_ONLY_SECTIONS = {"users", "settings", "domains"}


def resolve_permissions(user) -> dict:
    """Effective permission map: {sections: {name: bool, ...}, write: bool}.

    NULL/missing permissions column = defaults (admin: everything; non-admin:
    all non-admin sections read+write).
    """
    sections = {s: True for s in PERMISSION_SECTIONS}
    if user is None or user.is_admin:
        return {"sections": sections, "write": True}
    raw = user.permissions or {}
    raw_sections = raw.get("sections") or {}
    for s in PERMISSION_SECTIONS:
        default = s not in ADMIN_ONLY_SECTIONS
        sections[s] = bool(raw_sections.get(s, default))
    return {"sections": sections, "write": bool(raw.get("write", True))}


def get_user_permissions(username: Optional[str]) -> dict:
    if not username:
        return resolve_permissions(None)
    db = SessionLocal()
    try:
        return resolve_permissions(get_user(db, username))
    finally:
        db.close()


def _is_self_service(request: Request) -> bool:
    """True for the account self-service endpoints under /api/users/me[...]."""
    path = request.scope.get("path", "")
    return path.rstrip("/").endswith("/me") or "/me/" in path


def require_section(section: str):
    """Dependency factory: caller must be authenticated AND able to read `section`."""
    async def checker(request: Request, authorization: Optional[str] = Header(None)):
        principal = require_api_auth(request, authorization)
        if principal in ("admin", "api_token"):
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
    """Dependency factory: on top of section read access, non-admins need write=true."""
    async def checker(request: Request, authorization: Optional[str] = Header(None)):
        principal = await require_section(section)(request, authorization)
        if principal in ("admin", "api_token"):
            return principal
        if section == "users" and _is_self_service(request):
            return principal
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            perms = get_user_permissions(get_session_username(request))
            if not perms["write"]:
                raise HTTPException(status_code=403, detail="Write access denied")
        return principal
    return checker


def get_caller(request: Request, authorization: Optional[str] = None):
    """(username, is_admin) for the current request — api_token counts as admin."""
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization.split(" ", 1)[1].strip()
        # cache the settings-row token per request — it was loaded twice before
        try:
            api_token = getattr(request.state, "_api_token", None)
        except Exception:
            api_token = None
        if api_token is None:
            api_token = load_api_token()
            try:
                request.state._api_token = api_token
            except Exception:
                pass
        if api_token and len(provided) == len(api_token) \
                and secrets.compare_digest(provided, api_token):
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
    client_ip = request.client.host if request.client else "unknown"
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

    token = create_session(user.username)
    _login_failures.pop(limit_key, None)
    audit_event(user.username, "login_success", "user", user.username, ip=client_ip)

    # Store the token in cookies (Secure: the app is always behind https nginx)
    response.set_cookie(key="session_token", value=token, httponly=True,
                        samesite="lax", secure=True)

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
    client_ip = request.client.host if request.client else "unknown"
    if _totp_limited(data.totp_token):
        raise HTTPException(status_code=429, detail="Too many TOTP attempts — request a new code.")

    try:
        payload = jwt.decode(data.totp_token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired TOTP token")
    if payload.get("type") != "totp" or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid TOTP token")

    user = get_user(db, payload["sub"])
    if not user or not user.active or not user.totp_enabled or not user.totp_secret:
        raise HTTPException(status_code=401, detail="Invalid TOTP token")

    # Spend the one-shot jti BEFORE verifying the code: a replayed token is
    # rejected even if the attacker has a valid TOTP code.
    ensure_totp_token_table()
    if not _spend_totp_jti(db, payload.get("jti")):
        _record_totp_failure(data.totp_token)
        audit_event(user.username, "totp_replay", "user", user.username, ip=client_ip)
        raise HTTPException(status_code=401, detail="TOTP token already used")

    if not _verify_totp_code(db, user, data.code):
        _record_totp_failure(data.totp_token)
        audit_event(user.username, "totp_failed", "user", user.username, ip=client_ip)
        raise HTTPException(status_code=401, detail="Invalid code")

    token = create_session(user.username)
    _totp_failures.pop(data.totp_token, None)
    audit_event(user.username, "login_success", "user", user.username, {"totp": True}, client_ip)

    response.set_cookie(key="session_token", value=token, httponly=True,
                        samesite="lax", secure=True)
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


# ====== GET /status ======
@router.get("/status")
async def auth_status(request: Request):
    if is_authenticated(request):
        return {"authenticated": True}
    return {"authenticated": False}
