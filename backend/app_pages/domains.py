from fastapi import APIRouter, Depends, HTTPException, Request
from enum import Enum
from pydantic import BaseModel
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select

from db import get_db
from models.domain import DomainORM
import httpx
from datetime import datetime
from pathlib import Path

router = APIRouter()


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
async def get_domains(db: Session = Depends(get_db)):
    domains = db.query(DomainORM).order_by(DomainORM.id.asc()).all()
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
        for domain in domains
    ]


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
