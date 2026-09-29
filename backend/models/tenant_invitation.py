"""Workspace invitations (multi-tenancy phase 3).

One row per invitation: a single-use, expiring grant of one role in one
workspace. ``token_hash`` is the SHA-256 of the one-time token handed to the
inviter and returned exactly once — the raw token is never stored, never logged
and never included in a list or lookup response. The table is created by the
startup migration (backend/app.py) and install/sql/init.sql; the endpoints live
in app_pages/invitations.py.
"""
from sqlalchemy import Column, DateTime, Integer, String, func

from models.base import Base, TenantMixin


class TenantInvitationORM(TenantMixin, Base):
    __tablename__ = "tenant_invitations"

    id = Column(Integer, primary_key=True)
    email = Column(String(255))
    username = Column(String(255))
    role = Column(String(32), nullable=False, default="viewer")
    token_hash = Column(String(64), nullable=False, unique=True)
    invited_by = Column(String(255))
    created_at = Column(DateTime, server_default=func.now())
    expires_at = Column(DateTime, nullable=False)
    accepted_at = Column(DateTime)
    revoked_at = Column(DateTime)
