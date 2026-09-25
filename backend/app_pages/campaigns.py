from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from db import get_db
from models.campaigns import CampaignORM
from typing import List

from pydantic import BaseModel
from typing import Optional, Literal
from datetime import datetime, date, timedelta

from clickHouse import get_report_breakdown

router = APIRouter()

class CampaignIn(BaseModel):
    name: str
    alias: str
    type: Literal['campaign', 'tracking_only'] = 'campaign'
    status: Literal['active', 'paused', 'archived'] = 'active'
    redirect_mode: Literal['position', 'weight'] = 'position'
    traffic_source_id: Optional[int] = None
    domain_id: Optional[int] = None
    notes: Optional[str] = None
    config: Optional[dict] = None

class CampaignOut(CampaignIn):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


@router.get("/", response_model=List[CampaignOut])
def get_campaigns(db: Session = Depends(get_db)):
    return db.query(CampaignORM).order_by(CampaignORM.id.asc()).all()


@router.get("/metrics")
def get_campaign_metrics(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Per-campaign live metric columns from ClickHouse.

    Returns {campaign_id: {clicks, conversions, cost, revenue, profit, cr, epc, roi, ...}}.
    """
    filters = {"campaigns": [], "date_from": date_from, "date_to": date_to}
    try:
        rows = get_report_breakdown(request.state.ch, filters, "campaign_id")
    except Exception:
        return {}
    return {row["dimension"]: row for row in rows}


@router.post("/{campaign_id}/clone", response_model=dict)
def clone_campaign(campaign_id: int, db: Session = Depends(get_db)):
    """Duplicate a campaign — new alias derived from the original."""
    campaign = db.query(CampaignORM).filter(CampaignORM.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    base_alias = f"{campaign.alias}-copy"
    alias = base_alias
    n = 2
    while db.query(CampaignORM).filter(CampaignORM.alias == alias).first():
        alias = f"{base_alias}-{n}"
        n += 1

    clone = CampaignORM(
        name=f"{campaign.name} (copy)",
        alias=alias,
        type=campaign.type,
        status='paused',
        redirect_mode=campaign.redirect_mode,
        traffic_source_id=campaign.traffic_source_id,
        domain_id=campaign.domain_id,
        notes=campaign.notes,
        config=campaign.config,
    )
    db.add(clone)
    db.commit()
    db.refresh(clone)
    return {"message": "Campaign cloned", "id": clone.id, "alias": clone.alias}

@router.post("/", response_model=dict)
def create_campaign(data: CampaignIn, db: Session = Depends(get_db)):
    campaign = CampaignORM(**data.dict())
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return {"message": "Campaign created", "id": campaign.id}

@router.put("/{campaign_id}", response_model=CampaignOut)
def update_campaign(campaign_id: int, data: CampaignIn, db: Session = Depends(get_db)):
    campaign = db.query(CampaignORM).filter(CampaignORM.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")

    for key, value in data.dict().items():
        setattr(campaign, key, value)
    campaign.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(campaign)
    return campaign

@router.delete("/{campaign_id}")
def delete_campaign(campaign_id: int, db: Session = Depends(get_db)):
    campaign = db.query(CampaignORM).filter_by(id=campaign_id).first()
    if not campaign:
        raise HTTPException(404, detail="Campaign not found")

    db.delete(campaign)
    db.commit()
    return {"message": "Campaign deleted"}
