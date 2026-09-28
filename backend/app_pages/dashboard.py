from datetime import date as date_cls, timedelta
from datetime import datetime as datetime_cls

from fastapi import APIRouter, Request, HTTPException, Depends
from clickHouse import (
    get_recent_visits, get_metrics_series, get_report_breakdown, get_report_breakdown_multi,
    get_click_log, get_click_log_total, get_live_clicks, apply_custom_metrics, sum_rows,
    REPORT_DIMENSIONS,
)
from schemas import Filters
from sqlalchemy.orm import Session
from sqlalchemy import func, case
from db import get_db
from models.user import UserORM
from models.campaigns import CampaignORM

from pydantic import BaseModel
from typing import Optional, List

router = APIRouter()

# Unguarded router for public/shared content (mounted in app.py WITHOUT
# require_api_auth). Only the saved-report share endpoint lives here — it
# serves exactly one saved report's breakdown data and nothing else.
public_router = APIRouter()


class ReportFilters(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    campaigns: List[int] = []


class CustomMetric(BaseModel):
    name: str
    formula: str


class ReportRequest(BaseModel):
    dimension: Optional[str] = None                # legacy single-dimension mode
    dimensions: Optional[List[str]] = None         # G47 multi-dimension drill-down
    filters: ReportFilters = ReportFilters()
    date_basis: Optional[str] = None               # G55: click_date | conversion_date
    custom_metrics: List[CustomMetric] = []
    limit: int = 1000
    page: int = 1
    sort_by: Optional[str] = None
    sort_dir: str = "desc"


class ClickLogFilters(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    campaigns: List[int] = []
    country: Optional[str] = None
    device_type: Optional[str] = None
    os: Optional[str] = None
    browser: Optional[str] = None
    traffic_source_name: Optional[str] = None
    status: Optional[str] = None
    search: Optional[str] = None
    fraud_score: Optional[int] = None
    limit: Optional[int] = 500   # explicit cap; None → server default
    offset: Optional[int] = None  # presence switches the response to {items, total}


# Dimensions conversions_data can be grouped by for the conversion_date basis (G55).
# Anything else falls back to click_date with an explanatory note.
CONVERSION_BASIS_DIMENSIONS = {
    "campaign_id", "offer_id", "sub_id_1", "sub_id_2", "sub_id_3", "sub_id_4",
    "sub_id_5", "sub_id_6", "sub_id_7", "sub_id_8", "sub_id_9", "sub_id_10",
    "utm_source", "utm_campaign", "utm_creative", "traffic_source_name", "status",
}


def _require_iso_dates(date_from, date_to):
    """Reject non-ISO dates with 400 before they reach ClickHouse / Postgres
    (an unvalidated value used to surface as a raw 500)."""
    for label, value in (("date_from", date_from), ("date_to", date_to)):
        if value:
            try:
                date_cls.fromisoformat(str(value))
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid {label}: expected an ISO date (YYYY-MM-DD)")


def get_conversion_aggregates(db: Session, filters: dict, dimension: str,
                              campaign_scope: Optional[List[int]] = None) -> dict:
    """Group Postgres conversions_data by `dimension` for the date window.

    campaign_scope mirrors the campaigns:'own' click scope: None = no filter
    (admin/unscoped), a possibly-empty list = only those campaign ids.
    Returns {value_str: {leads, conversions, rejected, revenue, profit}}.
    """
    from app_pages.reports import Conversion

    column = getattr(Conversion, dimension, None)
    if column is None:
        return {}

    query = db.query(
        column,
        func.count(),
        func.sum(case((Conversion.status.in_(["lead", "sale"]), 1), else_=0)),
        func.sum(case((Conversion.status.in_(["sale", "upsale"]), 1), else_=0)),
        func.sum(case((Conversion.status == "rejected", 1), else_=0)),
        func.sum(func.coalesce(Conversion.revenue, 0)),
        func.sum(func.coalesce(Conversion.profit, 0)),
    )
    if campaign_scope is not None:
        if not campaign_scope:
            return {}
        query = query.filter(Conversion.campaign_id.in_(campaign_scope))
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    if date_from:
        query = query.filter(Conversion.received_at >= date_cls.fromisoformat(date_from))
    if date_to:
        query = query.filter(Conversion.received_at < date_cls.fromisoformat(date_to) + timedelta(days=1))

    out = {}
    for value, total, leads, conv, rejected, revenue, profit in query.group_by(column).all():
        out[str(value)] = {
            "leads": int(leads or 0),
            "conversions": int(conv or 0),
            "rejected": int(rejected or 0),
            "revenue": round(float(revenue or 0), 4),
            "profit": round(float(profit or 0), 4),
        }
    return out


@router.post("/visits")
async def get_visits(
        request: Request,
        filters: Filters,
        db: Session = Depends(get_db)
):
    try:
        _require_iso_dates(filters.date_from, filters.date_to)
        f = filters.dict()
        if not _apply_click_scope(f, _click_scope_campaign_ids(request, db)):
            return []
        ch = request.state.ch
        rows = get_recent_visits(ch, f)
        return rows
    except HTTPException:
        raise
    except Exception as e:
        print("dashboard visits error:", repr(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/live-clicks")
async def live_clicks(request: Request, after: Optional[str] = None, limit: int = 20,
                      db: Session = Depends(get_db)):
    """G51: raw live click feed. Latest rows first; ``after`` (ISO timestamp)
    polls for rows newer than the last seen one."""
    if after:
        try:
            datetime_cls.fromisoformat(after.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid 'after' timestamp — expected ISO format")
    try:
        ch = request.state.ch
        limit = min(max(limit, 1), 100)
        scope = _click_scope_campaign_ids(request, db)
        if scope is None:
            return get_live_clicks(ch, after=after, limit=limit)
        if not scope:
            return []
        # get_live_clicks has no campaign filter — over-fetch and filter here
        allowed = set(scope)
        rows = get_live_clicks(ch, after=after, limit=min(limit * 5, 500))
        return [r for r in rows if r.get("campaign_id") in allowed][:limit]
    except HTTPException:
        raise
    except Exception as e:
        print("dashboard live-clicks error:", repr(e))
        raise HTTPException(status_code=500, detail="Internal server error")


def _click_scope_campaign_ids(request: Request, db: Session) -> Optional[List[int]]:
    """campaigns:'own' parity with reports.py: scoped callers are limited to
    clicks on campaigns they own. Returns None for admins and unscoped users;
    a (possibly empty) campaign-id list for scoped ones."""
    from auth import get_caller
    username, is_admin = get_caller(request)
    if is_admin or not username:
        return None
    user = db.query(UserORM).filter(UserORM.username == username).first()
    raw = (user.permissions or {}) if user else {}
    if raw.get("campaigns") != "own":
        return None
    return [r[0] for r in db.query(CampaignORM.id)
                    .filter(CampaignORM.owner_id == user.id).all()]


def _apply_click_scope(f: dict, scope: Optional[List[int]]):
    """Narrow the click-log filter dict to the caller's own campaigns.
    Returns True when the query can run, False when the scope is empty (the
    caller must get an empty result — an empty 'campaigns' list would mean
    'no filter' downstream)."""
    if scope is None:
        return True
    if not scope:
        return False
    requested = set(f.get("campaigns") or [])
    f["campaigns"] = sorted(requested & set(scope)) if requested else list(scope)
    return True


def _series_payload(series):
    return {
        "labels": [str(row["day"]) for row in series],
        "visits": [row["visits"] for row in series],
        "unique_visits": [row["unique_visits"] for row in series],
        "clicks": [row["clicks"] for row in series],
        "conversions": [row["conversions"] for row in series],
        "unique_clicks": [row["unique_clicks"] for row in series],
    }


def _totals_from_series(series):
    total_visits = sum(row['visits'] for row in series)
    total_unique_visits = sum(row['unique_visits'] for row in series)
    total_clicks = sum(row['clicks'] for row in series)
    total_unique_clicks = sum(row['unique_clicks'] for row in series)
    total_conversions = sum(row['conversions'] for row in series)
    total_cost = sum(row['cost'] or 0 for row in series)
    total_revenue = sum(row['revenue'] or 0 for row in series)
    roi = f"{round(((total_revenue - total_cost) / total_cost * 100), 2)}%" if total_cost else "—"
    return {
        "visits": total_visits,
        "unique_visits": total_unique_visits,
        "clicks": total_clicks,
        "unique_clicks": total_unique_clicks,
        "conversions": total_conversions,
        "cost": round(total_cost, 2),
        "revenue": round(total_revenue, 2),
        "roi": roi,
    }


class MetricsRequest(Filters):
    """Typed body for POST /dashboard/metrics (previously parsed from the raw
    request, so non-JSON/array bodies came back as a 500)."""
    compare: bool = False


@router.post("/metrics")
async def get_metrics(request: Request, body: MetricsRequest,
                      db: Session = Depends(get_db)):
    _require_iso_dates(body.date_from, body.date_to)
    try:
        f = body.dict(exclude={"compare"})
        scope = _click_scope_campaign_ids(request, db)
        if not _apply_click_scope(f, scope):
            # campaigns:'own' with no owned campaigns — all-zero series over
            # the same window (an empty campaigns list would mean 'no filter')
            return {"metrics": _totals_from_series([]), "chart": _series_payload([])}
        filters = Filters(**f)
        ch = request.state.ch

        compare = body.compare
        series = get_metrics_series(ch, filters)

        payload = {
            "metrics": _totals_from_series(series),
            "chart": _series_payload(series),
        }

        # G50: period comparison — same-length window immediately before the selected range
        if compare and filters.date_from and filters.date_to:
            start = date_cls.fromisoformat(filters.date_from)
            end = date_cls.fromisoformat(filters.date_to)
            length = (end - start).days + 1
            prev_end = start - timedelta(days=1)
            prev_start = prev_end - timedelta(days=length - 1)
            prev_filters = filters.copy(update={
                "date_from": prev_start.isoformat(),
                "date_to": prev_end.isoformat(),
            })
            prev_series = get_metrics_series(ch, prev_filters)
            payload["previous"] = {
                "from": prev_start.isoformat(),
                "to": prev_end.isoformat(),
                "metrics": _totals_from_series(prev_series),
                "chart": _series_payload(prev_series),
            }

        return payload
    except HTTPException:
        raise
    except Exception as e:
        print("dashboard metrics error:", repr(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/dimensions")
async def get_dimensions():
    """Available breakdown dimensions for the report builder."""
    return [{"key": k, "label": k.replace("_", " ")} for k in REPORT_DIMENSIONS]


@router.post("/breakdown")
async def get_breakdown(request: Request, body: ReportRequest, db: Session = Depends(get_db)):
    try:
        ch = request.state.ch
        dimensions = body.dimensions or ([body.dimension] if body.dimension else [])
        if not dimensions:
            raise ValueError("No dimensions given")
        limit = min(max(body.limit, 1), 5000)

        date_basis = body.date_basis or "click_date"
        fallback_note = None
        if date_basis not in ("click_date", "conversion_date"):
            raise ValueError(f"Unknown date_basis: {date_basis}")

        _require_iso_dates(body.filters.date_from, body.filters.date_to)
        filters = body.filters.dict()
        scope = _click_scope_campaign_ids(request, db)
        if not _apply_click_scope(filters, scope):
            # campaigns:'own' with no owned campaigns — empty breakdown
            return {
                "dimension": dimensions[0],
                "dimensions": dimensions,
                "date_basis": date_basis,
                "fallback_note": fallback_note,
                "rows": [],
                "totals": sum_rows([]),
            }
        if date_basis == "conversion_date":
            unsupported = [d for d in dimensions if d not in CONVERSION_BASIS_DIMENSIONS]
            if unsupported:
                date_basis = "click_date"
                fallback_note = (
                    "conversion_date basis is not available for "
                    + ", ".join(unsupported)
                    + " — fell back to click_date"
                )

        rows = get_report_breakdown_multi(
            ch, filters, dimensions, limit=limit, page=body.page,
            sort_by=body.sort_by, sort_dir=body.sort_dir,
        )

        # G55 conversion_date basis: replace conversion metrics from Postgres
        # (conversions counted on conversion received_at) and add synthetic
        # rows for conversions whose clicks fall outside the click window.
        if date_basis == "conversion_date":
            per_level_dim = {lvl: dim for lvl, dim in enumerate(dimensions, 1) if dim in CONVERSION_BASIS_DIMENSIONS}
            for lvl, dim in per_level_dim.items():
                pg_agg = get_conversion_aggregates(
                    db, filters, dim,
                    campaign_scope=None if scope is None else filters.get("campaigns"))
                seen = set()
                for row in rows:
                    if row["level"] != lvl:
                        continue
                    agg = pg_agg.pop(row["value"], None)
                    seen.add(row["value"])
                    if agg is None:
                        agg = {"leads": 0, "conversions": 0, "rejected": 0, "revenue": 0.0, "profit": 0.0}
                    row.update(agg)
                    # recompute derived rates with the substituted numbers
                    visits = row.get("visits") or 0
                    clicks = row.get("clicks") or 0
                    cost = row.get("cost") or 0
                    row["cr"] = round(agg["conversions"] / clicks * 100, 2) if clicks else 0.0
                    row["epc"] = round(agg["revenue"] / clicks, 4) if clicks else 0.0
                    row["roi"] = round((agg["revenue"] - cost) / cost * 100, 2) if cost else 0.0
                    row["rejected_rate"] = round(agg["rejected"] / visits * 100, 2) if visits else 0.0
                    row["click_through_rate"] = round(clicks / visits * 100, 2) if visits else 0.0
                for value, agg in pg_agg.items():
                    if value in seen:
                        continue
                    row = {"level": lvl, "dim": dim, "value": value, "parent_key": "",
                           "visits": 0, "unique_visits": 0, "clicks": 0, "unique_clicks": 0}
                    row.update(agg)
                    row["cr"] = 0.0
                    row["epc"] = 0.0
                    row["roi"] = 0.0
                    row["rejected_rate"] = 0.0
                    row["click_through_rate"] = 0.0
                    rows.append(row)

        apply_custom_metrics(rows, [cm.dict() for cm in body.custom_metrics])

        totals = sum_rows([r for r in rows if r["level"] == 1])
        apply_custom_metrics([totals], [cm.dict() for cm in body.custom_metrics])

        return {
            "dimension": dimensions[0],           # legacy single-dim key
            "dimensions": dimensions,
            "date_basis": date_basis,
            "fallback_note": fallback_note,
            "rows": rows,
            "totals": totals,
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print("dashboard breakdown error:", repr(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/click-log")
async def get_click_log_view(request: Request, filters: ClickLogFilters,
                             db: Session = Depends(get_db)):
    try:
        ch = request.state.ch
        f = filters.dict()
        scope = _click_scope_campaign_ids(request, db)
        # Paginated mode is opted into by sending `offset` — the response then
        # carries the count() total so the UI can render a pager. Plain calls
        # (no offset) keep the legacy bare-list shape.
        if filters.offset is not None:
            limit = min(max(int(filters.limit or 50), 1), 5000)
            offset = max(int(filters.offset or 0), 0)
            if not _apply_click_scope(f, scope):
                return {"items": [], "total": 0, "limit": limit, "offset": offset}
            rows = get_click_log(ch, f, limit=limit, offset=offset)
            total = get_click_log_total(ch, f)
            return {"items": rows, "total": total, "limit": limit, "offset": offset}
        if not _apply_click_scope(f, scope):
            return []
        rows = get_click_log(ch, f, limit=min(max(int(filters.limit or 500), 1), 5000))
        return rows
    except Exception as e:
        print("dashboard click-log error:", repr(e))
        raise HTTPException(status_code=500, detail="Internal server error")


CLICK_LOG_EXPORT_FIELDS = ["received_at", "ip", "country", "device_type", "os", "browser",
                           "url", "referrer", "keyword", "traffic_source_name",
                           "campaign_id", "status", "is_bot", "visitor_id"]
CLICK_LOG_EXPORT_MAX = 50000


def _csv_safe(value):
    """Prefix cells that would start a spreadsheet formula (=,+,-,@) with an
    apostrophe so exported CSVs can't smuggle live formulas into Excel/Sheets."""
    s = "" if value is None else (value if isinstance(value, str) else str(value))
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


@router.post("/click-log/export")
async def export_click_log(request: Request, filters: ClickLogFilters,
                           db: Session = Depends(get_db)):
    """CSV export of the click log with the same drill-down filters (no cap on
    the usual 500-row view — up to CLICK_LOG_EXPORT_MAX rows)."""
    import csv as _csv
    import io as _io
    from fastapi.responses import Response
    try:
        ch = request.state.ch
        f = filters.dict()
        if not _apply_click_scope(f, _click_scope_campaign_ids(request, db)):
            rows = []
        else:
            rows = get_click_log(ch, f, limit=CLICK_LOG_EXPORT_MAX)
    except Exception as e:
        print("dashboard click-log export error:", repr(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    buf = _io.StringIO()
    writer = _csv.writer(buf, lineterminator="\n")
    writer.writerow(CLICK_LOG_EXPORT_FIELDS)
    for row in rows:
        writer.writerow([_csv_safe(row.get(k)) for k in CLICK_LOG_EXPORT_FIELDS])
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=click_log.csv"})


# ---------------------------------------------------------------------------
# G52: public shared reports — unauthenticated access to ONE saved report's
# breakdown data via its share token. Rate-limited with an in-memory counter.
# ---------------------------------------------------------------------------

import json
import time as time_mod

from models.settings import SettingsORM

# token -> list of recent epoch seconds; 60 requests/min per token
_public_rate: dict = {}
PUBLIC_RATE_MAX = 60
PUBLIC_RATE_WINDOW = 60


def _load_saved_reports(db: Session) -> list:
    row = db.query(SettingsORM).filter_by(name="saved_reports").first()
    if not row or not row.value:
        return []
    try:
        data = json.loads(row.value)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _public_rate_limited(token: str) -> bool:
    now = time_mod.time()
    hits = [t for t in _public_rate.get(token, []) if now - t < PUBLIC_RATE_WINDOW]
    _public_rate[token] = hits
    if len(hits) >= PUBLIC_RATE_MAX:
        return True
    hits.append(now)
    return False


@public_router.post("/public/report/{token}")
async def public_shared_report(token: str, request: Request, db: Session = Depends(get_db)):
    """Serve a shared saved report's breakdown data. No auth — the token IS
    the capability. Only the saved config's dimensions/filters are exposed."""
    if _public_rate_limited(token):
        raise HTTPException(status_code=429, detail="Too many requests")

    report = None
    for r in _load_saved_reports(db):
        if (r.get("share") or {}).get("token") == token:
            report = r
            break
    if not report:
        raise HTTPException(status_code=404, detail="Shared report not found or link revoked")

    cfg = report.get("config") or {}
    dimensions = [d for d in (cfg.get("dimensions") or []) if d in REPORT_DIMENSIONS][:5] or ["campaign_id"]
    date_range = cfg.get("date_range") or []
    filters = {
        "date_from": date_range[0] if len(date_range) > 0 else None,
        "date_to": date_range[1] if len(date_range) > 1 else None,
        "campaigns": cfg.get("campaigns") or [],
    }

    try:
        ch = request.state.ch
        rows = get_report_breakdown_multi(
            ch, filters, dimensions,
            sort_by=cfg.get("sortBy"), sort_dir=cfg.get("sortDir") or "desc",
        )
        apply_custom_metrics(rows, cfg.get("customMetrics") or [])
        totals = sum_rows([r for r in rows if r["level"] == 1])
        apply_custom_metrics([totals], cfg.get("customMetrics") or [])
        return {
            "name": report.get("name"),
            "dimensions": dimensions,
            "rows": rows,
            "totals": totals,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print("public shared report error:", repr(e))
        raise HTTPException(status_code=500, detail="Internal server error")
