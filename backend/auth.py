# app_pages/auth.py
from fastapi import APIRouter, Request, Response, HTTPException, status, Depends, Header
from pydantic import BaseModel

from sqlalchemy.orm import Session
from fastapi import Request
from jose import jwt, JWTError
from typing import Optional
from hashlib import md5
from db import get_db, get_user, SessionLocal
from hashlib import md5
from datetime import datetime, timedelta
import secrets
import json
import time
import os

import bcrypt
from sqlalchemy import text

from models.user import UserORM

router = APIRouter()

# JWT configuration (kept for compatibility; sessions are now DB-backed)
SECRET_KEY = os.environ.get("JWT_SECRET", "your-super-secret-key-for-jwt")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 2400
pass_salt = 'akm_'

SESSION_TTL_DAYS = 30


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
        if api_token and provided == api_token:
            return "api_token"
        raise HTTPException(status_code=401, detail="Invalid API token")

    user_type = is_authenticated(request)
    if not user_type:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_type


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
    client_ip = request.client.host if request.client else "unknown"
    limit_key = f"{client_ip}:{login_data.username}"
    if _login_limited(limit_key):
        raise HTTPException(status_code=429, detail="Too many failed login attempts — try again in a minute.")

    user = get_user(db, login_data.username)
    if not user:
        _record_failed_login(limit_key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    if not verify_password(login_data.password, user.password_hash):
        _record_failed_login(limit_key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials 2")

    # Transparent upgrade from the legacy md5 hash to bcrypt
    if not is_bcrypt_hash(user.password_hash or ""):
        user.password_hash = hash_password(login_data.password)
        db.commit()

    token = create_session(user.username)
    _login_failures.pop(limit_key, None)

    # Store the token in cookies
    response.set_cookie(key="session_token", value=token, httponly=True, samesite="lax")

    return {"message": "Login successful"}


# ====== POST /logout ======
@router.post("/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("session_token")
    if token:
        try:
            revoke_session(token)
        except Exception:
            pass
    response.delete_cookie(key="session_token")
    return {"message": "Logged out"}


# ====== GET /status ======
@router.get("/status")
async def auth_status(request: Request):
    if is_authenticated(request):
        return {"authenticated": True}
    return {"authenticated": False}
