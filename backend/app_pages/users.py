from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from hashlib import md5
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from datetime import datetime

import base64
import io
import json
import secrets

import pyotp
import qrcode

from db import get_db
from models.user import UserORM
from auth import (hash_password, get_caller, verify_password, _hash_backup_code,
                  session_token, list_sessions, revoke_session_prefix,
                  revoke_other_sessions)

router = APIRouter()

pass_salt = 'akm_'
# Pydantic models

class UserOut(BaseModel):
    id: int
    username: str
    email: Optional[EmailStr]
    is_admin: bool
    active: bool
    totp_enabled: bool = False
    permissions: Optional[dict] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

class UserCreateUpdate(BaseModel):
    username: str
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    is_admin: Optional[bool] = None
    active: Optional[bool] = None
    permissions: Optional[dict] = None
    clear_permissions: bool = False


def _current_user(request: Request, db: Session) -> UserORM:
    from auth import get_session_username
    username = get_session_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_obj = db.query(UserORM).filter(UserORM.username == username).first()
    if not user_obj:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_obj


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _audit(username, action, entity="user", entity_id="", detail=None, request=None):
    from audit_logger import audit_event
    audit_event(username, action, entity, entity_id, detail,
                _client_ip(request) if request is not None else "")


def _require_platform_admin(request: Request) -> str:
    """Phase 2A: the install-wide user plane (co-located 2FA/password reset,
    global enable/disable) is the platform operator's, not a workspace's.
    Membership authority lives in /api/members."""
    caller, is_admin = get_caller(request)
    if not is_admin:
        raise HTTPException(status_code=403, detail="Platform operator only")
    return caller or "api_token"


def _sync_membership_permissions(db: Session, user_id: int, permissions) -> None:
    """Mirror a users.permissions write onto the user's membership in the
    current tenant, where phase 2A reads authority from."""
    from tenant_context import current_tenant
    db.execute(text(
        "UPDATE tenant_memberships SET permissions = CAST(:p AS JSONB) "
        "WHERE user_id = :u AND tenant_id = :t"),
        {"p": json.dumps(permissions) if permissions is not None else None,
         "u": int(user_id), "t": current_tenant()})


def _other_active_admins(db: Session, user_obj: UserORM) -> int:
    """Active admins other than user_obj."""
    return db.query(UserORM).filter(
        UserORM.is_admin == True,  # noqa: E712
        UserORM.active == True,    # noqa: E712
        UserORM.id != user_obj.id).count()


# ====== My own profile / 2FA status ======

@router.get("/me")
def get_me(request: Request, db: Session = Depends(get_db)):
    user_obj = _current_user(request, db)
    return {"username": user_obj.username, "is_admin": user_obj.is_admin,
            "totp_enabled": bool(user_obj.totp_enabled)}


# ====== G62 — TOTP self-service ======

class TotpEnableRequest(BaseModel):
    code: str

class TotpDisableRequest(BaseModel):
    password: str


def _qr_data_uri(otpauth_url: str) -> str:
    import qrcode.image.svg
    img = qrcode.make(otpauth_url, image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


@router.post("/me/totp/setup")
def totp_setup(request: Request, db: Session = Depends(get_db)):
    user_obj = _current_user(request, db)
    if user_obj.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is already enabled — disable it first")

    secret = pyotp.random_base32()
    user_obj.totp_secret = secret
    user_obj.totp_enabled = False
    user_obj.totp_backup = None
    db.commit()

    otpauth_url = pyotp.TOTP(secret).provisioning_uri(
        name=user_obj.email or user_obj.username, issuer_name="AAA Tracker")
    _audit(user_obj.username, "totp_setup", "user", user_obj.username, request=request)
    return {"secret": secret, "otpauth_url": otpauth_url, "qr": _qr_data_uri(otpauth_url)}


@router.post("/me/totp/enable")
def totp_enable(data: TotpEnableRequest, request: Request, db: Session = Depends(get_db)):
    user_obj = _current_user(request, db)
    if not user_obj.totp_secret:
        raise HTTPException(status_code=400, detail="Run setup first")
    if user_obj.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is already enabled")

    if not pyotp.TOTP(user_obj.totp_secret).verify((data.code or "").strip(), valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid code — check your authenticator app's clock")

    # 10 one-time backup codes, shown exactly once
    codes = []
    hashes = []
    for _ in range(10):
        code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        codes.append(code)
        hashes.append({"h": _hash_backup_code(code), "used": False})

    user_obj.totp_enabled = True
    user_obj.totp_backup = hashes
    db.commit()
    _audit(user_obj.username, "totp_enabled", "user", user_obj.username, request=request)
    return {"message": "2FA enabled", "backup_codes": codes}


@router.post("/me/totp/disable")
def totp_disable(data: TotpDisableRequest, request: Request, db: Session = Depends(get_db)):
    user_obj = _current_user(request, db)
    if not verify_password(data.password, user_obj.password_hash):
        raise HTTPException(status_code=400, detail="Password is incorrect")
    user_obj.totp_enabled = False
    user_obj.totp_secret = None
    user_obj.totp_backup = None
    db.commit()
    _audit(user_obj.username, "totp_disabled", "user", user_obj.username, request=request)
    return {"message": "2FA disabled"}


# ====== G62 — admin reset of another user's 2FA ======

@router.post("/{user_id}/totp/reset")
def admin_totp_reset(user_id: int, request: Request, db: Session = Depends(get_db)):
    caller = _require_platform_admin(request)
    user_obj = db.query(UserORM).filter(UserORM.id == user_id).first()
    if not user_obj:
        raise HTTPException(status_code=404, detail="User not found")
    user_obj.totp_enabled = False
    user_obj.totp_secret = None
    user_obj.totp_backup = None
    db.commit()
    _audit(caller or "api_token", "totp_reset", "user", user_obj.username,
           {"by": caller or "api_token"}, request)
    return {"message": "2FA reset"}

# ====== Change my own password (any logged-in user) ======

class PasswordChange(BaseModel):
    current_password: str
    new_password: str


@router.patch("/me/password")
def change_my_password(data: PasswordChange, request: Request, db: Session = Depends(get_db)):
    from fastapi import Request as FastAPIRequest  # noqa: F401 (kept for clarity)
    from auth import get_session_username, verify_password, hash_password

    username = get_session_username(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")

    user_obj = db.query(UserORM).filter(UserORM.username == username).first()
    if not user_obj:
        raise HTTPException(status_code=404, detail="User not found")

    if not verify_password(data.current_password, user_obj.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")

    user_obj.password_hash = hash_password(data.new_password)
    db.commit()
    return {"message": "Password changed"}


# ====== Active sessions (account security) ======

@router.get("/me/sessions")
def my_sessions(request: Request, db: Session = Depends(get_db)):
    """The caller's own active sessions (devices), newest first."""
    user_obj = _current_user(request, db)
    return {"sessions": list_sessions(db, user_obj.username, session_token(request))}


@router.delete("/me/sessions")
def revoke_other_devices(request: Request, others: bool = False,
                         db: Session = Depends(get_db)):
    """Log out every session except the current one (others=true)."""
    user_obj = _current_user(request, db)
    if not others:
        raise HTTPException(status_code=400,
                            detail="Pass others=true to log out other devices")
    count = revoke_other_sessions(db, user_obj.username, session_token(request))
    _audit(user_obj.username, "sessions_revoked", "user", user_obj.username,
           {"scope": "others", "count": count}, request)
    return {"message": "Logged out other devices", "revoked": count}


@router.delete("/me/sessions/{session_id}")
def revoke_my_session(session_id: str, request: Request, db: Session = Depends(get_db)):
    """Revoke one of the caller's own sessions by its token prefix."""
    user_obj = _current_user(request, db)
    if session_id == session_token(request)[:12]:
        raise HTTPException(status_code=400,
                            detail="Use log out to end the current session")
    count = revoke_session_prefix(db, user_obj.username, session_id)
    if not count:
        raise HTTPException(status_code=404, detail="Session not found")
    _audit(user_obj.username, "session_revoked", "user", user_obj.username,
           {"session": session_id}, request)
    return {"message": "Session revoked", "revoked": count}


@router.get("/{user_id}/sessions")
def admin_user_sessions(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Platform-operator view of another user's active sessions."""
    _require_platform_admin(request)
    user_obj = db.query(UserORM).filter(UserORM.id == user_id).first()
    if not user_obj:
        raise HTTPException(status_code=404, detail="User not found")
    return {"username": user_obj.username,
            "sessions": list_sessions(db, user_obj.username, "")}


# ====== List all users ======

@router.get("/", response_model=List[UserOut])
def get_users(request: Request, db: Session = Depends(get_db)):
    # Install-wide user list — platform operator only. Workspace members are
    # listed per tenant by /api/members.
    _require_platform_admin(request)
    return db.query(UserORM).order_by(UserORM.id.asc()).all()

# ====== Create a user ======

@router.post("/")
def create_user(user: UserCreateUpdate, request: Request, db: Session = Depends(get_db)):
    _require_platform_admin(request)
    if not user.password:
        raise HTTPException(status_code=400, detail="Password is required")

    if user.username.lower() == "tracker_admin":
        raise HTTPException(status_code=403, detail="Cannot create tracker_admin user")

    password_hash = hash_password(user.password)

    new_user = UserORM(
        username=user.username,
        email=user.email,
        password_hash=password_hash,
        is_admin=False,
        active=user.active,
        permissions=user.permissions,
    )

    db.add(new_user)
    try:
        db.commit()
        db.refresh(new_user)
        # Multi-tenancy: a user is only visible in a workspace through a
        # membership. A new user joins the workspace the creating admin is in,
        # and its permissions seed the membership (phase 2A reads authority
        # from the membership, not the users row).
        from tenant_context import current_tenant
        db.execute(text(
            "INSERT INTO tenant_memberships (user_id, tenant_id, role, permissions) "
            "VALUES (:u, :t, :r, CAST(:p AS JSONB)) "
            "ON CONFLICT (user_id, tenant_id) DO NOTHING"),
            {"u": new_user.id, "t": current_tenant(),
             "r": "admin" if new_user.is_admin else "editor",
             "p": json.dumps(new_user.permissions) if new_user.permissions is not None else None})
        db.commit()
        _audit(user.username, "user_created", "user", user.username, request=request)
        return {"message": "User created", "id": new_user.id}
    except IntegrityError as e:
        db.rollback()
        if 'users_username_key' in str(e.orig):
            raise HTTPException(status_code=400, detail="Username already exists.")
        if 'users_email_key' in str(e.orig):
            raise HTTPException(status_code=400, detail="Email already exists.")
        raise HTTPException(status_code=500, detail="Database error")

# ====== Update a user ======

@router.patch("/{user_id}")
def update_user(user_id: int, user: UserCreateUpdate, request: Request, db: Session = Depends(get_db)):
    _require_platform_admin(request)
    user_obj = db.query(UserORM).filter(UserORM.id == user_id).first()
    if not user_obj:
        raise HTTPException(status_code=404, detail="User not found")

    # Lockout guards: the built-in tracker_admin stays active+admin, and the
    # last active admin can never be demoted or deactivated.
    if user_obj.username.lower() == "tracker_admin":
        if user.active is not None and not user.active:
            raise HTTPException(status_code=400,
                                detail="The built-in admin account cannot be deactivated")
        if user.is_admin is not None and not user.is_admin:
            raise HTTPException(status_code=400,
                                detail="The built-in admin account cannot be demoted")
    if user.active is not None and user_obj.active and not user.active \
            and user_obj.is_admin and _other_active_admins(db, user_obj) == 0:
        raise HTTPException(status_code=400,
                            detail="Cannot deactivate the last active admin")
    if user.is_admin is not None and user_obj.is_admin and not user.is_admin \
            and user_obj.active and _other_active_admins(db, user_obj) == 0:
        raise HTTPException(status_code=400,
                            detail="Cannot demote the last active admin")

    changes = {}
    if user.email is not None:
        user_obj.email = user.email
    if user_obj.username.lower() == "tracker_admin":
        user_obj.is_admin = True
    elif user.is_admin is not None:
        if user_obj.is_admin != user.is_admin:
            changes["is_admin"] = user.is_admin
        user_obj.is_admin = user.is_admin
    if user.active is not None:
        if user_obj.active != user.active:
            changes["active"] = user.active
        user_obj.active = user.active
    if user.password:
        user_obj.password_hash = hash_password(user.password)
        changes["password"] = True
    if user.clear_permissions:
        user_obj.permissions = None
        changes["permissions"] = "cleared"
    elif user.permissions is not None:
        user_obj.permissions = user.permissions
        changes["permissions"] = user.permissions

    if user.clear_permissions or user.permissions is not None:
        _sync_membership_permissions(db, user_obj.id, user_obj.permissions)

    db.commit()
    db.refresh(user_obj)
    _audit(user_obj.username, "user_updated", "user", user_obj.username,
           {"changes": changes}, request=request)
    return {"message": "User updated"}

# ====== Delete a user ======

@router.delete("/{user_id}")
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    _require_platform_admin(request)
    user_obj = db.query(UserORM).filter(UserORM.id == user_id).first()
    if not user_obj:
        raise HTTPException(status_code=404, detail="User not found")

    if user_obj.username.lower() == "tracker_admin":
        raise HTTPException(status_code=400,
                            detail="The built-in admin account cannot be deleted")
    if user_obj.is_admin and user_obj.active and _other_active_admins(db, user_obj) == 0:
        raise HTTPException(status_code=400,
                            detail="Cannot delete the last active admin")

    db.delete(user_obj)
    db.commit()
    _audit(user_obj.username, "user_deleted", "user", user_obj.username,
           request=request)
    return {"message": "User deleted"}
