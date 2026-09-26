from datetime import date as date_cls, timedelta
from datetime import datetime as datetime_cls

from fastapi import APIRouter, Request, HTTPException, Depends
from clickHouse import (
    get_recent_visits, get_metrics_series, get_report_breakdown, get_report_breakdown_multi,
    get_click_log, get_live_clicks, apply_custom_metrics, sum_rows, REPORT_DIMENSIONS,
)
from schemas import Filters
from sqlalchemy.orm import Session
from sqlalchemy import func, case
from db import get_db

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
    limit: int = 500


# Dimensions conversions_data can be grouped by for the conversion_date basis (G55).
# Anything else falls back to click_date with an explanatory note.
CONVERSION_BASIS_DIMENSIONS = {
    "campaign_id", "offer_id", "sub_id_1", "sub_id_2", "sub_id_3", "sub_id_4",
    "sub_id_5", "sub_id_6", "sub_id_7", "sub_id_8", "sub_id_9", "sub_id_10",
    "utm_source", "utm_campaign", "utm_creative", "traffic_source_name", "status",
}


def get_conversion_aggregates(db: Session, filters: dict, dimension: str) -> dict:
    """Group Postgres conversions_data by `dimension` for the date window.

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
        filters: Filters
):
    try:
        ch = request.state.ch
        rows = get_recent_visits(ch, filters)
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/live-clicks")
async def live_clicks(request: Request, after: Optional[str] = None, limit: int = 20):
    """G51: raw live click feed. Latest rows first; ``after`` (ISO timestamp)
    polls for rows newer than the last seen one."""
    if after:
        try:
            datetime_cls.fromisoformat(after.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid 'after' timestamp — expected ISO format")
    try:
        ch = request.state.ch
        return get_live_clicks(ch, after=after, limit=min(max(limit, 1), 100))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


@router.post("/metrics")
async def get_metrics(request: Request):
    ch = request.state.ch
    body = await request.json()
    filter_fields = set(Filters.__fields__)
    filters = Filters(**{k: v for k, v in body.items() if k in filter_fields})
    compare = bool(body.get("compare"))

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

        filters = body.filters.dict()
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
                pg_agg = get_conversion_aggregates(db, filters, dim)
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
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/click-log")
async def get_click_log_view(request: Request, filters: ClickLogFilters):
    try:
        ch = request.state.ch
        rows = get_click_log(ch, filters.dict(), limit=filters.limit)
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))
