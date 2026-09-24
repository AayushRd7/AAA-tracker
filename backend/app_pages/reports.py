from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from datetime import datetime
from typing import List, Optional
from db import get_db
from models.base import Base

from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime

router = APIRouter()

class Conversion(Base):
    __tablename__ = "conversions_data"

    id = Column(Integer, primary_key=True, index=True)
    received_at = Column(DateTime)
    click_id = Column(String)
    campaign_id = Column(Integer)
    offer_id = Column(Integer)
    landing_id = Column(Integer)
    status = Column(String)
    external_id = Column(String)
    payout = Column(Float)
    revenue = Column(Float)
    profit = Column(Float)
    currency = Column(String)
    transaction_id = Column(String)
    country = Column(String)
    region = Column(String)
    city = Column(String)
    ip = Column(String)
    visitor_id = Column(String)
    sub_id_1 = Column(String)
    sub_id_2 = Column(String)
    sub_id_3 = Column(String)
    sub_id_4 = Column(String)
    sub_id_5 = Column(String)
    sub_id_6 = Column(String)
    sub_id_7 = Column(String)
    sub_id_8 = Column(String)
    sub_id_9 = Column(String)
    sub_id_10 = Column(String)
    utm_campaign = Column(String)
    utm_creative = Column(String)
    utm_source = Column(String)
    traffic_source_name = Column(String)
    os = Column(String)
    isp = Column(String)
    is_using_proxy = Column(Boolean)
    is_bot = Column(Boolean)
    device_type = Column(String)
    postback_count = Column(Integer)
    last_postback_at = Column(DateTime)

VALID_STATUSES = {"lead", "sale", "upsale", "rejected", "hold", "trash"}

@router.get("/")
def get_conversions(request: Request, limit: int = 100, db: Session = Depends(get_db)):

    # fields allowed for filtering
    ALLOWED_FILTER_FIELDS = {
        "campaign_id", "offer_id", "landing_id", "status", "click_id", "external_id",
        "sub_id_1", "sub_id_2", "sub_id_3", "sub_id_4", "sub_id_5",
        "sub_id_6", "sub_id_7", "sub_id_8", "sub_id_9", "sub_id_10",
        "utm_source", "utm_campaign", "utm_creative", "traffic_source_name"
    }

    query = db.query(Conversion)

    # Apply the filters
    for key, value in request.query_params.items():
        if key in ALLOWED_FILTER_FIELDS:
            column = getattr(Conversion, key, None)
            if column is not None:
                query = query.filter(column == value)
        elif key == "date_from":
            query = query.filter(Conversion.received_at >= datetime.fromisoformat(value))
        elif key == "date_to":
            query = query.filter(Conversion.received_at <= datetime.fromisoformat(value))
        elif key == "search":
            # free-text search across the identifier columns
            term = f"%{value}%"
            query = query.filter(
                (Conversion.click_id.ilike(term)) |
                (Conversion.external_id.ilike(term)) |
                (Conversion.transaction_id.ilike(term)) |
                (Conversion.visitor_id.ilike(term))
            )

    rows = query.order_by(Conversion.received_at.desc()).limit(limit).all()

    # Convert the ORM objects to dicts
    return [row.__dict__ for row in rows]


class ConversionUpdate(BaseModel):
    status: Optional[str] = None
    payout: Optional[float] = None
    revenue: Optional[float] = None
    external_id: Optional[str] = None
    transaction_id: Optional[str] = None


@router.patch("/{conversion_id}")
def update_conversion(conversion_id: int, data: ConversionUpdate, db: Session = Depends(get_db)):
    conv = db.query(Conversion).filter_by(id=conversion_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversion not found")

    updates = data.dict(exclude_none=True)
    if "status" in updates:
        if updates["status"] not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status. Allowed: {', '.join(sorted(VALID_STATUSES))}")
        conv.status = updates["status"]
    for field in ("payout", "revenue"):
        if field in updates:
            setattr(conv, field, updates[field])
    for field in ("external_id", "transaction_id"):
        if field in updates:
            setattr(conv, field, updates[field])

    db.commit()
    return {"message": "Conversion updated"}


@router.delete("/{conversion_id}")
def delete_conversion(conversion_id: int, db: Session = Depends(get_db)):
    conv = db.query(Conversion).filter_by(id=conversion_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversion not found")

    db.delete(conv)
    db.commit()
    return {"message": "Conversion deleted"}
