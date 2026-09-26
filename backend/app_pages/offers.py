from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from db import get_db
from models.offers import OfferORM

from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime

router = APIRouter()

class OfferIn(BaseModel):
    name: str
    url: str
    affiliate_network_id: Optional[int] = None
    countries: Optional[List[Dict[str, Any]]] = []
    payout: Optional[float] = 0
    currency: Optional[str] = "USD"
    status: Optional[str] = "active"
    tokens: Optional[Dict[str, Any]] = {}
    notes: Optional[str] = ''
    tags: Optional[List[str]] = []
    daily_conversions_cap: Optional[int] = None
    overflow_offer_id: Optional[int] = None

class OfferBulkIn(BaseModel):
    ids: List[int]
    action: str  # 'tags_add' | 'tags_remove' | 'archive' | 'delete'
    tags: Optional[List[str]] = None

class OfferImportIn(BaseModel):
    lines: str

class OfferOut(OfferIn):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


def _client_ip(request: Request) -> str:
    return request.client.host if request and request.client else ""


OFFER_EXPORT_FIELDS = ["id", "name", "url", "affiliate_network_id", "countries",
                       "payout", "currency", "status", "tags", "daily_conversions_cap",
                       "overflow_offer_id", "notes"]


@router.get("/", response_model=List[OfferOut])
def get_offers(db: Session = Depends(get_db)):
    return db.query(OfferORM).order_by(OfferORM.id.desc()).all()

@router.post("/")
def create_offer(offer: OfferIn, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    new_offer = OfferORM(**offer.dict())
    db.add(new_offer)
    try:
        db.commit()
        db.refresh(new_offer)
        from auth import get_caller
        caller, _ = get_caller(request)
        audit_event(caller or "api_token", "create", "offers", str(new_offer.id),
                    {"name": new_offer.name}, _client_ip(request))
        return {"message": "Offer created", "id": new_offer.id}
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Offer with this name already exists")

@router.patch("/{offer_id}")
def update_offer(offer_id: int, offer: OfferIn, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    db_offer = db.query(OfferORM).filter_by(id=offer_id).first()
    if not db_offer:
        raise HTTPException(status_code=404, detail="Offer not found")

    changed = []
    for key, value in offer.dict().items():
        if getattr(db_offer, key, None) != value:
            changed.append(key)
        setattr(db_offer, key, value)

    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "offers", str(offer_id),
                {"fields": changed}, _client_ip(request))
    return {"message": "Offer updated"}

@router.delete("/{offer_id}")
def delete_offer(offer_id: int, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    db_offer = db.query(OfferORM).filter_by(id=offer_id).first()
    if not db_offer:
        raise HTTPException(status_code=404, detail="Offer not found")

    db.delete(db_offer)
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "delete", "offers", str(offer_id),
                {"name": db_offer.name}, _client_ip(request))
    return {"message": "Offer deleted"}


@router.post("/bulk", response_model=dict)
def bulk_offers(data: OfferBulkIn, request: Request, db: Session = Depends(get_db)):
    """G67 bulk actions: tags_add / tags_remove / archive / delete over a set of ids."""
    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)
    offers = db.query(OfferORM).filter(OfferORM.id.in_(data.ids)).all()
    clean = sorted({str(t).strip() for t in (data.tags or []) if str(t).strip()})

    if data.action == 'tags_add':
        for o in offers:
            o.tags = sorted(set(o.tags or []) | set(clean))
        db.commit()
        detail = {"bulk": "tags_add", "tags": clean, "count": len(offers)}
    elif data.action == 'tags_remove':
        for o in offers:
            o.tags = sorted(set(o.tags or []) - set(clean))
        db.commit()
        detail = {"bulk": "tags_remove", "tags": clean, "count": len(offers)}
    elif data.action == 'archive':
        db.execute(text("UPDATE offers SET archived = true WHERE id = ANY(:ids)"),
                   {"ids": [o.id for o in offers]})
        db.commit()
        detail = {"bulk": "archive", "count": len(offers)}
    elif data.action == 'delete':
        for o in offers:
            db.delete(o)
        db.commit()
        detail = {"bulk": "delete", "count": len(offers)}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action '{data.action}'")

    audit_event(caller or "api_token", "update" if data.action != "delete" else "delete",
                "offers", ",".join(map(str, data.ids)), detail, _client_ip(request))
    return {"message": f"Bulk {data.action} applied to {len(offers)} offers",
            "updated": len(offers)}


def _csv_safe(value):
    """Prefix cells that would start a spreadsheet formula (=,+,-,@) with an
    apostrophe so exported CSVs can't smuggle live formulas into Excel/Sheets.
    str-subclass values (e.g. CampaignType) are used as-is — str() on a
    (str, Enum) member yields 'CampaignType.campaign', breaking round-trips."""
    s = "" if value is None else (value if isinstance(value, str) else str(value))
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


@router.get("/export")
def export_offers(db: Session = Depends(get_db)):
    """G67 CSV export (tags ;-separated, countries as ISO codes)."""
    import csv as _csv
    import io as _io
    from fastapi.responses import Response
    rows = db.query(OfferORM).order_by(OfferORM.id.asc()).all()
    buf = _io.StringIO()
    writer = _csv.writer(buf, lineterminator="\n")
    writer.writerow(OFFER_EXPORT_FIELDS)
    for o in rows:
        codes = ";".join((c or {}).get("code", "") for c in (o.countries or []))
        writer.writerow([
            _csv_safe(v) for v in (
                o.id, o.name, o.url, o.affiliate_network_id or "", codes,
                float(o.payout or 0), o.currency or "USD", o.status or "active",
                ";".join(o.tags or []), o.daily_conversions_cap or "",
                o.overflow_offer_id or "", o.notes or "",
            )
        ])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=offers.csv"})


@router.post("/import", response_model=dict)
def import_offers(data: OfferImportIn, request: Request, db: Session = Depends(get_db)):
    """G67 CSV import — one offer per line, matched by id (preferred) or name.
    Never fails the batch: every line reports its own result."""
    import csv as _csv
    import io as _io
    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)

    results = []
    rows = [r for r in _csv.reader(_io.StringIO(data.lines or ""))
            if r and any(str(c).strip() for c in r)]
    if not rows:
        return {"results": [], "imported": 0, "failed": 0}

    first = [str(h).strip().lower() for h in rows[0]]
    if "name" in first:
        header = first
        body = [(i + 2, r) for i, r in enumerate(rows[1:])]
    else:
        header = OFFER_EXPORT_FIELDS
        body = [(i + 1, r) for i, r in enumerate(rows)]

    def cell(row, key):
        try:
            return str(row[header.index(key)]).strip()
        except (ValueError, IndexError):
            return ""

    created = 0
    for line_no, row in body:
        name = cell(row, "name")
        url = cell(row, "url")
        if not name or not url:
            results.append({"line": line_no, "ok": False,
                            "detail": "missing name or url"})
            continue
        offer_id = cell(row, "id")
        offer = None
        if offer_id and offer_id.isdigit():
            offer = db.query(OfferORM).filter(OfferORM.id == int(offer_id)).first()
        if offer is None:
            offer = db.query(OfferORM).filter(OfferORM.name == name).first()

        try:
            payout = float(cell(row, "payout") or 0)
        except ValueError:
            results.append({"line": line_no, "ok": False,
                            "detail": f"invalid payout '{cell(row, 'payout')}'"})
            continue
        countries = [{"code": c} for c in
                     cell(row, "countries").replace(",", ";").split(";") if c.strip()]
        cap = cell(row, "daily_conversions_cap")
        fields = {
            "name": name,
            "url": url,
            "affiliate_network_id": int(cell(row, "affiliate_network_id"))
                if cell(row, "affiliate_network_id").isdigit() else None,
            "countries": countries,
            "payout": payout,
            "currency": cell(row, "currency") or "USD",
            "status": cell(row, "status") or "active",
            "tags": sorted({t.strip() for t in cell(row, "tags").replace(",", ";").split(";")
                            if t.strip()}),
            "daily_conversions_cap": int(cap) if cap.isdigit() else None,
            "overflow_offer_id": int(cell(row, "overflow_offer_id"))
                if cell(row, "overflow_offer_id").isdigit() else None,
            "notes": cell(row, "notes") or '',
        }

        if offer is not None:
            for key, value in fields.items():
                setattr(offer, key, value)
            db.commit()
            results.append({"line": line_no, "ok": True,
                            "detail": f"updated offer {offer.id}"})
        else:
            offer = OfferORM(name=name, tokens={},
                             **{k: v for k, v in fields.items() if k != "name"})
            db.add(offer)
            try:
                db.commit()
                created += 1
                results.append({"line": line_no, "ok": True,
                                "detail": f"created offer {offer.id}"})
            except IntegrityError:
                db.rollback()
                results.append({"line": line_no, "ok": False,
                                "detail": "offer with this name already exists"})
            except Exception as e:
                db.rollback()
                results.append({"line": line_no, "ok": False, "detail": f"create failed: {e}"})

    audit_event(caller or "api_token", "update", "offers", "import",
                {"created": created, "lines": len(results)}, _client_ip(request))
    ok_count = sum(1 for r in results if r["ok"])
    return {"results": results, "imported": ok_count, "failed": len(results) - ok_count}
