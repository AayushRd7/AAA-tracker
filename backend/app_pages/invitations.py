"""Workspace invitations — multi-tenancy phase 3, slice 1.

An invitation is a single-use, expiring grant of a role in ONE workspace. The
raw token is generated at creation, returned exactly once (with a ready-to-use
accept URL) and never stored: only its SHA-256 hash lives in
``tenant_invitations.token_hash``, so a database dump cannot be replayed as a
credential and no list/lookup response can leak it.

Rules enforced here (the server is the authority; the UI mirrors them):

* creating, listing and revoking invitations needs **owner or admin in the
  request's current workspace** — never the install-global ``users.is_admin``
  flag. A Bearer API token is its own workspace's owner credential (mirrors
  ``app_pages/members.py``) and is not a platform operator;
* the invited role may not exceed the inviter's own role (the role ceiling: an
  admin cannot invite an owner), so an invitation is never a privilege
  escalation;
* one pending invitation per invitee (email/username, case-insensitive) per
  workspace — a second one is a 409;
* ``GET /lookup`` and ``POST /accept`` are public: the invitee has no account
  yet. ``accept`` refuses an existing username or email, creates exactly one
  ``tenant_memberships`` row **in the invitation's workspace** (the workspace is
  read from the invitation row, never from the request — a token for workspace X
  can never mint a membership anywhere else), and marks the invitation accepted
  in the same transaction, so two concurrent accepts yield one user and one
  membership and a token is single-use;
* the default expiry is 7 days and is recorded on the row; an expired, revoked
  or already-accepted token is a 410 from both lookup and accept.

Still deliberately out of scope: billing, white-label branding and per-tenant
user accounts (users stay install-global rows; only the membership is scoped).
"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import (TENANT_ROLES, effective_tenant_role, get_session_username,
                  hash_password, require_api_auth)
from db import get_db
from tenant_context import (api_token_tenant, current_tenant, reset_current_tenant,
                            set_current_tenant)

router = APIRouter()

VALID_ROLES = set(TENANT_ROLES)
MANAGER_ROLES = {"owner", "admin"}
ROLE_RANK = {"owner": 3, "admin": 2, "editor": 1, "viewer": 0}
INVITE_TTL_DAYS = 7


class InvitationCreate(BaseModel):
    email: Optional[str] = None
    username: Optional[str] = None
    role: str = "editor"


class InvitationAccept(BaseModel):
    token: str
    username: str
    password: str
    email: Optional[str] = None


def _hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def _new_token() -> tuple:
    """(raw token, sha256 hash) — the raw token is returned to the caller once."""
    token = secrets.token_urlsafe(32)
    return token, _hash_token(token)


PUBLIC_BASE_VAR = "PUBLIC_BASE_URL"


def _accept_url(request: Request, token: str) -> str:
    """The ready-to-use link the inviter passes on — the dashboard sign-in page,
    which pre-fills the (still account-less) invitee's invitation.

    Mirrors ``app_pages.integrations.derive_callback_url``: prefers the configured
    public origin, else the request's scheme/host with the app root_path
    (``/backend``) as the prefix — ``request.base_url`` already contains that
    prefix behind the proxy, so it is not appended twice."""
    query = f"/auth?invite={token}"
    base = (os.environ.get(PUBLIC_BASE_VAR) or "").strip().rstrip("/")
    if base:
        return f"{base}/backend{query}"
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if not proto:
        proto = request.url.scheme
    host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if not host:
        host = (request.headers.get("host") or request.url.netloc or "").strip()
    root = (request.scope.get("root_path") or "").rstrip("/") or "/backend"
    return f"{proto}://{host}{root}{query}"


def _authority(request: Request, db: Session) -> tuple:
    """(caller_username, role) when the caller manages the current workspace.

    Owners and admins manage invitations; the role comes from the membership in
    the request's tenant (or a managed ancestor workspace — hierarchy traversal),
    so a workspace admin cannot reach another workspace and the platform flag
    grants nothing here. A Bearer API token acts as its own workspace's owner.
    """
    if api_token_tenant() is not None:
        return None, "owner"
    caller = get_session_username(request)
    if not caller:
        raise HTTPException(status_code=401, detail="Not authenticated")
    role = effective_tenant_role(db, caller, current_tenant())
    if role not in MANAGER_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Invitations are managed by the workspace owners and admins")
    return caller, role


def _invitation_by_token(db: Session, token: str):
    """The invitation row for a raw token, or None. Raw SQL: the caller is
    unauthenticated and reads the row that owns the token, which then names the
    workspace — the request never supplies one."""
    token = (token or "").strip()
    if not token:
        return None
    row = db.execute(text(
        "SELECT id, tenant_id, email, username, role, invited_by, expires_at, "
        "accepted_at, revoked_at FROM tenant_invitations WHERE token_hash = :h"),
        {"h": _hash_token(token)}).fetchone()
    if not row:
        return None
    return {"id": int(row[0]), "tenant_id": int(row[1]), "email": row[2],
            "username": row[3], "role": row[4] or "viewer", "invited_by": row[5],
            "expires_at": row[6], "accepted_at": row[7], "revoked_at": row[8]}


def _require_pending(inv: dict) -> None:
    """410 for anything no longer usable (revoked / already accepted / expired)."""
    if inv["revoked_at"] is not None:
        raise HTTPException(status_code=410, detail="Invitation was revoked")
    if inv["accepted_at"] is not None:
        raise HTTPException(status_code=410, detail="Invitation was already accepted")
    if inv["expires_at"] is not None and inv["expires_at"] < datetime.utcnow():
        raise HTTPException(status_code=410, detail="Invitation has expired")


@router.get("")
@router.get("/")
def list_invitations(request: Request, _principal: str = Depends(require_api_auth),
                     db: Session = Depends(get_db)):
    """The CURRENT workspace's pending invitations. A manager-only view that
    never carries a token or a token hash."""
    caller, role = _authority(request, db)
    tenant_id = current_tenant()
    rows = db.execute(text(
        "SELECT id, email, username, role, invited_by, created_at, expires_at "
        "FROM tenant_invitations WHERE tenant_id = :t AND accepted_at IS NULL "
        "AND revoked_at IS NULL AND expires_at > now() ORDER BY id DESC"),
        {"t": tenant_id}).fetchall()
    invitations = [{
        "id": int(r[0]),
        "email": r[1],
        "username": r[2],
        "role": r[3] or "viewer",
        "invited_by": r[4],
        "created_at": r[5].isoformat() if r[5] else None,
        "expires_at": r[6].isoformat() if r[6] else None,
    } for r in rows]
    return {"invitations": invitations, "tenant_id": int(tenant_id),
            "caller_role": role, "can_manage": True}


@router.post("")
@router.post("/")
def create_invitation(data: InvitationCreate, request: Request,
                      principal: str = Depends(require_api_auth),
                      db: Session = Depends(get_db)):
    """Invite someone into the CURRENT workspace. The raw token (and the accept
    URL built from it) is returned exactly once, here."""
    from audit_logger import audit_event
    caller, role = _authority(request, db)
    tenant_id = current_tenant()

    email = (data.email or "").strip().lower()
    username = (data.username or "").strip()
    if not email and not username:
        raise HTTPException(status_code=400, detail="An email or a username is required")
    invited_role = (data.role or "editor").lower()
    if invited_role not in VALID_ROLES:
        raise HTTPException(status_code=400,
                            detail=f"role must be one of {sorted(VALID_ROLES)}")
    # Role ceiling: an invitation never exceeds the inviter's own role, so an
    # admin can invite an admin but not an owner.
    if ROLE_RANK[invited_role] > ROLE_RANK.get(role, 0):
        raise HTTPException(
            status_code=403,
            detail=f"A {role} cannot invite a {invited_role} — an invitation never "
                   "exceeds the inviter's own role")

    pending = db.execute(text(
        "SELECT 1 FROM tenant_invitations WHERE tenant_id = :t AND accepted_at IS NULL "
        "AND revoked_at IS NULL AND expires_at > now() "
        "AND ((:email <> '' AND lower(coalesce(email, '')) = :email) "
        "     OR (:uname <> '' AND lower(coalesce(username, '')) = lower(:uname)))"),
        {"t": tenant_id, "email": email, "uname": username}).fetchone()
    if pending:
        raise HTTPException(
            status_code=409,
            detail="An invitation for this invitee is already pending in this workspace")

    token, token_hash = _new_token()
    expires_at = datetime.utcnow() + timedelta(days=INVITE_TTL_DAYS)
    invitation_id = db.execute(text(
        "INSERT INTO tenant_invitations (tenant_id, email, username, role, token_hash, "
        "invited_by, expires_at) VALUES (:t, :e, :u, :r, :h, :by, :x) RETURNING id"),
        {"t": tenant_id, "e": email or None, "u": username or None, "r": invited_role,
         "h": token_hash, "by": caller or "api_token", "x": expires_at}).scalar()
    db.commit()
    # The audit detail never carries the token.
    audit_event(caller or "api_token", "invitation_created", "tenant_invitation",
                str(invitation_id),
                {"tenant_id": int(tenant_id), "email": email or None,
                 "username": username or None, "role": invited_role},
                request.client.host if request.client else "")
    return {"message": "Invitation created", "id": int(invitation_id),
            "tenant_id": int(tenant_id), "email": email or None,
            "username": username or None, "role": invited_role, "token": token,
            "accept_url": _accept_url(request, token),
            "expires_at": expires_at.isoformat()}


@router.delete("/{invitation_id}")
def revoke_invitation(invitation_id: int, request: Request,
                      principal: str = Depends(require_api_auth),
                      db: Session = Depends(get_db)):
    """Revoke a pending invitation of the CURRENT workspace. Another workspace's
    invitation id is a 404 — the id is looked up inside the request's tenant."""
    from audit_logger import audit_event
    caller, _role = _authority(request, db)
    tenant_id = current_tenant()
    row = db.execute(text(
        "SELECT id, email, username FROM tenant_invitations WHERE id = :i AND tenant_id = :t"),
        {"i": int(invitation_id), "t": tenant_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Invitation not found in this workspace")
    result = db.execute(text(
        "UPDATE tenant_invitations SET revoked_at = now() WHERE id = :i AND tenant_id = :t "
        "AND accepted_at IS NULL AND revoked_at IS NULL"),
        {"i": int(invitation_id), "t": tenant_id})
    db.commit()
    if not result.rowcount:
        raise HTTPException(status_code=409, detail="Invitation was already accepted or revoked")
    audit_event(caller or "api_token", "invitation_revoked", "tenant_invitation",
                str(invitation_id),
                {"tenant_id": int(tenant_id), "email": row[1], "username": row[2]},
                request.client.host if request.client else "")
    return {"message": "Invitation revoked", "id": int(invitation_id)}


@router.get("/lookup")
def lookup_invitation(token: str, db: Session = Depends(get_db)):
    """PUBLIC: resolve a token to its workspace/role/invitee (no session needed —
    the invitee has no account yet). Never returns the token. 404 for an unknown
    token, 410 for one that is expired, revoked or already accepted."""
    inv = _invitation_by_token(db, token)
    if not inv:
        raise HTTPException(status_code=404, detail="Unknown invitation")
    _require_pending(inv)
    tenant = db.execute(text("SELECT name, slug FROM tenants WHERE id = :t"),
                        {"t": inv["tenant_id"]}).fetchone()
    return {"tenant_id": inv["tenant_id"],
            "workspace_name": tenant[0] if tenant else "",
            "workspace_slug": tenant[1] if tenant else "",
            "role": inv["role"], "email": inv["email"], "username": inv["username"],
            "invited_by": inv["invited_by"],
            "expires_at": inv["expires_at"].isoformat() if inv["expires_at"] else None}


@router.post("/accept")
def accept_invitation(data: InvitationAccept, request: Request, db: Session = Depends(get_db)):
    """PUBLIC: create the invitee's user account and their membership in the
    invitation's workspace, then consume the token (single-use).

    The workspace comes from the invitation row — not from the request — so a
    token for workspace X can never create a membership in workspace Y. User,
    membership and "accepted" mark are written in one transaction: a losing
    concurrent accept rolls everything back.
    """
    from audit_logger import audit_event
    inv = _invitation_by_token(db, data.token)
    if not inv:
        raise HTTPException(status_code=404, detail="Unknown invitation")
    _require_pending(inv)

    username = (data.username or "").strip()
    password = data.password or ""
    email = (data.email or "").strip().lower() or None
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    if not password:
        raise HTTPException(status_code=400, detail="password is required")
    invited_role = (inv["role"] or "viewer").lower()
    if invited_role not in VALID_ROLES:
        db.execute(text("UPDATE tenant_invitations SET revoked_at = now() WHERE id = :i"),
                   {"i": inv["id"]})
        db.commit()
        raise HTTPException(status_code=409, detail="Invitation carries an unknown role")
    if db.execute(text("SELECT 1 FROM users WHERE username = :u"),
                  {"u": username}).fetchone():
        raise HTTPException(status_code=409, detail="Username already exists")
    if email and db.execute(text("SELECT 1 FROM users WHERE lower(email) = :e"),
                            {"e": email}).fetchone():
        raise HTTPException(status_code=409, detail="Email already exists")
    if inv["email"] and not email:
        email = inv["email"]

    try:
        user_id = int(db.execute(text(
            "INSERT INTO users (username, email, password_hash, is_admin, active) "
            "VALUES (:u, :e, :p, false, true) RETURNING id"),
            {"u": username, "e": email, "p": hash_password(password)}).scalar())
        db.execute(text(
            "INSERT INTO tenant_memberships (user_id, tenant_id, role, permissions) "
            "VALUES (:u, :t, :r, NULL)"),
            {"u": user_id, "t": inv["tenant_id"], "r": invited_role})
        spent = db.execute(text(
            "UPDATE tenant_invitations SET accepted_at = now() WHERE id = :i "
            "AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now()"),
            {"i": inv["id"]})
        if not spent.rowcount:
            db.rollback()
            raise HTTPException(status_code=410, detail="Invitation is no longer usable")
        db.commit()
    except HTTPException:
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username or email already exists")
    except Exception:
        db.rollback()
        raise

    # The audit row belongs to the inviting workspace, not to the (unauthenticated)
    # request's default tenant.
    ctx = set_current_tenant(inv["tenant_id"])
    try:
        audit_event(username, "invitation_accepted", "tenant_invitation", str(inv["id"]),
                    {"tenant_id": inv["tenant_id"], "role": invited_role},
                    request.client.host if request.client else "")
    finally:
        reset_current_tenant(ctx)
    return {"message": "Invitation accepted", "tenant_id": inv["tenant_id"],
            "role": invited_role, "username": username}
