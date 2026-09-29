"""Tenant (workspace) API — multi-tenancy phase 1.

Three session-scoped endpoints plus a provisioning endpoint:

* ``GET  /api/tenants``         — the caller's tenants, the current one, the role
* ``POST /api/tenants/switch``  — move the *session* into another membership
* ``GET  /api/tenants/current`` — the tenant this session is working in
* ``POST /api/tenants``         — admin only: create a tenant, optionally with
                                  its first user + owner membership

No endpoint accepts a tenant id for *reading data*: the active tenant always
comes from the session (auth_sessions.current_tenant_id), which the middleware
validates against tenant_memberships on every request. ``switch`` is the only
place a tenant id is accepted, and it only ever writes the caller's own session
after checking the membership.

Phase 1 did not cover per-tenant settings; phase 2B seeds the new tenant's
settings document on create (documented defaults + its own API token).

Still deliberately out of scope: per-tenant users (users are install-global —
every tenant's admin sees every user), invites, billing, white-label branding
and hierarchy traversal.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from auth import (TENANT_ROLES, caller_role_in_tenant, get_caller, get_session_username,
                  hash_password, is_platform_operator, list_user_memberships, session_token,
                  set_session_tenant)
from db import get_db
from tenant_context import current_tenant
from tenant_settings import seed_tenant_settings

router = APIRouter()

VALID_ROLES = set(TENANT_ROLES)


class TenantSwitch(BaseModel):
    tenant_id: int


class TenantCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    parent_tenant_id: Optional[int] = None
    # Optional first user for the new workspace. When `username` is omitted the
    # tenant is created empty (nobody is a member of it yet).
    username: Optional[str] = None
    password: Optional[str] = None
    email: Optional[str] = None
    role: str = "owner"


def _caller_username(request: Request) -> Optional[str]:
    """The session username (None for an api_token principal)."""
    return get_session_username(request)


@router.get("/")
def list_tenants(request: Request, db: Session = Depends(get_db)):
    """The caller's tenants + which one is current. A Bearer api_token principal
    is workspace-scoped and has no memberships; it sees the workspace the token
    belongs to (the request's current tenant) and cannot switch out of it."""
    username = _caller_username(request)
    current = current_tenant()
    if not username:
        row = db.execute(text("SELECT id, name, slug, status FROM tenants WHERE id = :t"),
                         {"t": current}).fetchone()
        tenants = ([{"tenant_id": int(row[0]), "role": "admin", "name": row[1],
                     "slug": row[2], "status": row[3]}] if row else [])
        return {"tenants": tenants, "current_tenant_id": current,
                "role": "admin", "can_switch": False}
    memberships = list_user_memberships(db, username)
    role = next((m["role"] for m in memberships if m["tenant_id"] == current), None)
    return {"tenants": memberships, "current_tenant_id": current, "role": role,
            "can_switch": len(memberships) > 1}


@router.get("/current")
def get_current_tenant(request: Request, db: Session = Depends(get_db)):
    current = current_tenant()
    row = db.execute(text("SELECT id, name, slug, parent_tenant_id, status FROM tenants "
                          "WHERE id = :t"), {"t": current}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")
    username = _caller_username(request)
    role = caller_role_in_tenant(db, username, current) if username else "admin"
    return {"tenant_id": int(row[0]), "name": row[1], "slug": row[2],
            "parent_tenant_id": row[3], "status": row[4], "role": role}


@router.post("/switch")
def switch_tenant(data: TenantSwitch, request: Request, db: Session = Depends(get_db)):
    """Persist a new current tenant on the caller's session (audited).

    Membership-checked: a tenant the caller is not a member of is a 403 and the
    session value is not written, so this endpoint cannot be used to reach
    another tenant's data.
    """
    from audit_logger import audit_event
    username = _caller_username(request)
    if not username:
        raise HTTPException(status_code=403, detail="Tenant switching needs a user session")
    role = caller_role_in_tenant(db, username, data.tenant_id)
    if not role:
        raise HTTPException(status_code=403, detail="Not a member of that workspace")
    token = session_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    set_session_tenant(db, token, data.tenant_id)
    audit_event(username, "tenant_switch", "tenant", str(data.tenant_id),
                {"role": role}, request.client.host if request.client else "")
    return {"message": "Switched", "tenant_id": int(data.tenant_id), "role": role}


@router.post("/")
def create_tenant(data: TenantCreate, request: Request, db: Session = Depends(get_db)):
    """Platform operator only. Creates a tenant, seeds its settings document
    (documented defaults + a fresh per-tenant API token) and, when `username` is
    given, its first user (created if the username is new) plus an owner
    membership — that user can then log in and land directly in the new
    workspace."""
    from audit_logger import audit_event
    caller, _ = get_caller(request)
    if not is_platform_operator(request):
        raise HTTPException(status_code=403, detail="Platform operator only")

    name = (data.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    slug = (data.slug or name).strip().lower()
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in slug).strip("-") or "tenant"
    role = (data.role or "owner").lower()
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {sorted(VALID_ROLES)}")

    if data.parent_tenant_id is not None:
        parent = db.execute(text("SELECT 1 FROM tenants WHERE id = :t"),
                            {"t": int(data.parent_tenant_id)}).fetchone()
        if not parent:
            raise HTTPException(status_code=400, detail="parent_tenant_id does not exist")

    if db.execute(text("SELECT 1 FROM tenants WHERE slug = :s"), {"s": slug}).fetchone():
        raise HTTPException(status_code=400, detail="slug already in use")

    tenant_id = db.execute(text(
        "INSERT INTO tenants (name, slug, parent_tenant_id) VALUES (:n, :s, :p) RETURNING id"),
        {"n": name, "s": slug, "p": data.parent_tenant_id}).scalar()

    if data.username:
        username = data.username.strip()
        user_row = db.execute(text("SELECT id FROM users WHERE username = :u"),
                              {"u": username}).fetchone()
        if user_row:
            user_id = int(user_row[0])
        else:
            if not data.password:
                raise HTTPException(status_code=400,
                                    detail="password is required for a new user")
            user_id = db.execute(text(
                "INSERT INTO users (username, email, password_hash, is_admin, active) "
                "VALUES (:u, :e, :p, false, true) RETURNING id"),
                {"u": username, "e": data.email, "p": hash_password(data.password)}).scalar()
        db.execute(text(
            "INSERT INTO tenant_memberships (user_id, tenant_id, role, permissions) "
            "VALUES (:u, :t, :r, NULL) ON CONFLICT (user_id, tenant_id) DO UPDATE "
            "SET role = EXCLUDED.role"), {"u": user_id, "t": tenant_id, "r": role})
    # Phase 2B: the new workspace gets its own settings document (documented
    # defaults + a fresh API token) instead of starting with no settings row.
    seed_tenant_settings(db, tenant_id)
    db.commit()
    audit_event(caller or "api_token", "tenant_created", "tenant", str(tenant_id),
                {"name": name, "slug": slug, "username": data.username},
                request.client.host if request.client else "")
    return {"message": "Tenant created", "tenant_id": int(tenant_id), "slug": slug}
