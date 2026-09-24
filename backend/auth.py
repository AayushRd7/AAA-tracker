# app_pages/auth.py
from fastapi import APIRouter, Request, Response, HTTPException, status, Depends
from pydantic import BaseModel

from sqlalchemy.orm import Session
from fastapi import Request
from jose import jwt, JWTError
from typing import Optional
from hashlib import md5
from db import get_db, get_user, SessionLocal
from hashlib import md5
from datetime import datetime, timedelta

from models.user import UserORM

router = APIRouter()

# JWT configuration
SECRET_KEY = "your-super-secret-key-for-jwt"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 2400
pass_salt = 'akm_'


# Token generation
def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS))
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


# Model for passing the login and password
class LoginRequest(BaseModel):
    username: str
    password: str


# Secret key used to sign tokens
SECRET_KEY = "your-super-secret-key"
ALGORITHM = "HS256"

# Fake user store
fake_users_db = {
    "admin": {
        "username": "admin",
        "password_hash": md5("akm_admin".encode()).hexdigest()
    },
    "user1": {
        "username": "user1",
        "password_hash": md5("akm_user".encode()).hexdigest()
    }
}


# ====== Authorization check ======
def is_authenticated(request: Request) -> any:
    token = request.cookies.get("session_token")
    if not token:
        return False

    try:
        # Decode the JWT token
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")

        if not username:
            return False

        db: Session = SessionLocal()
        user = get_user(db, username)
        db.close()
        if user:
            if not user.active:
                return False
            else:
                # Check that the token is not expired
                if datetime.fromtimestamp(payload["exp"]) < datetime.utcnow():
                    return False
                else:
                    if user.username == "tracker_admin":
                        return "admin"
                    else:
                        return "user"
        else:
            return False


    except JWTError:
        return False


# ====== POST /login ======
@router.post("/login")
async def login(request: Request, response: Response, login_data: LoginRequest, db: Session = Depends(get_db)):
    user = get_user(db, login_data.username)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # Hash the password as "akm" + password
    hashed_password = md5((pass_salt + login_data.password).encode()).hexdigest()

    if user.password_hash != hashed_password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials 2")

    # Generate the token
    token_data = {"sub": user.username}
    token = create_access_token(data=token_data)

    # Store the token in cookies
    response.set_cookie(key="session_token", value=token, httponly=True)

    return {"message": "Login successful"}


# ====== POST /logout ======
@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key="session_token")
    return {"message": "Logged out"}


# ====== GET /status ======
@router.get("/status")
async def auth_status(request: Request):
    token = request.cookies.get("session_token")
    if token == "valid_token":
        return {"authenticated": True}
    return {"authenticated": False}
