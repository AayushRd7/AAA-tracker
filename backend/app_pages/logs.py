"""Audit-log read API — the Logs area behind the sidebar's Logs section.

Four server-side event trails written by the tracking plane and the admin
plane:

* ``postback_logs``      — every inbound S2S postback (/pb), incl. rejections
* ``click_forward_logs`` — every redirect decision a click went through
* ``meta_capi_log``      — outbound CAPI delivery attempts (extended columns)
* ``cost_update_logs``   — retroactive cost updates applied from Reports

All endpoints share one shape: an optional date range plus text filters, a
``limit``/``offset`` pair (``offset`` opts into the ``{items,total}`` paged
shape, matching /api/dashboard/click-log) and ``?format=csv`` for a
formula-injection-safe, UTF-8-BOM export.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Column, DateTime, Float, Integer, String, text, func, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from db import get_db
from models.base import Base, TenantMixin

router = APIRouter()

# Rows returned by an export — a busy window can never turn one click into an
# unbounded in-memory CSV.
EXPORT_MAX = 50_000
# Rows returned by a single list call unless the caller asks for less.
LIST_MAX = 5_000


class PostbackLog(TenantMixin, Base):
    __tablename__ = "postback_logs"

    id = Column(Integer, primary_key=True)
    received_at = Column(DateTime)
    click_id = Column(String)
    status = Column(String)
    payout = Column(Float)
    transaction_id = Column(String)
    source_ip = Column(String)
    url = Column(String)
    result = Column(String)
    reason = Column(String)
    raw = Column(JSONB)


class ClickForwardLog(TenantMixin, Base):
    __tablename__ = "click_forward_logs"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime)
    click_id = Column(String)
    campaign_id = Column(Integer)
    flow_index = Column(Integer)
    schema = Column(String)
    offer_id = Column(Integer)
    destination_url = Column(String)
    status = Column(String)
    reason = Column(String)
    ip = Column(String)
    user_agent = Column(String)


class CostUpdateLog(TenantMixin, Base):
    __tablename__ = "cost_update_logs"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime)
    username = Column(String)
    campaign_id = Column(Integer)
    date_from = Column(DateTime)
    date_to = Column(DateTime)
    cost = Column(Float)
    updated_rows = Column(Integer)


class MetaCapiLog(TenantMixin, Base):
    __tablename__ = "meta_capi_log"

    id = Column(Integer, primary_key=True)
    at = Column(DateTime)
    click_id = Column(String)
    status = Column(String)
    event_name = Column(String)
    dataset_id = Column(String)
    outcome = Column(String)
    attempt = Column(Integer)
    response_status = Column(Integer)
    detail = Column(String)
    # Added by the Logs area so the outbound delivery table carries the
    # platform/pixel the attempt targeted and the HTTP response snippet.
    platform = Column(String)
    pixel_id = Column(String)
    http_status = Column(Integer)
    response_snippet = Column(String)
    attempts = Column(Integer)
    created_at = Column(DateTime)


def _csv_safe(value):
    """Prefix cells that would start a spreadsheet formula (=,+,-,@) with an
    apostrophe so exported CSVs can't smuggle live formulas into Excel/Sheets."""
    s = "" if value is None else (value if isinstance(value, str) else str(value))
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def _as_dict(row) -> dict:
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def _csv_response(rows, fields, filename):
    """CSV body with a UTF-8 BOM and the formula-injection guard applied."""
    import csv as _csv
    import io as _io
    from fastapi.responses import Response
    buf = _io.StringIO()
    writer = _csv.writer(buf, lineterminator="\n")
    writer.writerow(fields)
    for row in rows:
        d = _as_dict(row)
        writer.writerow([_csv_safe(d.get(k)) for k in fields])
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


def _parse_dt(value, label: str, end_of_day: bool = False):
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {label}: expected YYYY-MM-DD")
    # A bare date (no time part) must cover the whole day or a same-day filter
    # silently drops every row recorded after midnight.
    if end_of_day and "T" not in raw and " " not in raw:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def _date_range_column(query, column, params):
    """Apply date_from/date_to (inclusive days) when present."""
    date_from = params.get("date_from")
    date_to = params.get("date_to")
    if date_from:
        query = query.filter(column >= _parse_dt(date_from, "date_from"))
    if date_to:
        query = query.filter(column <= _parse_dt(date_to, "date_to", end_of_day=True))
    return query


def _paginate(query, model, params, order_by):
    """Bare list without `offset`; `{items,total,limit,offset}` with it."""
    limit = min(max(int(params.get("limit") or 50), 1), LIST_MAX)
    offset_param = params.get("offset")
    query = query.order_by(order_by)
    if offset_param is None:
        return [_as_dict(r) for r in query.limit(limit).all()]
    offset = max(int(offset_param or 0), 0)
    total = query.order_by(None).count()
    return {"items": [_as_dict(r) for r in query.limit(limit).offset(offset).all()],
            "total": total, "limit": limit, "offset": offset}


def _wants_csv(request: Request) -> bool:
    return (request.query_params.get("format") or "").lower() == "csv"


# ---------------------------------------------------------------------------
# S2S postbacks
# ---------------------------------------------------------------------------
POSTBACK_FIELDS = ["id", "received_at", "click_id", "status", "payout",
                   "transaction_id", "source_ip", "url", "result", "reason"]


@router.get("/postbacks")
def list_postbacks(request: Request, db: Session = Depends(get_db)):
    params = dict(request.query_params)
    query = db.query(PostbackLog)
    query = _date_range_column(query, PostbackLog.received_at, params)
    if params.get("click_id"):
        query = query.filter(PostbackLog.click_id.ilike(f"%{params['click_id']}%"))
    if params.get("transaction_id"):
        query = query.filter(PostbackLog.transaction_id.ilike(f"%{params['transaction_id']}%"))
    if params.get("status"):
        query = query.filter(PostbackLog.status == params["status"])
    if params.get("result"):
        query = query.filter(PostbackLog.result == params["result"])
    if params.get("search"):
        term = f"%{params['search']}%"
        query = query.filter(or_(PostbackLog.url.ilike(term),
                                 PostbackLog.reason.ilike(term),
                                 PostbackLog.source_ip.ilike(term)))
    if _wants_csv(request):
        return _csv_response(query.order_by(PostbackLog.received_at.desc()).limit(EXPORT_MAX).all(),
                             POSTBACK_FIELDS, "postback_logs.csv")
    return _paginate(query, PostbackLog, params, PostbackLog.received_at.desc())


# ---------------------------------------------------------------------------
# Click forwarding
# ---------------------------------------------------------------------------
FORWARD_FIELDS = ["id", "created_at", "click_id", "campaign_id", "flow_index",
                  "schema", "offer_id", "destination_url", "status", "reason",
                  "ip", "user_agent"]


@router.get("/click-forwarding")
def list_click_forwarding(request: Request, db: Session = Depends(get_db)):
    params = dict(request.query_params)
    query = db.query(ClickForwardLog)
    query = _date_range_column(query, ClickForwardLog.created_at, params)
    if params.get("click_id"):
        query = query.filter(ClickForwardLog.click_id.ilike(f"%{params['click_id']}%"))
    if params.get("campaign_id"):
        try:
            query = query.filter(ClickForwardLog.campaign_id == int(params["campaign_id"]))
        except ValueError:
            raise HTTPException(status_code=400, detail="campaign_id must be an integer")
    if params.get("status"):
        query = query.filter(ClickForwardLog.status == params["status"])
    if params.get("search"):
        term = f"%{params['search']}%"
        query = query.filter(or_(ClickForwardLog.reason.ilike(term),
                                 ClickForwardLog.destination_url.ilike(term)))
    if _wants_csv(request):
        return _csv_response(query.order_by(ClickForwardLog.created_at.desc()).limit(EXPORT_MAX).all(),
                             FORWARD_FIELDS, "click_forward_logs.csv")
    return _paginate(query, ClickForwardLog, params, ClickForwardLog.created_at.desc())


# ---------------------------------------------------------------------------
# Outbound API (CAPI) postbacks
# ---------------------------------------------------------------------------
API_FIELDS = ["id", "created_at", "click_id", "platform", "pixel_id", "event_name",
              "status", "http_status", "outcome", "attempts", "response_snippet"]


@router.get("/api-postbacks")
def list_api_postbacks(request: Request, db: Session = Depends(get_db)):
    params = dict(request.query_params)
    created = func.coalesce(MetaCapiLog.created_at, MetaCapiLog.at)
    query = db.query(MetaCapiLog)
    if params.get("date_from"):
        query = query.filter(created >= _parse_dt(params["date_from"], "date_from"))
    if params.get("date_to"):
        query = query.filter(created <= _parse_dt(params["date_to"], "date_to", end_of_day=True))
    if params.get("click_id"):
        query = query.filter(MetaCapiLog.click_id.ilike(f"%{params['click_id']}%"))
    if params.get("event_name"):
        query = query.filter(MetaCapiLog.event_name == params["event_name"])
    if params.get("status"):
        query = query.filter(MetaCapiLog.status == params["status"])
    if params.get("outcome"):
        query = query.filter(MetaCapiLog.outcome == params["outcome"])
    if _wants_csv(request):
        rows = query.order_by(created.desc()).limit(EXPORT_MAX).all()
        return _csv_response(rows, API_FIELDS, "api_postbacks.csv")
    return _paginate(query, MetaCapiLog, params, created.desc())


# ---------------------------------------------------------------------------
# Retroactive cost updates
# ---------------------------------------------------------------------------
COST_FIELDS = ["id", "created_at", "username", "campaign_id", "date_from",
               "date_to", "cost", "updated_rows"]


@router.get("/cost-updates")
def list_cost_updates(request: Request, db: Session = Depends(get_db)):
    params = dict(request.query_params)
    query = db.query(CostUpdateLog)
    query = _date_range_column(query, CostUpdateLog.created_at, params)
    if params.get("username"):
        query = query.filter(CostUpdateLog.username.ilike(f"%{params['username']}%"))
    if params.get("campaign_id"):
        try:
            query = query.filter(CostUpdateLog.campaign_id == int(params["campaign_id"]))
        except ValueError:
            raise HTTPException(status_code=400, detail="campaign_id must be an integer")
    if _wants_csv(request):
        return _csv_response(query.order_by(CostUpdateLog.created_at.desc()).limit(EXPORT_MAX).all(),
                             COST_FIELDS, "cost_update_logs.csv")
    return _paginate(query, CostUpdateLog, params, CostUpdateLog.created_at.desc())
