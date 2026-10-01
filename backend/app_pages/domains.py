import socket
from fastapi import APIRouter, Depends, HTTPException, Request
from enum import Enum
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select
from sqlalchemy import text

from db import get_db
from tenant_context import current_tenant
from models.domain import DomainORM
from models.user import UserORM
from models.domain_groups import DomainGroupORM, DomainGroupDomainORM, DomainGroupUserORM
import httpx
import ssl as ssl_mod
from datetime import datetime
from pathlib import Path

router = APIRouter()


def server_public_ip() -> str:
    """Best-effort egress IP of this machine (what an A record should point to).
    UDP connect sends no traffic; falls back to loopback on failure."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def resolve_domain(domain: str) -> list:
    try:
        infos = socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
        return sorted({i[4][0] for i in infos})
    except OSError:
        return []


# Pydantic model for the API
class Handle404Enum(str, Enum):
    handle = 'handle'
    error = 'error'


class StatusEnum(str, Enum):
    pending = 'pending'
    success = 'success'
    error = 'error'


class DomainCreateUpdate(BaseModel):
    domain: str
    redirect_https: Optional[bool] = True
    handle_404: Handle404Enum = Handle404Enum.error
    default_campaign_id: Optional[int] = None
    group_name: Optional[str] = None


# ====== GET /domains ======
@router.get("/", response_model=List[dict])
async def get_domains(request: Request, db: Session = Depends(get_db)):
    domains = db.query(DomainORM).order_by(DomainORM.id.asc()).all()

    # D2 — domains that belong to a group are only listed for granted users
    # (and admins); everyone else still sees ungrouped domains. This endpoint
    # feeds the campaign editor's domain picker, so the filter lives here.
    from auth import get_caller
    username, is_admin = get_caller(request)
    hidden_ids = set()
    if not is_admin and username:
        user = db.query(UserORM).filter(UserORM.username == username).first()
        if user:
            rows = db.execute(
                text("""
                    SELECT dgd.domain_id FROM domain_group_domains dgd
                    JOIN domain_groups dg ON dg.id = dgd.group_id
                    WHERE dg.tenant_id = :tid
                      AND dgd.group_id NOT IN (
                        SELECT group_id FROM domain_group_users WHERE user_id = :uid
                    )
                """),
                {"uid": user.id, "tid": current_tenant()},
            ).fetchall()
            hidden_ids = {r[0] for r in rows}

    return [
        {
            "id": domain.id,
            "domain": domain.domain,
            "redirect_https": domain.redirect_https,
            "handle_404": domain.handle_404,
            "default_campaign_id": domain.default_campaign_id,
            "group_name": domain.group_name,
            "status": domain.status,
            "ssl_status": domain.ssl_status,
            "created_at": domain.created_at.isoformat() if domain.created_at else None,
            "updated_at": domain.updated_at.isoformat() if domain.updated_at else None,
        }
        for domain in domains if domain.id not in hidden_ids
    ]


# ====== GET /domains/dns-status ======
@router.get("/server-info")
async def server_info():
    """The address a tracking domain's DNS record should point at.

    The page shows this as the A-record alternative to a CNAME so an operator
    never has to look the host's public IP up by hand."""
    return {"server_ip": server_public_ip()}


@router.get("/dns-status")
async def dns_status(domain: str, db: Session = Depends(get_db)):
    """Pre-SSL DNS check: does the domain resolve, and does it point at this
    server? The certificate challenge is fetched over plain HTTP at the domain,
    so it can only succeed once the domain routes here. Only domains already in
    our table may be looked up (no open resolver)."""
    row = db.query(DomainORM).filter(DomainORM.domain == domain).first()
    if not row:
        raise HTTPException(status_code=404, detail="Domain not in the domain list")
    addresses = resolve_domain(domain)
    server_ip = server_public_ip()
    return {
        "domain": domain,
        "resolves": bool(addresses),
        "addresses": addresses,
        "server_ip": server_ip,
        "points_to_server": server_ip in addresses,
    }


# ====== GET /domains/ssl-expiry ======
LETSENCRYPT_LIVE = Path("/etc/letsencrypt/live")


def _cert_expiry_days(domain: str) -> Optional[int]:
    """Days until the domain's certificate expires (None when no/invalid cert).

    Certificates are the certbot layout mounted at /etc/letsencrypt — the same
    files nginx's per-domain server blocks point at."""
    cert_path = LETSENCRYPT_LIVE / domain / "fullchain.pem"
    if not cert_path.is_file():
        return None
    try:
        info = ssl_mod._ssl._test_decode_cert(str(cert_path))
        not_after = info.get("notAfter")
        if not not_after:
            return None
        expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
        return (expiry - datetime.utcnow()).days
    except Exception:
        return None


@router.get("/ssl-expiry")
async def ssl_expiry(db: Session = Depends(get_db)):
    """Days-to-expiry per managed domain. Domains without a certificate on
    disk report status "unknown" so the UI shows a neutral chip instead of
    failing."""
    out = []
    for d in db.query(DomainORM).order_by(DomainORM.id.asc()).all():
        days = _cert_expiry_days(d.domain)
        if days is None:
            status = "unknown"
        elif days < 0:
            status = "expired"
        elif days <= 7:
            status = "critical"
        elif days <= 30:
            status = "warning"
        else:
            status = "ok"
        out.append({
            "id": d.id,
            "domain": d.domain,
            "days_to_expiry": days,
            "status": status,
        })
    return {"domains": out}


# ====== POST /domains ======
@router.post("/")
async def create_domain(domain: DomainCreateUpdate, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    try:
        new_domain = DomainORM(
            domain=domain.domain,
            redirect_https=domain.redirect_https,
            handle_404=domain.handle_404,
            default_campaign_id=domain.default_campaign_id,
            group_name=domain.group_name,
            status='pending'  # <-- always set to 'pending'
        )
        db.add(new_domain)
        db.commit()
        db.refresh(new_domain)
        from auth import get_caller
        caller, _ = get_caller(request)
        audit_event(caller or "api_token", "create", "domains", str(new_domain.id),
                    {"domain": new_domain.domain},
                    request.client.host if request.client else "")
        return {"message": "Domain created", "id": new_domain.id}
    except IntegrityError as e:
        db.rollback()
        if 'domains_domain_key' in str(e.orig):
            raise HTTPException(status_code=400, detail="A domain with this name already exists.")
        raise HTTPException(status_code=500, detail="Database error")


# ====== PATCH /domains/{domain_id} ======
@router.put("/{domain_id}")
async def update_domain(domain_id: int, domain: DomainCreateUpdate, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    domain_obj = db.query(DomainORM).filter(DomainORM.id == domain_id).first()
    if not domain_obj:
        raise HTTPException(status_code=404, detail="Domain not found")

    changed = []
    for key, value in domain.dict(exclude_unset=True).items():
        if getattr(domain_obj, key, None) != value:
            changed.append(key)
        setattr(domain_obj, key, value)

    db.commit()
    db.refresh(domain_obj)
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "domains", str(domain_id),
                {"fields": changed}, request.client.host if request.client else "")
    return {"message": f"Domain {domain_id} updated"}


# ====== DELETE /domains/{domain_id} ======
@router.delete("/{domain_id}")
async def delete_domain(domain_id: int, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    domain_obj = db.query(DomainORM).filter(DomainORM.id == domain_id).first()
    if not domain_obj:
        raise HTTPException(status_code=404, detail="Domain not found")

    from models.campaigns import CampaignORM
    if db.query(CampaignORM).filter_by(domain_id=domain_obj.id).first():
        raise HTTPException(status_code=409,
                            detail=f'Domain "{domain_obj.domain}" is linked to campaigns '
                                   'and cannot be deleted. Unlink it from the '
                                   'campaigns first.')

    db.delete(domain_obj)
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "delete", "domains", str(domain_id),
                {"domain": domain_obj.domain},
                request.client.host if request.client else "")

    # delete from nginx
    # /var/www/nginx/domains/domain_id_ file
    # nginx_config_path = f"/var/www/nginx/domains/{domain_id}_*.conf"
    for file in Path("/var/www/nginx/domains").glob(f"{domain_id}_*.conf"):
        file.unlink()

    return {"message": f"Domain {domain_id} deleted"}


#################### DOMAINS CHECK STATUS #########################

async def check_domain_http(domain: str) -> bool:
    try:
        url = f"http://{domain}/domain_ping"  # or "/" or a custom endpoint
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, follow_redirects=True)
            return response.status_code == 200
    except Exception:
        return False


@router.get("/check-domains")
async def check_domains(db: Session = Depends(get_db)):
    domains = db.execute(select(DomainORM)).scalars().all()
    results = []

    for domain_obj in domains:
        domain = domain_obj.domain
        ok = await check_domain_http(domain)

        domain_obj.status = 'success' if ok else 'error'
        domain_obj.updated_at = datetime.utcnow()

        results.append({
            "domain": domain,
            "status": "✅ reachable" if ok else "❌ unreachable"
        })

    db.commit()
    return {"results": results}


#################### DOMAIN GROUPS (D2) #########################

class DomainGroupIn(BaseModel):
    name: str


class GroupIdsIn(BaseModel):
    ids: List[int]


def _group_payload(db: Session, group: DomainGroupORM) -> dict:
    domain_rows = db.query(DomainGroupDomainORM) \
        .filter(DomainGroupDomainORM.group_id == group.id).all()
    user_rows = db.query(DomainGroupUserORM) \
        .filter(DomainGroupUserORM.group_id == group.id).all()
    users = {u.id: u.username for u in
             db.query(UserORM).filter(UserORM.id.in_([r.user_id for r in user_rows])).all()} \
        if user_rows else {}
    return {
        "id": group.id,
        "name": group.name,
        "domain_ids": [r.domain_id for r in domain_rows],
        "users": [{"id": r.user_id, "username": users.get(r.user_id, str(r.user_id))}
                  for r in user_rows],
    }


@router.get("/groups", response_model=List[dict])
async def list_groups(db: Session = Depends(get_db)):
    groups = db.query(DomainGroupORM).order_by(DomainGroupORM.id.asc()).all()
    return [_group_payload(db, g) for g in groups]


@router.post("/groups", response_model=dict)
async def create_group(data: DomainGroupIn, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    group = DomainGroupORM(name=name)
    db.add(group)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="A group with this name already exists")
    db.refresh(group)
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "create", "domain_groups", str(group.id),
                {"name": group.name}, request.client.host if request.client else "")
    return {"message": "Domain group created", "id": group.id}


@router.put("/groups/{group_id}", response_model=dict)
async def update_group(group_id: int, data: DomainGroupIn, request: Request,
                       db: Session = Depends(get_db)):
    from audit_logger import audit_event
    group = db.query(DomainGroupORM).filter(DomainGroupORM.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    group.name = name
    group.updated_at = datetime.utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="A group with this name already exists")
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "domain_groups", str(group_id),
                {"name": name}, request.client.host if request.client else "")
    return {"message": f"Group {group_id} updated"}


@router.delete("/groups/{group_id}", response_model=dict)
async def delete_group(group_id: int, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    group = db.query(DomainGroupORM).filter(DomainGroupORM.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    name = group.name
    db.delete(group)  # member + grant rows cascade
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "delete", "domain_groups", str(group_id),
                {"name": name}, request.client.host if request.client else "")
    return {"message": f"Group {group_id} deleted"}


@router.post("/groups/{group_id}/domains", response_model=dict)
async def assign_group_domains(group_id: int, data: GroupIdsIn, request: Request,
                               db: Session = Depends(get_db)):
    """Add domains to a group (idempotent — existing members are skipped)."""
    from audit_logger import audit_event
    group = db.query(DomainGroupORM).filter(DomainGroupORM.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    valid = {d.id for d in db.query(DomainORM)
             .filter(DomainORM.id.in_(data.ids)).all()} if data.ids else set()
    existing = {r.domain_id for r in db.query(DomainGroupDomainORM)
                .filter(DomainGroupDomainORM.group_id == group_id).all()}
    added = 0
    for domain_id in valid - existing:
        db.add(DomainGroupDomainORM(group_id=group_id, domain_id=domain_id))
        added += 1
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "domain_groups", str(group_id),
                {"domains_added": sorted(valid - existing)}, request.client.host if request.client else "")
    return {"message": f"Added {added} domains to group", "updated": added}


@router.delete("/groups/{group_id}/domains/{domain_id}", response_model=dict)
async def unassign_group_domain(group_id: int, domain_id: int, request: Request,
                                db: Session = Depends(get_db)):
    row = db.query(DomainGroupDomainORM) \
        .filter(DomainGroupDomainORM.group_id == group_id,
                DomainGroupDomainORM.domain_id == domain_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Domain is not in this group")
    db.delete(row)
    db.commit()
    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "domain_groups", str(group_id),
                {"domain_removed": domain_id}, request.client.host if request.client else "")
    return {"message": f"Domain {domain_id} removed from group"}


@router.post("/groups/{group_id}/users", response_model=dict)
async def grant_group_users(group_id: int, data: GroupIdsIn, request: Request,
                            db: Session = Depends(get_db)):
    """Grant users access to the group's domains (idempotent)."""
    from audit_logger import audit_event
    group = db.query(DomainGroupORM).filter(DomainGroupORM.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    valid = {u.id for u in db.query(UserORM)
             .filter(UserORM.id.in_(data.ids)).all()} if data.ids else set()
    existing = {r.user_id for r in db.query(DomainGroupUserORM)
                .filter(DomainGroupUserORM.group_id == group_id).all()}
    granted = valid - existing
    for user_id in granted:
        db.add(DomainGroupUserORM(group_id=group_id, user_id=user_id))
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "domain_groups", str(group_id),
                {"users_granted": sorted(granted)}, request.client.host if request.client else "")
    return {"message": f"Granted {len(granted)} users", "updated": len(granted)}


@router.delete("/groups/{group_id}/users/{user_id}", response_model=dict)
async def revoke_group_user(group_id: int, user_id: int, request: Request,
                            db: Session = Depends(get_db)):
    row = db.query(DomainGroupUserORM) \
        .filter(DomainGroupUserORM.group_id == group_id,
                DomainGroupUserORM.user_id == user_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="User has no grant on this group")
    db.delete(row)
    db.commit()
    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "domain_groups", str(group_id),
                {"user_revoked": user_id}, request.client.host if request.client else "")
    return {"message": f"User {user_id} grant revoked"}
