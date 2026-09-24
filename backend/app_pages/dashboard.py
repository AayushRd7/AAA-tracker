from fastapi import APIRouter, Request, HTTPException, Query
from clickHouse import get_recent_visits, get_metrics_series, get_report_breakdown, get_click_log, REPORT_DIMENSIONS
from schemas import Filters

from pydantic import BaseModel
from typing import Optional, List

router = APIRouter()


class ReportFilters(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    campaigns: List[int] = []


class ReportRequest(BaseModel):
    dimension: str
    filters: ReportFilters = ReportFilters()


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


@router.post("/visits")
async def get_visits(
        request: Request,
        filters: Filters
):
    try:
        ch = request.app.state.ch
        rows = get_recent_visits(ch, filters)
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/metrics")
async def get_metrics(request: Request, filters: Filters):
    ch = request.app.state.ch
    series = get_metrics_series(ch, filters)

    total_visits = sum(row['visits'] for row in series)
    total_unique_visits = sum(row['unique_visits'] for row in series)
    total_clicks = sum(row['clicks'] for row in series)
    total_unique_clicks = sum(row['unique_clicks'] for row in series)
    total_conversions = sum(row['conversions'] for row in series)
    total_cost = sum(row['cost'] or 0 for row in series)
    total_revenue = sum(row['revenue'] or 0 for row in series)
    roi = f"{round(((total_revenue - total_cost) / total_cost * 100), 2)}%" if total_cost else "—"

    return {
        "metrics": {
            "visits": total_visits,
            "unique_visits": total_unique_visits,
            "clicks": total_clicks,
            "unique_clicks": total_unique_clicks,
            "conversions": total_conversions,
            "cost": round(total_cost, 2),
            "revenue": round(total_revenue, 2),
            "roi": roi
        },
        "chart": {
            "labels": [str(row["day"]) for row in series],
            "visits": [row["visits"] for row in series],
            "unique_visits": [row["unique_visits"] for row in series],
            "clicks": [row["clicks"] for row in series],
            "conversions": [row["conversions"] for row in series],
            "unique_clicks": [row["unique_clicks"] for row in series]
        }
    }


@router.get("/dimensions")
async def get_dimensions():
    """Available breakdown dimensions for the report builder."""
    return [{"key": k, "label": k.replace("_", " ")} for k in REPORT_DIMENSIONS]


@router.post("/breakdown")
async def get_breakdown(request: Request, body: ReportRequest):
    try:
        ch = request.app.state.ch
        rows = get_report_breakdown(ch, body.filters.dict(), body.dimension)
        return {"dimension": body.dimension, "rows": rows}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/click-log")
async def get_click_log_view(request: Request, filters: ClickLogFilters):
    try:
        ch = request.app.state.ch
        rows = get_click_log(ch, filters.dict(), limit=filters.limit)
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
