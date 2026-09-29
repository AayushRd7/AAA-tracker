from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, or_, and_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import or_
from sqlalchemy import text
from sqlalchemy import func, case
from db import get_db
from tenant_context import current_tenant
from models.base import Base, TenantMixin
from models.settings import SettingsORM
from models.campaigns import CampaignORM

import csv
import io
import json
import re
from datetime import datetime, timedelta
from typing import Optional, List

router = APIRouter()

class Conversion(TenantMixin, Base):
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
    approval = Column(String)
    is_duplicate = Column(Boolean)
    funnel_step = Column(Integer)
    events = Column(JSONB)

VALID_STATUSES = {"lead", "sale", "upsale", "rejected", "hold", "trash"}

# Wave 19B — reconciliation lifecycle for networks that approve conversions.
VALID_APPROVALS = {"pending", "approved", "declined", "other"}

# Upper bound on the visitor ids pulled from ClickHouse for click_date
# attribution. Above this the id set is truncated (with a loud log) so a busy
# window can never turn the conversions list/export into a giant Postgres
# IN(...) that exhausts memory.
CONVERSION_CLICK_WINDOW_ID_CAP = 50_000


def _escape_like(term: str) -> str:
    """Escape SQL LIKE wildcards so user text matches literally (Postgres's
    default escape character is the backslash)."""
    return str(term).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_iso_dt(value, label: str) -> datetime:
    """Validate an ISO date/datetime query param — a bad value used to 500 in
    fromisoformat downstream."""
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {label}: expected an ISO date (YYYY-MM-DD)")


def _conversions_scope(request: Request, db: Session, query):
    """G63/D1c parity with campaigns:'own' — a scoped caller only sees
    conversions whose campaign they own; unattributable rows (NULL campaign_id)
    are hidden from them. Admins and regular users pass through."""
    from auth import get_caller, membership_for
    username, is_admin = get_caller(request)
    if is_admin or not username:
        return query
    member = membership_for(db, username)
    raw = member[2] if member else {}
    if raw.get("campaigns") != "own" or not member:
        return query
    owned = db.query(CampaignORM.id).filter(CampaignORM.owner_id == member[0])
    return query.filter(Conversion.campaign_id.in_(owned))


def _require_conversion_access(request: Request, db: Session, conv: "Conversion") -> None:
    """Mutation counterpart of _conversions_scope: scoped users may only touch
    conversions on campaigns they own (unattributable rows are off-limits)."""
    from auth import get_caller, membership_for
    username, is_admin = get_caller(request)
    if is_admin or not username:
        return
    member = membership_for(db, username)
    raw = member[2] if member else {}
    if raw.get("campaigns") != "own" or not member:
        return
    if conv.campaign_id is None:
        raise HTTPException(status_code=403,
                            detail="You can only modify conversions of your own campaigns")
    owner_id = db.query(CampaignORM.owner_id).filter_by(id=conv.campaign_id).scalar()
    if owner_id != member[0]:
        raise HTTPException(status_code=403,
                            detail="You can only modify conversions of your own campaigns")


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
def get_conversions(request: Request, limit: int = 100, offset: Optional[int] = None,
                    db: Session = Depends(get_db)):
    query = _build_conversions_query(request, db)
    total = None
    if offset is not None:
        # Paginated mode: `offset` opts in so legacy callers keep the bare-list
        # shape; total comes from count(), never len(items).
        total = query.count()
        offset = max(int(offset or 0), 0)
    rows = (query.order_by(Conversion.received_at.desc(), Conversion.id.desc())
                 .limit(min(max(int(limit or 50), 1), 5000)))
    if offset is not None:
        rows = rows.offset(offset)
    items = [_conversion_to_dict(row) for row in rows.all()]
    if total is not None:
        return {"items": items, "total": total, "limit": limit, "offset": offset}
    return items


def _conversion_to_dict(conv: "Conversion") -> dict:
    """Serialize one conversion row, adding ``dedupe_token`` — the
    transaction/external id the dedupe guard matches on (derived, no column)."""
    item = dict(conv.__dict__)
    item.pop("_sa_instance_state", None)
    item["dedupe_token"] = conv.transaction_id or conv.external_id
    return item


CONVERSION_EXPORT_FIELDS = ["id", "received_at", "click_id", "campaign_id", "offer_id",
                            "landing_id", "status", "approval", "external_id",
                            "transaction_id", "visitor_id", "country", "payout", "revenue",
                            "profit", "currency", "postback_count"]


@router.get("/export")
def export_conversions(request: Request, db: Session = Depends(get_db)):
    """CSV export of the conversion log with the same filters as the list."""
    from fastapi.responses import Response
    query = _build_conversions_query(request, db)
    rows = query.order_by(Conversion.received_at.desc(), Conversion.id.desc()).limit(50000).all()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CONVERSION_EXPORT_FIELDS)
    for conv in rows:
        writer.writerow([_csv_safe(getattr(conv, k, None)) for k in CONVERSION_EXPORT_FIELDS])
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=conversions.csv"})


def _csv_safe(value):
    """Prefix cells that would start a spreadsheet formula (=,+,-,@) with an
    apostrophe so exported CSVs can't smuggle live formulas into Excel/Sheets."""
    s = "" if value is None else (value if isinstance(value, str) else str(value))
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def _build_conversions_query(request: Request, db: Session):
    """Full filtered conversions query shared by the list and CSV export."""
    # fields allowed for filtering
    ALLOWED_FILTER_FIELDS = {
        "campaign_id", "offer_id", "landing_id", "status", "approval", "click_id",
        "external_id",
        "sub_id_1", "sub_id_2", "sub_id_3", "sub_id_4", "sub_id_5",
        "sub_id_6", "sub_id_7", "sub_id_8", "sub_id_9", "sub_id_10",
        "utm_source", "utm_campaign", "utm_creative", "traffic_source_name"
    }

    params = dict(request.query_params)
    # G55: attribution basis — click_date (default): conversions attributed to the
    # click's received_at; conversion_date: counted on the conversion received_at.
    # The join key is the first-party visitor cookie (aaa_vid): the tracking
    # plane stamps it on both the ClickHouse click row and the Postgres
    # conversion row. visitor_id is NOT the conversion's click_id — joining on
    # that pair always yields empty.
    date_basis = params.get("date_basis") or "click_date"
    date_from = params.get("date_from")
    date_to = params.get("date_to")
    if date_from and date_to:
        # validate once up front — these flow into ClickHouse toDate() and
        # Postgres fromisoformat() below
        _parse_iso_dt(date_from, "date_from")
        _parse_iso_dt(date_to, "date_to")

    visitor_ids_in_window = None
    if date_from and date_to and date_basis == "click_date":
        ch = request.state.ch
        # G63 click_date basis: attribute each conversion to the date of the
        # CLICK it came from (visitor cookie join). The click window lives in
        # ClickHouse, conversions in Postgres, so we collect the window's
        # visitor ids and filter the PG side with an IN(...) — an unbounded set
        # here used to build a millions-element query (OOM footgun) on every
        # list page and CSV export. We keep the correct join semantics (a
        # conversion may land days after its click and must still count on the
        # click's date) but bound the id set to CONVERSION_CLICK_WINDOW_ID_CAP
        # and degrade loudly rather than silently, instead of pushing the
        # conversion's own received_at window onto the PG side — that would
        # hide exactly the late-landing conversions click_date is meant to keep.
        try:
            res = ch.query(
                "SELECT DISTINCT visitor_id FROM clicks_data "
                "WHERE tenant_id = %(tenant_id)s "
                "AND toDate(received_at) BETWEEN toDate(%(df)s) AND toDate(%(dt)s) "
                "AND visitor_id != '' "
                "LIMIT %(cap)s",
                parameters={"df": date_from, "dt": date_to,
                            "tenant_id": current_tenant(),
                            "cap": CONVERSION_CLICK_WINDOW_ID_CAP + 1},
            )
            ids = [row[0] for row in res.result_rows if row and row[0]]
            if len(ids) > CONVERSION_CLICK_WINDOW_ID_CAP:
                print(f"CONVERSIONS CLICK WINDOW CAPPED: click window "
                      f"{date_from}..{date_to} has more than "
                      f"{CONVERSION_CLICK_WINDOW_ID_CAP} visitor ids — "
                      f"click_date attribution is truncated; narrow the date range")
                ids = ids[:CONVERSION_CLICK_WINDOW_ID_CAP]
            visitor_ids_in_window = set(ids)
        except Exception as e:
            # Never silently empty the list: log loudly and fall back to the
            # legacy own-date window for NULL-visitor rows only.
            print("CONVERSIONS CLICK WINDOW ERROR: ClickHouse visitor-id lookup "
                  f"failed ({e!r}) — falling back to conversion-date window for "
                  f"{date_from}..{date_to}")
            visitor_ids_in_window = set()

    query = db.query(Conversion)
    query = _conversions_scope(request, db, query)

    # Apply the filters
    for key, value in request.query_params.items():
        if key in ALLOWED_FILTER_FIELDS:
            column = getattr(Conversion, key, None)
            if column is not None:
                query = query.filter(column == value)
        elif key in ("date_from", "date_to", "date_basis", "limit", "offset"):
            continue
        elif key == "search":
            # free-text search across the identifier columns
            term = f"%{_escape_like(value)}%"
            query = query.filter(
                (Conversion.click_id.ilike(term)) |
                (Conversion.external_id.ilike(term)) |
                (Conversion.transaction_id.ilike(term)) |
                (Conversion.visitor_id.ilike(term))
            )
        elif key == "url":
            # substring match against the bound offer's URL — conversion rows
            # carry no URL column themselves
            from models.offers import OfferORM
            term = f"%{_escape_like(value)}%"
            query = query.join(OfferORM, Conversion.offer_id == OfferORM.id) \
                         .filter(OfferORM.url.ilike(term))

    if date_from and date_to:
        if date_basis == "conversion_date":
            query = query.filter(Conversion.received_at >= _parse_iso_dt(date_from, "date_from"))
            query = query.filter(
                Conversion.received_at < _parse_iso_dt(date_to, "date_to") + timedelta(days=1))
        elif visitor_ids_in_window is not None:
            # Linked conversions: window by their CLICK date (visitor cookie).
            # Legacy rows written before visitor attribution have no link —
            # fall back to their own conversion date for the window so the log
            # doesn't silently empty out on reload.
            window_start = _parse_iso_dt(date_from, "date_from")
            window_end = _parse_iso_dt(date_to, "date_to") + timedelta(days=1)
            query = query.filter(or_(
                Conversion.visitor_id.in_(visitor_ids_in_window or ["__none__"]),
                and_(Conversion.visitor_id.is_(None),
                     Conversion.received_at >= window_start,
                     Conversion.received_at < window_end),
            ))

    return query


@router.get("/funnel/{campaign_id}")
def get_funnel_report(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    """G10 funnel report: per-step visits/click-outs/conversions for a funnel
    campaign. Visits come from ClickHouse (flow_index IS the step index for
    funnel campaigns); conversion-side aggregates from conversions_data
    (funnel_step = the step at click-out time). Non-funnel campaigns 404."""
    row = db.execute(
        text("SELECT id, name, config, owner_id FROM campaigns "
             "WHERE id = :cid AND tenant_id = :tid"),
        {"cid": campaign_id, "tid": current_tenant()}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # campaigns:'own' parity with the list/export: a scoped caller only reads
    # funnels of campaigns they own (404, since it's invisible to them).
    from auth import get_caller, membership_for
    username, is_admin = get_caller(request)
    if not is_admin and username:
        member = membership_for(db, username)
        if member and (member[2] or {}).get("campaigns") == "own" \
                and row["owner_id"] != member[0]:
            raise HTTPException(status_code=404, detail="Campaign not found")

    raw_config = row["config"]
    if isinstance(raw_config, dict):
        config = raw_config
    else:
        try:
            config = json.loads(raw_config or "{}")
        except (TypeError, json.JSONDecodeError):
            config = {}
    funnel = config.get("funnel") or {}
    steps_cfg = [s for s in (funnel.get("steps") or []) if isinstance(s, dict)]
    if not funnel.get("enabled") or not steps_cfg:
        raise HTTPException(status_code=404, detail="Campaign has no active funnel")

    # Visits per step — funnel campaigns stamp the step into flow_index.
    visits_by_step: dict = {}
    ch = request.state.ch
    try:
        res = ch.query(
            "SELECT flow_index, count() FROM clicks_data "
            "WHERE campaign_id = %(cid)s AND tenant_id = %(tenant_id)s "
            "GROUP BY flow_index",
            parameters={"cid": campaign_id, "tenant_id": current_tenant()})
        visits_by_step = {int(r[0]): int(r[1]) for r in res.result_rows}
    except Exception:
        visits_by_step = {}

    # Click-out rows per step. Every conversions_data row originates from a
    # click-out at /c, so COUNT(*) is the click-out count (status was 'lead'
    # at insert time; a postback may later change it, the row still counts).
    # Conversions = rows whose current status is not rejected/trash.
    agg_rows = db.execute(text("""
        SELECT funnel_step,
               COUNT(*) AS clickouts,
               COUNT(*) FILTER (WHERE status NOT IN ('rejected', 'trash')) AS conversions,
               COALESCE(SUM(revenue) FILTER (WHERE status NOT IN ('rejected', 'trash')), 0) AS revenue,
               COALESCE(SUM(profit)  FILTER (WHERE status NOT IN ('rejected', 'trash')), 0) AS profit
        FROM conversions_data
        WHERE tenant_id = :tid AND campaign_id = :cid AND funnel_step IS NOT NULL
        GROUP BY funnel_step
    """), {"cid": campaign_id, "tid": current_tenant()}).mappings().all()
    agg_by_step = {int(r["funnel_step"]): r for r in agg_rows}

    steps_out = []
    cumulative_conversions = 0
    prev_visits = None
    for i, step in enumerate(steps_cfg):
        visits = visits_by_step.get(i, 0)
        agg = agg_by_step.get(i, {})
        clickouts = int(agg.get("clickouts") or 0)
        conversions = int(agg.get("conversions") or 0)
        cumulative_conversions += conversions
        step_out = {
            "step": i,
            "name": step.get("name") or f"Step {i + 1}",
            "landing_id": step.get("landing"),
            "offers": step.get("offers") or [],
            "schema": step.get("schema") or "landing_offer",
            "visits": visits,
            "clickouts": clickouts,
            "conversions": conversions,
            "revenue": float(agg.get("revenue") or 0),
            "profit": float(agg.get("profit") or 0),
            "cr": (clickouts / visits) if visits else 0,
            "cumulative_conversions": cumulative_conversions,
            "cumulative_cr": (cumulative_conversions / visits_by_step.get(0, 0))
                             if visits_by_step.get(0) else 0,
            "drop_off_pct": None if prev_visits in (None, 0)
                            else round(100 * (prev_visits - visits) / prev_visits, 2),
        }
        steps_out.append(step_out)
        prev_visits = visits

    return {"campaign_id": campaign_id, "campaign_name": row["name"],
            "funnel": {"enabled": True}, "steps": steps_out}


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
def import_conversions(data: ConversionImport, request: Request,
                       db: Session = Depends(get_db)):
    """Manual conversion import (G25): one conversion per CSV line
    'click_id,payout,transaction_id,status'. Existing rows accumulate per the
    LTV semantics; unknown click ids create unattributed rows (click_id 'none').
    Returns a per-line result list — never fails the whole batch."""
    from audit_logger import audit_event
    from auth import get_caller
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
            payout = float(payout_raw.strip().replace(",", "."))
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
            # This row was matched by the dedupe lookup and accumulated onto
            # instead of inserting a new one — the dedupe path.
            conv.is_duplicate = True
            if tid:
                conv.transaction_id = tid
            db.add(conv)
            results.append({"line": i, "ok": True, "detail": f"updated conversion {conv.id}"})
        else:
            conv = Conversion(
                click_id="none", status=status, payout=payout, revenue=payout, profit=payout,
                external_id=tid, postback_count=1,
                approval="pending", is_duplicate=False,
                received_at=datetime.utcnow(), last_postback_at=datetime.utcnow(),
                events=[{"status": status, "payout": payout,
                         "received_at": datetime.utcnow().isoformat(), "source": "import"}])
            db.add(conv)
            db.flush()
            results.append({"line": i, "ok": True,
                            "detail": f"created unattributed conversion {conv.id}"})

    db.commit()
    imported = sum(1 for r in results if r["ok"])
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "import", "conversions", "bulk",
                {"imported": imported, "failed": len(results) - imported},
                request.client.host if request.client else "")
    return {"results": results, "imported": imported, "failed": len(results) - imported}


class ConversionUpdate(BaseModel):
    status: Optional[str] = None
    approval: Optional[str] = None
    payout: Optional[float] = None
    revenue: Optional[float] = None
    external_id: Optional[str] = None
    transaction_id: Optional[str] = None


def normalize_approval(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


@router.patch("/{conversion_id}")
def update_conversion(conversion_id: int, data: ConversionUpdate,
                      request: Request, db: Session = Depends(get_db)):
    conv = db.query(Conversion).filter_by(id=conversion_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversion not found")
    _require_conversion_access(request, db, conv)

    updates = data.dict(exclude_none=True)
    if "status" in updates:
        status = normalize_status(updates["status"])
        if status not in all_valid_statuses(db):
            raise HTTPException(status_code=400, detail=f"Invalid status. Allowed: {', '.join(sorted(all_valid_statuses(db)))}")
        conv.status = status
    if "approval" in updates:
        approval = normalize_approval(updates["approval"])
        if approval not in VALID_APPROVALS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid approval. Allowed: {', '.join(sorted(VALID_APPROVALS))}")
        conv.approval = approval
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


class BulkConversionIds(BaseModel):
    ids: List[int]
    approval: Optional[str] = None
    status: Optional[str] = None


def _resolve_conversions(db: Session, ids: List[int]) -> List["Conversion"]:
    clean = [int(i) for i in (ids or [])]
    if not clean:
        return []
    return db.query(Conversion).filter(Conversion.id.in_(clean)).all()


@router.post("/bulk-approval")
def bulk_set_approval(data: BulkConversionIds, request: Request, db: Session = Depends(get_db)):
    """Bulk-set the reconciliation approval on the given conversion ids."""
    from audit_logger import audit_event
    from auth import get_caller
    approval = normalize_approval(data.approval)
    if approval not in VALID_APPROVALS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid approval. Allowed: {', '.join(sorted(VALID_APPROVALS))}")
    rows = _resolve_conversions(db, data.ids)
    applied = []
    for conv in rows:
        _require_conversion_access(request, db, conv)
        conv.approval = approval
        applied.append(conv.id)
    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "conversions", "bulk",
                {"action": "approval", "approval": approval, "ids": applied},
                request.client.host if request.client else "")
    return {"message": f"Approval set to {approval}", "updated": len(applied), "ids": applied}


@router.post("/bulk-status")
def bulk_set_status(data: BulkConversionIds, request: Request, db: Session = Depends(get_db)):
    """Bulk-set the conversion status on the given ids (same validation as the
    single-conversion PATCH: built-ins plus configured custom statuses)."""
    from audit_logger import audit_event
    from auth import get_caller
    status = normalize_status(data.status)
    valid = all_valid_statuses(db)
    if status not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Allowed: {', '.join(sorted(valid))}")
    rows = _resolve_conversions(db, data.ids)
    applied = []
    for conv in rows:
        _require_conversion_access(request, db, conv)
        conv.status = status
        applied.append(conv.id)
    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "conversions", "bulk",
                {"action": "status", "status": status, "ids": applied},
                request.client.host if request.client else "")
    return {"message": f"Status set to {status}", "updated": len(applied), "ids": applied}


class ConversionCreate(BaseModel):
    """Operator-entered offline conversion (manual add — no CSV)."""
    status: Optional[str] = None
    approval: Optional[str] = None
    payout: Optional[float] = None
    revenue: Optional[float] = None
    click_id: Optional[str] = None
    external_id: Optional[str] = None
    transaction_id: Optional[str] = None
    sub_ids: Optional[dict] = None


@router.post("/conversion")
def create_conversion(data: ConversionCreate, request: Request, db: Session = Depends(get_db)):
    """Create (or accumulate onto) a single conversion from the manual-add
    dialog. Mirrors import_conversions' LTV dedupe semantics but accepts the
    richer field set (revenue, sub ids) the dialog offers."""
    from audit_logger import audit_event
    from auth import get_caller
    status = normalize_status(data.status) if data.status else "sale"
    if status not in all_valid_statuses(db):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Allowed: {', '.join(sorted(all_valid_statuses(db)))}")
    approval = normalize_approval(data.approval) if data.approval else "pending"
    if approval not in VALID_APPROVALS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid approval. Allowed: {', '.join(sorted(VALID_APPROVALS))}")
    click_id = str(data.click_id or "").strip()
    external_id = str(data.external_id or "").strip() or None
    transaction_id = str(data.transaction_id or "").strip() or None
    if not (click_id or external_id or transaction_id):
        raise HTTPException(
            status_code=400,
            detail="Provide a click id, external id or transaction id")
    payout = float(data.payout or 0)
    revenue = float(data.revenue) if data.revenue is not None else payout

    sub_fields = {}
    for k, v in (data.sub_ids or {}).items():
        if re.fullmatch(r"sub_id_\d{1,2}", str(k)) and v not in (None, ""):
            sub_fields[str(k)] = str(v)[:50]

    conv = None
    match_conds = []
    if click_id:
        match_conds.append(Conversion.click_id == click_id)
    if external_id:
        match_conds.append(Conversion.external_id == external_id)
    if transaction_id:
        match_conds.append(Conversion.transaction_id == transaction_id)
    if match_conds:
        conv = db.query(Conversion).filter(or_(*match_conds)).first()

    now = datetime.utcnow()
    if conv:
        _append_event(conv, status, payout, source="manual")
        conv.is_duplicate = True
        if transaction_id:
            conv.transaction_id = transaction_id
        if external_id:
            conv.external_id = external_id
        if data.revenue is not None:
            conv.revenue = revenue
            conv.profit = revenue
        if data.approval:
            conv.approval = approval
        for k, v in sub_fields.items():
            setattr(conv, k, v)
        created = False
    else:
        conv = Conversion(
            click_id=click_id or "none", status=status, approval=approval,
            is_duplicate=False,
            payout=payout, revenue=revenue, profit=revenue,
            external_id=external_id, transaction_id=transaction_id,
            postback_count=1, received_at=now, last_postback_at=now,
            events=[{"status": status, "payout": payout,
                     "received_at": now.isoformat(), "source": "manual"}],
            **sub_fields)
        db.add(conv)
        db.flush()
        created = True

    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "create", "conversions", str(conv.id),
                {"source": "manual", "created": created, "status": status,
                 "approval": approval},
                request.client.host if request.client else "")
    return {"message": "Conversion created" if created else "Conversion updated",
            "id": conv.id, "created": created}


@router.get("/summary")
def conversions_summary(request: Request, db: Session = Depends(get_db)):
    """Approval reconciliation totals over the same filters as the list:
    pending / approved / declined / other plus approval and decline rates
    (percentage, divide-by-zero safe). Aggregated in Postgres."""
    query = _build_conversions_query(request, db)
    approval_col = func.coalesce(Conversion.approval, "pending")
    total, approved, declined, pending, other = query.with_entities(
        func.count(Conversion.id),
        func.sum(case((approval_col == "approved", 1), else_=0)),
        func.sum(case((approval_col == "declined", 1), else_=0)),
        func.sum(case((approval_col == "pending", 1), else_=0)),
        func.sum(case((approval_col == "other", 1), else_=0)),
    ).one()
    total = int(total or 0)
    approved = int(approved or 0)
    declined = int(declined or 0)
    return {
        "total": total,
        "approved": approved,
        "declined": declined,
        "pending": int(pending or 0),
        "other": int(other or 0),
        "approval_rate": round(approved / total * 100, 2) if total else 0.0,
        "decline_rate": round(declined / total * 100, 2) if total else 0.0,
    }


@router.delete("/{conversion_id}")
def delete_conversion(conversion_id: int, request: Request, db: Session = Depends(get_db)):
    conv = db.query(Conversion).filter_by(id=conversion_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversion not found")
    _require_conversion_access(request, db, conv)

    db.delete(conv)
    db.commit()
    return {"message": "Conversion deleted"}
