from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from hashlib import md5
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from datetime import datetime

import base64
import io
import secrets

import pyotp
import qrcode

from db import get_db
from models.user import UserORM
from auth import hash_password, get_caller, verify_password, _hash_backup_code

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
    caller, is_admin = get_caller(request)
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
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


# ====== List all users ======

@router.get("/", response_model=List[UserOut])
def get_users(db: Session = Depends(get_db)):
    return db.query(UserORM).order_by(UserORM.id.asc()).all()

# ====== Create a user ======

@router.post("/")
def create_user(user: UserCreateUpdate, request: Request, db: Session = Depends(get_db)):
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
    user_obj = db.query(UserORM).filter(UserORM.id == user_id).first()
    if not user_obj:
        raise HTTPException(status_code=404, detail="User not found")

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

    db.commit()
    db.refresh(user_obj)
    _audit(user_obj.username, "user_updated", "user", user_obj.username,
           {"changes": changes}, request=request)
    return {"message": "User updated"}

# ====== Delete a user ======

@router.delete("/{user_id}")
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    user_obj = db.query(UserORM).filter(UserORM.id == user_id).first()
    if not user_obj:
        raise HTTPException(status_code=404, detail="User not found")

    db.delete(user_obj)
    db.commit()
    _audit(user_obj.username, "user_deleted", "user", user_obj.username,
           request=request)
    return {"message": "User deleted"}
