from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import or_
from db import get_db
from models.base import Base
from models.settings import SettingsORM

import csv
import io
import json
import re
from datetime import datetime, timedelta
from typing import Optional

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
    events = Column(JSONB)

VALID_STATUSES = {"lead", "sale", "upsale", "rejected", "hold", "trash"}


def normalize_status(status: str) -> str:
    """Slugify a status to lowercase_underscore (case/space tolerant)."""
    return re.sub(r"[^a-z0-9]+", "_", str(status or "").strip().lower()).strip("_")


def all_valid_statuses(db: Session) -> set:
    """Built-ins plus custom statuses configured in the settings row."""
    statuses = set(VALID_STATUSES)
    row = db.query(SettingsORM).filter_by(name="settings").first()
    if row and row.value:
        try:
            cfg = json.loads(row.value)
            for s in cfg.get("custom_statuses") or []:
                slug = normalize_status(s.get("name") if isinstance(s, dict) else s)
                if slug:
                    statuses.add(slug)
        except Exception:
            pass
    return statuses


@router.get("/")
def get_conversions(request: Request, limit: int = 100, db: Session = Depends(get_db)):

    # fields allowed for filtering
    ALLOWED_FILTER_FIELDS = {
        "campaign_id", "offer_id", "landing_id", "status", "click_id", "external_id",
        "sub_id_1", "sub_id_2", "sub_id_3", "sub_id_4", "sub_id_5",
        "sub_id_6", "sub_id_7", "sub_id_8", "sub_id_9", "sub_id_10",
        "utm_source", "utm_campaign", "utm_creative", "traffic_source_name"
    }

    params = dict(request.query_params)
    # G55: attribution basis — click_date (default): conversions attributed to the
    # click's received_at; conversion_date: counted on the conversion received_at.
    date_basis = params.get("date_basis") or "click_date"
    date_from = params.get("date_from")
    date_to = params.get("date_to")

    click_ids_in_window = None
    if date_from and date_to and date_basis == "click_date":
        ch = request.state.ch
        try:
            res = ch.query(
                "SELECT DISTINCT visitor_id FROM clicks_data "
                "WHERE toDate(received_at) BETWEEN toDate(%(df)s) AND toDate(%(dt)s)",
                parameters={"df": date_from, "dt": date_to},
            )
            click_ids_in_window = {row[0] for row in res.result_rows}
        except Exception:
            click_ids_in_window = set()

    query = db.query(Conversion)

    # Apply the filters
    for key, value in request.query_params.items():
        if key in ALLOWED_FILTER_FIELDS:
            column = getattr(Conversion, key, None)
            if column is not None:
                query = query.filter(column == value)
        elif key in ("date_from", "date_to", "date_basis"):
            continue
        elif key == "search":
            # free-text search across the identifier columns
            term = f"%{value}%"
            query = query.filter(
                (Conversion.click_id.ilike(term)) |
                (Conversion.external_id.ilike(term)) |
                (Conversion.transaction_id.ilike(term)) |
                (Conversion.visitor_id.ilike(term))
            )

    if date_from and date_to:
        if date_basis == "conversion_date":
            query = query.filter(Conversion.received_at >= datetime.fromisoformat(date_from))
            query = query.filter(Conversion.received_at < datetime.fromisoformat(date_to) + timedelta(days=1))
        elif click_ids_in_window is not None:
            query = query.filter(Conversion.click_id.in_(click_ids_in_window or ["__none__"]))

    rows = query.order_by(Conversion.received_at.desc()).limit(limit).all()

    # Convert the ORM objects to dicts
    return [row.__dict__ for row in rows]


class ConversionImport(BaseModel):
    lines: str


def _append_event(conv: Conversion, status: str, payout: float, source: str) -> None:
    """Accumulate a conversion event onto a row (LTV semantics, mirrors the engine)."""
    events = list(conv.events) if isinstance(conv.events, list) else []
    events.append({"status": status, "payout": payout,
                   "received_at": datetime.utcnow().isoformat(), "source": source})
    conv.events = events  # new list object — JSONB needs reassignment to be flagged dirty
    conv.status = status
    conv.payout = float(conv.payout or 0) + payout
    conv.revenue = float(conv.revenue or 0) + payout
    conv.profit = conv.revenue  # conversions_data has no cost column
    conv.postback_count = (conv.postback_count or 0) + 1
    conv.last_postback_at = datetime.utcnow()


@router.post("/import")
def import_conversions(data: ConversionImport, db: Session = Depends(get_db)):
    """Manual conversion import (G25): one conversion per CSV line
    'click_id,payout,transaction_id,status'. Existing rows accumulate per the
    LTV semantics; unknown click ids create unattributed rows (click_id 'none').
    Returns a per-line result list — never fails the whole batch."""
    statuses = all_valid_statuses(db)
    results = []
    rows = [r for r in csv.reader(io.StringIO(data.lines or ""))
            if r and any(str(c).strip() for c in r)]

    for i, cells in enumerate(rows, 1):
        cells = [str(c).strip() for c in cells]
        # tolerate a header row
        if i == 1 and cells[0].lower() in ("subid", "click_id", "clickid"):
            continue
        if len(cells) < 2 or not cells[0]:
            results.append({"line": i, "ok": False,
                            "detail": "malformed line (need at least click_id,payout)"})
            continue
        subid, payout_raw = cells[0], cells[1]
        tid = cells[2] if len(cells) > 2 and cells[2] else None
        status = normalize_status(cells[3]) if len(cells) > 3 and cells[3] else "sale"
        try:
            payout = float(payout_raw)
        except ValueError:
            results.append({"line": i, "ok": False,
                            "detail": f"invalid payout '{payout_raw}'"})
            continue
        if status not in statuses:
            results.append({"line": i, "ok": False,
                            "detail": f"invalid status '{cells[3] if len(cells) > 3 else ''}'"})
            continue

        conv = db.query(Conversion).filter(or_(
            Conversion.click_id == subid,
            Conversion.external_id == subid,
            Conversion.transaction_id == subid,
        )).first()
        if conv:
            _append_event(conv, status, payout, source="import")
            if tid:
                conv.transaction_id = tid
            db.add(conv)
            results.append({"line": i, "ok": True, "detail": f"updated conversion {conv.id}"})
        else:
            conv = Conversion(
                click_id="none", status=status, payout=payout, revenue=payout, profit=payout,
                external_id=tid, postback_count=1,
                received_at=datetime.utcnow(), last_postback_at=datetime.utcnow(),
                events=[{"status": status, "payout": payout,
                         "received_at": datetime.utcnow().isoformat(), "source": "import"}])
            db.add(conv)
            db.flush()
            results.append({"line": i, "ok": True,
                            "detail": f"created unattributed conversion {conv.id}"})

    db.commit()
    imported = sum(1 for r in results if r["ok"])
    return {"results": results, "imported": imported, "failed": len(results) - imported}


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
        status = normalize_status(updates["status"])
        if status not in all_valid_statuses(db):
            raise HTTPException(status_code=400, detail=f"Invalid status. Allowed: {', '.join(sorted(all_valid_statuses(db)))}")
        conv.status = status
    # Payout drives revenue (conversions carry no cost → profit = revenue),
    # consistent with record_conversion's accumulate semantics.
    if "payout" in updates and "revenue" not in updates:
        updates["revenue"] = updates["payout"]
    for field in ("payout", "revenue"):
        if field in updates:
            setattr(conv, field, updates[field])
    for field in ("external_id", "transaction_id"):
        if field in updates:
            setattr(conv, field, updates[field])
    if "payout" in updates or "revenue" in updates:
        conv.profit = float(conv.revenue or 0)

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
