from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from typing import Optional, List
from hashlib import md5
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from datetime import datetime

from db import get_db
from models.user import UserORM
from auth import hash_password

router = APIRouter()

pass_salt = 'akm_'
# Pydantic models

class UserOut(BaseModel):
    id: int
    username: str
    email: Optional[EmailStr]
    is_admin: bool
    active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

class UserCreateUpdate(BaseModel):
    username: str
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    is_admin: Optional[bool] = False
    active: Optional[bool] = True

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
def create_user(user: UserCreateUpdate, db: Session = Depends(get_db)):
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
        active=user.active
    )

    db.add(new_user)
    try:
        db.commit()
        db.refresh(new_user)
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
def update_user(user_id: int, user: UserCreateUpdate, db: Session = Depends(get_db)):
    user_obj = db.query(UserORM).filter(UserORM.id == user_id).first()
    if not user_obj:
        raise HTTPException(status_code=404, detail="User not found")

    if user.email is not None:
        user_obj.email = user.email
    if user.is_admin is not None:
        user_obj.is_admin = user.is_admin
    if user.active is not None:
        user_obj.active = user.active
    if user.password:
        user_obj.password_hash = hash_password(user.password)

    if user_obj.username.lower() != "tracker_admin":
        user.is_admin = False
    else:
        user.is_admin = True

    db.commit()
    db.refresh(user_obj)
    return {"message": "User updated"}

# ====== Delete a user ======

@router.delete("/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db)):
    user_obj = db.query(UserORM).filter(UserORM.id == user_id).first()
    if not user_obj:
        raise HTTPException(status_code=404, detail="User not found")

    db.delete(user_obj)
    db.commit()
    return {"message": "User deleted"}
