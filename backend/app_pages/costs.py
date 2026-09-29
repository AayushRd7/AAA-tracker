"""Retroactive cost update (admin tool).

Sets the per-click cost on existing ClickHouse rows inside a date window,
optionally scoped to one campaign. Uses the same synchronous-mutation
mechanism as the conversion sync in the tracking plane
(``sync_conversion_to_clickhouse``: ``ALTER TABLE ... UPDATE ...`` with
``SETTINGS mutations_sync = 1``) so the change is fully applied before the
endpoint responds and tests are deterministic.

Cost semantics: **per-click** — every matching row gets exactly ``cost``.
Total spend = cost x updated_rows. Rows whose click flag is set get the same
value as visit rows; existing costs are overwritten, not adjusted. Pass
``cost: 0`` to clear costs retroactively.
"""
import math
from datetime import date
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from tenant_context import current_tenant
from models.campaigns import CampaignORM

router = APIRouter()

# Guard rails so a typo can't rewrite years of data or absurd amounts.
MAX_PERIOD_DAYS = 366
MAX_COST = 1_000_000


class CostUpdateIn(BaseModel):
    campaign_id: Optional[int] = None   # omit = all campaigns
    period: Optional[dict] = None       # {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}
    cost: Optional[float] = None        # per-click cost (see module docstring)


def _log_cost_update(db: Session, username: str, campaign_id: Optional[int],
                     date_from: date, date_to: date, cost: float,
                     updated_rows: int) -> None:
    """Append to cost_update_logs (best-effort — a logging failure must never
    fail the cost mutation itself)."""
    try:
        db.execute(text("""
            INSERT INTO cost_update_logs
                (username, campaign_id, date_from, date_to, cost, updated_rows, tenant_id)
            VALUES (:username, :campaign_id, :date_from, :date_to, :cost, :updated_rows, :tid)
        """), {"username": username, "campaign_id": campaign_id,
               "date_from": date_from, "date_to": date_to,
               "cost": cost, "updated_rows": updated_rows,
               "tid": current_tenant()})
        db.commit()
    except Exception as e:
        db.rollback()
        print("cost_update_logs write error:", repr(e))


@router.post("/update")
def update_costs(data: CostUpdateIn, request: Request, db: Session = Depends(get_db)):
    """Apply a retroactive per-click cost to matching clicks_data rows.

    Admin-only (router is mounted behind the settings section gate).
    Returns the number of rows the mutation touched.
    """
    period = data.period if isinstance(data.period, dict) else None
    if not period or not period.get("from") or not period.get("to"):
        raise HTTPException(status_code=400,
                            detail="period with 'from' and 'to' (YYYY-MM-DD) is required")
    try:
        d_from = date.fromisoformat(str(period["from"]).strip())
        d_to = date.fromisoformat(str(period["to"]).strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="period dates must be YYYY-MM-DD")
    if d_from > d_to:
        raise HTTPException(status_code=400, detail="period 'from' must not be after 'to'")
    if (d_to - d_from).days + 1 > MAX_PERIOD_DAYS:
        raise HTTPException(status_code=400,
                            detail=f"period spans more than {MAX_PERIOD_DAYS} days")

    cost = data.cost
    if cost is None or isinstance(cost, bool) or math.isnan(cost) or math.isinf(cost) \
            or cost < 0 or cost > MAX_COST:
        raise HTTPException(status_code=400,
                            detail=f"cost must be a number between 0 and {MAX_COST}")

    if data.campaign_id is not None and int(data.campaign_id) <= 0:
        raise HTTPException(status_code=400, detail="campaign_id must be a positive integer")
    if data.campaign_id is not None \
            and not db.query(CampaignORM).filter_by(id=int(data.campaign_id)).first():
        # previously a typo'd id "succeeded" with updated_rows 0
        raise HTTPException(status_code=404, detail="Campaign not found")

    where = ("tenant_id = %(tenant_id)s AND "
             "toDate(received_at) BETWEEN toDate(%(df)s) AND toDate(%(dt)s)")
    params = {"df": d_from.isoformat(), "dt": d_to.isoformat(),
              "tenant_id": current_tenant()}
    if data.campaign_id is not None:
        where += " AND campaign_id = %(cid)s"
        params["cid"] = int(data.campaign_id)

    ch = request.state.ch
    matched = int(ch.query(f"SELECT count() FROM clicks_data WHERE {where}",
                           parameters=params).result_rows[0][0])
    ch.command(
        f"ALTER TABLE clicks_data UPDATE cost = %(cost)s WHERE {where}",
        parameters={**params, "cost": float(cost)},
        settings={"mutations_sync": 1})

    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "cost_update", "campaigns",
                str(data.campaign_id) if data.campaign_id is not None else "all",
                {"from": d_from.isoformat(), "to": d_to.isoformat(),
                 "cost": float(cost), "updated_rows": matched},
                request.client.host if request.client else "")

    # Logs area: the retroactive cost update is itself an audit event — record
    # who changed which campaign's costs over which window and how many rows.
    _log_cost_update(db, caller or "api_token",
                     int(data.campaign_id) if data.campaign_id is not None else None,
                     d_from, d_to, float(cost), matched)

    return {
        "updated_rows": matched,
        "campaign_id": int(data.campaign_id) if data.campaign_id is not None else None,
        "from": d_from.isoformat(),
        "to": d_to.isoformat(),
        "cost": float(cost),
    }
