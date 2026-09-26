from sqlalchemy import Column, Integer, String, Boolean, Text, TIMESTAMP, JSON, func
from sqlalchemy.ext.declarative import declarative_base
from models.base import Base

class UserORM(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())
    username = Column(String(255), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=True)
    password_hash = Column(Text, nullable=False)
    is_admin = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    # G62 — TOTP two-factor authentication
    totp_secret = Column(Text, nullable=True)
    totp_enabled = Column(Boolean, default=False)
    totp_backup = Column(JSON, nullable=True)
    # G63 — per-resource permissions ({sections: {...}, write: bool}); NULL = defaults
    permissions = Column(JSON, nullable=True)
