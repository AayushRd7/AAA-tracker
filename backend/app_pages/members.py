"""Workspace member management — multi-tenancy phase 2A.

Members are managed from *inside* a workspace. Every endpoint below acts on the
request's current tenant unless a platform operator passes ``?tenant_id=``
(refused for everyone else). Authority is the caller's membership role in the
target tenant — never the install-global ``users.is_admin`` flag, which only
gates the platform plane.

Rules enforced here (the server is the authority; the UI mirrors them):

* owners and admins manage members; editors/viewers cannot (403);
* role ``owner`` is never assigned by PUT — ownership changes only through
  ``POST /api/members/{user_id}/transfer-ownership``, which promotes the target
  and demotes the previous owners;
* the last owner can never be demoted or removed;
* a member cannot change their own role (no self-escalation);
* DELETE removes the membership only — the global user (and their sessions)
  survives;
* an existing install-global account is only attached when it already belongs
  to the workspace's hierarchy (its ancestors/descendants); an unrelated
  account — including a platform operator — is refused (409/403) and must be
  brought in through the consent-based invitation flow;
* the names reserved for the platform operator namespace cannot be minted or
  attached by a workspace admin (see ``app_pages/reserved.py``);
* the tenant's ``seats`` limit, when set, bounds POST /api/members.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_pages.reserved import is_reserved_username
from auth import (TENANT_ROLES, effective_permissions, get_caller, hash_password,
                  membership_for, validate_permission_scopes, validate_password)
from db import get_db
from tenant_context import api_token_tenant, current_tenant

router = APIRouter()

VALID_ROLES = set(TENANT_ROLES)
MANAGER_ROLES = {"owner", "admin"}


class MemberAdd(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None
    role: str = "editor"
    permissions: Optional[dict] = None


class MemberUpdate(BaseModel):
    role: Optional[str] = None
    permissions: Optional[dict] = None
    clear_permissions: bool = False


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _audit(username, action, entity_id, detail, request):
    from audit_logger import audit_event
    audit_event(username, action, "tenant_membership", entity_id, detail, _client_ip(request))


def _manager_context(request: Request, db: Session, tenant_id: Optional[int]):
    """(caller, is_admin, target_tenant_id, caller_role_in_target).

    ``?tenant_id=`` is a platform-operator-only escape hatch; a workspace
    admin cannot use it to reach another workspace. A Bearer API token is
    workspace-scoped and only ever targets its own tenant."""
    token_tenant = api_token_tenant()
    if token_tenant is not None:
        if tenant_id is not None and int(tenant_id) != int(token_tenant):
            raise HTTPException(
                status_code=403,
                detail="An API token only acts inside its own workspace")
        # Workspace-owner authority inside its own tenant; never a platform
        # operator (is_admin=False), so the install-global user plane stays out
        # of reach and is_platform_admin reports honestly.
        return None, False, int(token_tenant), "owner"
    caller, is_admin = get_caller(request)
    if tenant_id is not None:
        if not is_admin:
            raise HTTPException(status_code=403,
                                detail="Only a platform operator may target another workspace")
        tid = int(tenant_id)
    else:
        tid = current_tenant()
    if is_admin:
        return caller, True, tid, "owner"
    member = membership_for(db, caller, tid)
    if not member or member[1] not in MANAGER_ROLES:
        raise HTTPException(status_code=403,
                            detail="Workspace members are managed by its owners and admins")
    return caller, False, tid, member[1]


def _member_count(db: Session, tenant_id: int) -> int:
    return int(db.execute(text("SELECT count(*) FROM tenant_memberships WHERE tenant_id = :t"),
                          {"t": int(tenant_id)}).scalar() or 0)


def _owner_count(db: Session, tenant_id: int) -> int:
    return int(db.execute(text(
        "SELECT count(*) FROM tenant_memberships WHERE tenant_id = :t AND role = 'owner'"),
        {"t": int(tenant_id)}).scalar() or 0)


def _membership_row(db: Session, user_id: int, tenant_id: int):
    return db.execute(text(
        "SELECT u.id, u.username, u.email, m.role, m.permissions "
        "FROM tenant_memberships m JOIN users u ON u.id = m.user_id "
        "WHERE m.user_id = :u AND m.tenant_id = :t"),
        {"u": int(user_id), "t": int(tenant_id)}).fetchone()


def _tenant_family(db: Session, tenant_id: int) -> set:
    """The target tenant plus its ancestors and descendants (its hierarchy).

    A workspace manager may only attach an existing account that already
    belongs to this family. A user whose memberships are all in an unrelated
    workspace is refused: the consent-based invitation flow is the only way
    into a workspace they do not already belong to."""
    tid = int(tenant_id)
    family = {tid}
    # Ancestors: walk parent_tenant_id upward, stopping on a cycle.
    current = tid
    while True:
        row = db.execute(text("SELECT parent_tenant_id FROM tenants WHERE id = :t"),
                         {"t": current}).fetchone()
        if not row or row[0] is None:
            break
        parent = int(row[0])
        if parent in family:
            break
        family.add(parent)
        current = parent
    # Descendants: recursive CTE; UNION dedupes, so a malformed cycle terminates.
    rows = db.execute(text(
        "WITH RECURSIVE tree(id) AS ("
        "  SELECT id FROM tenants WHERE id = :t"
        "  UNION"
        "  SELECT child.id FROM tenants child JOIN tree ON child.parent_tenant_id = tree.id"
        ") SELECT id FROM tree"),
        {"t": tid}).fetchall()
    family.update(int(r[0]) for r in rows)
    return family


def _user_tenant_ids(db: Session, user_id: int) -> set:
    rows = db.execute(text("SELECT tenant_id FROM tenant_memberships WHERE user_id = :u"),
                      {"u": int(user_id)}).fetchall()
    return {int(r[0]) for r in rows}


@router.get("")
@router.get("/")
def list_members(request: Request, tenant_id: Optional[int] = None,
                 db: Session = Depends(get_db)):
    caller, is_admin, tid, caller_role = _manager_context(request, db, tenant_id)

    rows = db.execute(text(
        "SELECT u.id, u.username, u.email, m.role, m.permissions, u.is_admin, u.active, "
        "       u.totp_enabled, u.created_at, "
        "       (SELECT max(s.last_seen) FROM auth_sessions s WHERE s.username = u.username) "
        "FROM tenant_memberships m JOIN users u ON u.id = m.user_id "
        "WHERE m.tenant_id = :t ORDER BY u.id ASC"),
        {"t": tid}).fetchall()

    members = []
    for r in rows:
        raw = r[4] or {}
        members.append({
            "user_id": int(r[0]),
            "username": r[1],
            "email": r[2],
            "role": r[3] or "viewer",
            "permissions": raw,
            "effective_permissions": effective_permissions(db, r[1], tid),
            "is_platform_admin": bool(r[5]),
            "active": bool(r[6]),
            "totp_enabled": bool(r[7]),
            "created_at": r[8].isoformat() if r[8] else None,
            "last_login": r[9].isoformat() if r[9] else None,
        })

    tenant_row = db.execute(text(
        "SELECT plan, seats, retention_days, features FROM tenants WHERE id = :t"),
        {"t": tid}).fetchone()
    plan = tenant_row[0] if tenant_row else None
    seats = tenant_row[1] if tenant_row else None
    retention_days = tenant_row[2] if tenant_row else None
    features = tenant_row[3] if tenant_row else None
    if isinstance(features, str):
        import json as _json
        try:
            features = _json.loads(features)
        except Exception:
            features = None
    return {
        "members": members,
        "tenant_id": tid,
        "current_tenant_id": current_tenant(),
        "caller_role": caller_role,
        "can_manage": True,
        "member_count": len(members),
        "seats": int(seats) if seats is not None else None,
        "plan": plan or "free",
        "retention_days": int(retention_days) if retention_days is not None else None,
        "features": features if isinstance(features, dict) else {},
        "is_platform_admin": bool(is_admin),
    }


@router.post("")
@router.post("/")
def add_member(data: MemberAdd, request: Request, tenant_id: Optional[int] = None,
               db: Session = Depends(get_db)):
    caller, is_admin, tid, _role = _manager_context(request, db, tenant_id)

    role = (data.role or "editor").lower()
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {sorted(VALID_ROLES)}")
    # Reject an unknown owner-scope value before anything is written: a scope
    # key, when present, must be 'own' (the same validation the users API runs).
    try:
        validate_permission_scopes(data.permissions)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if role == "owner" and not is_admin and _owner_count(db, tid) > 0:
        # A workspace owner only ever moves through transfer-ownership; only a
        # platform operator may seed an owner (e.g. into a brand-new workspace).
        raise HTTPException(status_code=400,
                            detail="Use transfer-ownership to change the workspace owner")

    username = (data.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    if is_reserved_username(username):
        raise HTTPException(status_code=403,
                            detail="That username is reserved for the platform operator")

    seats = db.execute(text("SELECT seats FROM tenants WHERE id = :t"), {"t": tid}).scalar()
    if seats is not None:
        used = _member_count(db, tid)
        if used >= int(seats):
            raise HTTPException(
                status_code=400,
                detail=f"Workspace seat limit reached ({used} of {int(seats)} seats used)")

    try:
        user_row = db.execute(text("SELECT id FROM users WHERE username = :u"),
                              {"u": username}).fetchone()
        created_user = False
        if user_row:
            user_id = int(user_row[0])
            if not is_admin:
                # An existing install-global account must not be attached to an
                # arbitrary workspace: doing so would be a cross-tenant grant,
                # and could hand a workspace a platform operator's account.
                # Only accounts already inside this workspace's hierarchy may
                # join directly; everyone else must go through the
                # consent-based invitation flow (app_pages/invitations.py).
                target_is_admin = db.execute(
                    text("SELECT is_admin FROM users WHERE id = :u"),
                    {"u": user_id}).scalar()
                if target_is_admin:
                    raise HTTPException(
                        status_code=403,
                        detail="Cannot add a platform operator account to a workspace")
                if not (_user_tenant_ids(db, user_id) & _tenant_family(db, tid)):
                    raise HTTPException(
                        status_code=409,
                        detail="This account belongs to an unrelated workspace; "
                               "invite it through the invitation flow instead")
        else:
            if not data.password:
                raise HTTPException(status_code=400,
                                    detail="password is required for a new user")
            validate_password(data.password)
            user_id = db.execute(text(
                "INSERT INTO users (username, email, password_hash, is_admin, active) "
                "VALUES (:u, :e, :p, false, true) RETURNING id"),
                {"u": username, "e": data.email, "p": hash_password(data.password)}).scalar()
            user_id = int(user_id)
            created_user = True

        existing = db.execute(text(
            "SELECT 1 FROM tenant_memberships WHERE user_id = :u AND tenant_id = :t"),
            {"u": user_id, "t": tid}).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="User is already a member of this workspace")

        import json as _json
        db.execute(text(
            "INSERT INTO tenant_memberships (user_id, tenant_id, role, permissions) "
            "VALUES (:u, :t, :r, CAST(:p AS JSONB))"),
            {"u": user_id, "t": tid, "r": role,
             "p": _json.dumps(data.permissions) if data.permissions is not None else None})
        db.commit()
    except IntegrityError as e:
        db.rollback()
        if "users_username_key" in str(e.orig):
            raise HTTPException(status_code=400, detail="Username already exists.")
        if "users_email_key" in str(e.orig):
            raise HTTPException(status_code=400, detail="Email already exists.")
        raise HTTPException(status_code=500, detail="Database error")

    _audit(caller or "api_token", "member_added", str(user_id),
           {"tenant_id": tid, "username": username, "role": role,
            "user_created": created_user}, request)
    return {"message": "Member added", "user_id": user_id, "role": role,
            "user_created": created_user}


@router.put("/{user_id}")
def update_member(user_id: int, data: MemberUpdate, request: Request,
                  tenant_id: Optional[int] = None, db: Session = Depends(get_db)):
    caller, is_admin, tid, _role = _manager_context(request, db, tenant_id)

    row = _membership_row(db, user_id, tid)
    if not row:
        raise HTTPException(status_code=404, detail="Not a member of this workspace")
    target_username = row[1]
    target_role = row[3] or "viewer"

    # Validate an owner-scope value before mutating: only 'own' is accepted.
    try:
        validate_permission_scopes(data.permissions)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    new_role = None
    if data.role is not None:
        new_role = data.role.lower()
        if new_role not in VALID_ROLES:
            raise HTTPException(status_code=400,
                                detail=f"role must be one of {sorted(VALID_ROLES)}")
        if new_role != target_role:
            if not is_admin:
                if caller and target_username == caller:
                    raise HTTPException(status_code=400,
                                        detail="You cannot change your own role")
                if new_role == "owner" or target_role == "owner":
                    raise HTTPException(
                        status_code=400,
                        detail="Ownership changes go through transfer-ownership")
            if target_role == "owner" and new_role != "owner" and _owner_count(db, tid) <= 1:
                raise HTTPException(status_code=400,
                                    detail="Cannot demote the last owner of the workspace")

    sets, params = [], {"uid": int(user_id), "t": tid}
    if new_role is not None and new_role != target_role:
        sets.append("role = :role")
        params["role"] = new_role
    if data.clear_permissions:
        sets.append("permissions = NULL")
    elif data.permissions is not None:
        import json as _json
        sets.append("permissions = CAST(:perms AS JSONB)")
        params["perms"] = _json.dumps(data.permissions)

    if sets:
        db.execute(text("UPDATE tenant_memberships SET " + ", ".join(sets) +
                        " WHERE user_id = :uid AND tenant_id = :t"), params)
        db.commit()

    _audit(caller or "api_token", "member_updated", str(user_id),
           {"tenant_id": tid, "username": target_username, "role": new_role,
            "permissions_changed": bool(data.clear_permissions or data.permissions is not None)},
           request)
    return {"message": "Member updated", "role": new_role or target_role}


@router.delete("/{user_id}")
def remove_member(user_id: int, request: Request, tenant_id: Optional[int] = None,
                  db: Session = Depends(get_db)):
    caller, is_admin, tid, _role = _manager_context(request, db, tenant_id)

    row = _membership_row(db, user_id, tid)
    if not row:
        raise HTTPException(status_code=404, detail="Not a member of this workspace")
    target_username = row[1]
    target_role = row[3] or "viewer"

    if target_role == "owner" and not is_admin:
        if _owner_count(db, tid) <= 1:
            raise HTTPException(status_code=400,
                                detail="Cannot remove the last owner of the workspace")
        raise HTTPException(status_code=400,
                            detail="Transfer ownership before removing the owner")

    # Membership only: the global user row and their sessions are untouched, so
    # they can still sign in to any other workspace they belong to.
    db.execute(text("DELETE FROM tenant_memberships WHERE user_id = :u AND tenant_id = :t"),
               {"u": int(user_id), "t": tid})
    db.commit()
    _audit(caller or "api_token", "member_removed", str(user_id),
           {"tenant_id": tid, "username": target_username}, request)
    return {"message": "Member removed", "user_id": int(user_id)}


@router.post("/{user_id}/transfer-ownership")
def transfer_ownership(user_id: int, request: Request, tenant_id: Optional[int] = None,
                       db: Session = Depends(get_db)):
    caller, is_admin, tid, caller_role = _manager_context(request, db, tenant_id)
    if not is_admin and caller_role != "owner":
        raise HTTPException(status_code=403,
                            detail="Only the workspace owner may transfer ownership")

    target = _membership_row(db, user_id, tid)
    if not target:
        raise HTTPException(status_code=404, detail="Not a member of this workspace")
    caller_member = membership_for(db, caller, tid) if caller else None
    if caller_member and int(caller_member[0]) == int(user_id):
        raise HTTPException(status_code=400, detail="Already the owner")

    db.execute(text("UPDATE tenant_memberships SET role = 'owner' "
                    "WHERE user_id = :u AND tenant_id = :t"), {"u": int(user_id), "t": tid})
    # A workspace has one owner: every other owner steps down to admin.
    db.execute(text("UPDATE tenant_memberships SET role = 'admin' "
                    "WHERE tenant_id = :t AND role = 'owner' AND user_id != :u"),
               {"t": tid, "u": int(user_id)})
    db.commit()
    _audit(caller or "api_token", "ownership_transferred", str(user_id),
           {"tenant_id": tid, "username": target[1]}, request)
    return {"message": "Ownership transferred", "user_id": int(user_id)}
