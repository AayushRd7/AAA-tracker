"""Tenant (workspace) API — multi-tenancy phase 1.

Session-scoped endpoints:

* ``GET  /api/tenants``           — the caller's tenants + the current one + role
* ``POST /api/tenants/switch``    — move the *session* into another workspace
* ``GET  /api/tenants/current``   — the workspace this session is working in
* ``GET  /api/tenants/onboarding``— the current workspace's setup checklist
* ``POST /api/tenants``           — platform operator only: create a workspace,
                                    optionally with its first user + owner
                                    membership

No endpoint accepts a tenant id for *reading data*: the active tenant always
comes from the session (auth_sessions.current_tenant_id), which the middleware
validates against tenant_memberships on every request. ``switch`` is the only
place a tenant id is accepted, and it only ever writes the caller's own session
after checking the membership.

Phase 1 did not cover per-tenant settings; phase 2B seeds the new tenant's
settings document on create (documented defaults + its own API token).

Phase 3 adds invitations (``app_pages/invitations.py``: a single-use, hashed
token that grants one role in one workspace) and **hierarchy traversal**. A
caller who holds owner/admin in a workspace may switch the session into any of
its DESCENDANTS — the walk goes UP the ``parent_tenant_id`` chain from the
target, so they act there with that manager role for the rest of the session.
Access only flows down the tree: a manager of a parent reaches every descendant,
a manager of a child never reaches its parent, and a sibling or cousin of a
managed workspace stays out of reach. While the session is parked in a child,
only the child's resources are visible (session-scoped isolation guarantees it);
``GET /api/tenants`` offers the current workspace's direct children to a manager.

Still deliberately out of scope: per-tenant user accounts (users stay
install-global rows; only the membership is scoped), billing and white-label
branding.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from auth import (TENANT_ROLES, caller_role_in_tenant, effective_tenant_role,
                  get_caller, get_session_username, hash_password, is_platform_operator,
                  list_user_memberships, managed_ancestor_role, session_token,
                  set_session_tenant)
from db import get_db
from tenant_context import current_tenant
from tenant_settings import seed_tenant_settings

router = APIRouter()

VALID_ROLES = set(TENANT_ROLES)
MANAGER_ROLES = {"owner", "admin"}

# The onboarding checklist, in the order a new workspace normally needs it.
ONBOARDING_STEPS = ("has_team", "has_traffic_source", "has_campaign", "has_offer",
                    "has_domain", "has_integration")


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


def _tenant_entry(row, role: str, via_parent: bool = False) -> dict:
    """One workspace shaped like a membership row (same keys the UI reads)."""
    return {"tenant_id": int(row[0]), "role": role, "name": row[1], "slug": row[2],
            "status": row[3], "via_parent": via_parent}


@router.get("/")
def list_tenants(request: Request, db: Session = Depends(get_db)):
    """The caller's tenants + which one is current, plus the direct children of
    the current workspace that the caller may move into.

    Hierarchy traversal (phase 3): a caller holding owner/admin in the current
    workspace also sees its DIRECT children (``parent_tenant_id = current``)
    with the inherited role, and ``can_switch`` is true whenever there is
    somewhere to move. A session parked in a descendant (no membership there)
    reports that workspace with the role inherited from its managed ancestor. A
    Bearer api_token principal is workspace-scoped and has no memberships; it
    sees its own workspace and cannot switch out of it."""
    username = _caller_username(request)
    current = current_tenant()
    if not username:
        row = db.execute(text("SELECT id, name, slug, status FROM tenants WHERE id = :t"),
                         {"t": current}).fetchone()
        tenants = ([_tenant_entry(row, "admin")] if row else [])
        return {"tenants": tenants, "current_tenant_id": current,
                "role": "admin", "can_switch": False}

    memberships = list_user_memberships(db, username)
    role = next((m["role"] for m in memberships if m["tenant_id"] == current), None)
    tenants = list(memberships)
    if role is None:
        # The session may be parked in a DESCENDANT of a workspace the caller
        # manages (POST /switch) — report the inherited role and the tenant.
        role = managed_ancestor_role(db, username, current)
        row = db.execute(text("SELECT id, name, slug, status FROM tenants WHERE id = :t"),
                         {"t": current}).fetchone() if role else None
        if row:
            tenants.insert(0, _tenant_entry(row, role, via_parent=True))
    if role in MANAGER_ROLES:
        known = {t["tenant_id"] for t in tenants}
        children = db.execute(text(
            "SELECT id, name, slug, status FROM tenants WHERE parent_tenant_id = :t "
            "ORDER BY id ASC"), {"t": current}).fetchall()
        tenants += [_tenant_entry(c, role, via_parent=True) for c in children
                    if int(c[0]) not in known]
    return {"tenants": tenants, "current_tenant_id": current, "role": role,
            "can_switch": len(tenants) > 1}


@router.get("/current")
def get_current_tenant(request: Request, db: Session = Depends(get_db)):
    current = current_tenant()
    row = db.execute(text("SELECT id, name, slug, parent_tenant_id, status FROM tenants "
                          "WHERE id = :t"), {"t": current}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")
    username = _caller_username(request)
    # A session parked in a descendant has no membership there: report the role
    # inherited from the managed ancestor workspace.
    role = effective_tenant_role(db, username, current) if username else "admin"
    return {"tenant_id": int(row[0]), "name": row[1], "slug": row[2],
            "parent_tenant_id": row[3], "status": row[4], "role": role}


@router.get("/onboarding")
def onboarding(request: Request, db: Session = Depends(get_db)):
    """The current workspace's setup checklist: booleans plus ``next_step`` (the
    first unmet item, None when everything is done).

    Every count filters on the request's CURRENT tenant only, so a parent
    workspace's members/data never show up in a child's checklist. "Has a
    configured integration" means the workspace owns a CAPI pixel or an
    integration connection."""
    tenant_id = current_tenant()

    def present(sql: str) -> bool:
        return int(db.execute(text(sql), {"t": tenant_id}).scalar() or 0) > 0

    checks = {
        "has_team": present("SELECT count(*) FROM tenant_memberships "
                            "WHERE tenant_id = :t AND role <> 'owner'"),
        "has_traffic_source": present("SELECT count(*) FROM sources WHERE tenant_id = :t"),
        "has_campaign": present("SELECT count(*) FROM campaigns WHERE tenant_id = :t"),
        "has_offer": present("SELECT count(*) FROM offers WHERE tenant_id = :t"),
        "has_domain": present("SELECT count(*) FROM domains WHERE tenant_id = :t"),
        "has_integration": present(
            "SELECT (SELECT count(*) FROM capi_pixels WHERE tenant_id = :t) "
            "+ (SELECT count(*) FROM integration_connections WHERE tenant_id = :t)"),
    }
    next_step = next((step for step in ONBOARDING_STEPS if not checks[step]), None)
    return {"tenant_id": int(tenant_id), **checks, "next_step": next_step,
            "complete": next_step is None}


@router.post("/switch")
def switch_tenant(data: TenantSwitch, request: Request, db: Session = Depends(get_db)):
    """Persist a new current tenant on the caller's session (audited).

    A caller may move into

    * a workspace they hold a membership in (any role) — unchanged phase-1
      behaviour; or
    * a workspace that is a DESCENDANT of one where they hold owner/admin,
      found by walking ``parent_tenant_id`` upward from the target. They then
      act there with that manager role for the rest of the session.

    It never allows a workspace the caller has no membership in and no managed
    ancestor of, nor a sibling/cousin of a managed workspace, nor any *parent*
    from a *child* — the walk only ever goes up. A refused switch is a 403 and
    the session value is not written. While the session is parked in the child,
    only the child's resources are visible (session-scoped isolation: the
    middleware + tenant_scope)."""
    from audit_logger import audit_event
    username = _caller_username(request)
    if not username:
        raise HTTPException(status_code=403, detail="Tenant switching needs a user session")
    role = caller_role_in_tenant(db, username, data.tenant_id)
    via_parent = False
    if not role:
        role = managed_ancestor_role(db, username, data.tenant_id)
        via_parent = role is not None
    if not role:
        raise HTTPException(status_code=403, detail="Not a member of that workspace")
    token = session_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    set_session_tenant(db, token, data.tenant_id)
    audit_event(username, "tenant_switch", "tenant", str(data.tenant_id),
                {"role": role, "via_parent": via_parent},
                request.client.host if request.client else "")
    return {"message": "Switched", "tenant_id": int(data.tenant_id), "role": role,
            "via_parent": via_parent}


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
