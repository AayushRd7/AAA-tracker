"""G76 — AI auto-optimizer (Voluum/RedTrack-style flow reweighting).

For campaigns in 'weight' distribution mode the optimizer periodically
re-balances flow weights from real performance over a lookback window:

  clicks     — ClickHouse clicks_data per flow_index (bots excluded)
  conversions— PG conversions_data per flow_index (rejected/trash excluded)

Per-flow metric: cr = convs/clicks, epc = profit/clicks (earnings per click,
cost-based profitability — NOT revenue-based), profit/revenue = totals.

Reweighting rules:
  - only ENABLED 'regular' flows with clicks >= min_clicks participate
  - participating weights are set proportional to the metric, scaled so the
    best flow reaches up to (1 + max_shift_pct/100) x the average weight
  - each single shift is clamped to +-max_shift_pct of the old weight, so one
    pass can never flip a campaign's traffic upside down
  - flows with clicks < min_clicks keep their weight plus a +20%-of-average
    exploration boost (fresh flows must accumulate data before being judged)
  - flows with clicks < min_clicks + protect_clicks are never moved DOWN
    (young flows are protected from premature starvation)
  - zero/negative-metric flows get a floor of 10% of average, never 0
  - forced / default / disabled / monitor-disabled flows are NEVER touched

Results are persisted into campaigns.config["optimizer"]: the tuned settings
plus a capped last_runs history (10) with per-flow metrics and old -> new
weights. A Telegram alert fires when any flow hits the shift cap.
"""
import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db, SessionLocal
from tenant_context import current_tenant
from clickHouse import get_clickhouse_client

router = APIRouter()

LOOP_INTERVAL_SECONDS = 15 * 60
METRICS = ("epc", "cr", "profit", "revenue")
DEFAULTS = {"enabled": False, "metric": "epc", "lookback_hours": 24,
            "min_clicks": 50, "max_shift_pct": 80, "protect_clicks": 20}
MAX_RUN_HISTORY = 10

# last time the background loop completed a full sweep (for /status)
_loop_last_run = None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _sorted_flows(config: dict) -> list:
    """Flows in routing order — forced first, then position. flow_index values
    (CH clicks_data, conversions_data.flow_index, stickiness fi) all index
    into THIS ordering."""
    return sorted(
        config.get("flows") or [],
        key=lambda f: (0 if isinstance(f, dict) and f.get("type") == "forced" else 1,
                       f.get("position", 9999) if isinstance(f, dict) else 9999))


def _settings(block) -> dict:
    out = dict(DEFAULTS)
    if isinstance(block, dict):
        for k in DEFAULTS:
            if k in block and block[k] is not None:
                out[k] = block[k]
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _click_metrics(ch, campaign_id: int, hours: int) -> dict:
    rows = ch.query(f"""
        SELECT flow_index, count() AS clicks, sumOrNull(toFloat64(cost)) AS cost
        FROM clicks_data
        WHERE campaign_id = %(cid)s
          AND tenant_id = %(tenant_id)s
          AND received_at >= now() - toIntervalHour(%(hours)s)
          AND flow_index > 0
          AND NOT (is_bot = true)
        GROUP BY flow_index""",
        parameters={"cid": int(campaign_id), "hours": int(hours),
                    "tenant_id": current_tenant()}).result_rows
    return {int(r[0]): {"clicks": int(r[1] or 0), "cost": float(r[2] or 0)}
            for r in rows}


def _conversion_metrics(db: Session, campaign_id: int, hours: int) -> dict:
    rows = db.execute(text("""
        SELECT flow_index, count(*) AS convs,
               COALESCE(sum(profit), 0) AS profit, COALESCE(sum(revenue), 0) AS revenue
        FROM conversions_data
        WHERE tenant_id = :tid
          AND campaign_id = :cid
          AND received_at >= now() - make_interval(hours => :h)
          AND flow_index IS NOT NULL
          AND status NOT IN ('rejected', 'trash')
        GROUP BY flow_index"""),
        {"cid": int(campaign_id), "h": int(hours),
         "tid": current_tenant()}).fetchall()
    return {int(r[0]): {"convs": int(r[1] or 0), "profit": float(r[2] or 0),
                        "revenue": float(r[3] or 0)} for r in rows}


def compute_flow_metrics(ch, db: Session, campaign_id: int, settings: dict) -> dict:
    """flow_index -> {clicks, cost, convs, profit, revenue, value} where value
    is the configured metric (cr = convs/clicks, epc = profit/clicks)."""
    clicks = _click_metrics(ch, campaign_id, int(settings["lookback_hours"]))
    convs = _conversion_metrics(db, campaign_id, int(settings["lookback_hours"]))
    metric = settings["metric"]
    out = {}
    for idx in set(clicks) | set(convs):
        c = clicks.get(idx, {"clicks": 0, "cost": 0.0})
        v = convs.get(idx, {"convs": 0, "profit": 0.0, "revenue": 0.0})
        clicks_n, convs_n = c["clicks"], v["convs"]
        if metric == "cr":
            value = convs_n / clicks_n if clicks_n else 0.0
        elif metric == "epc":
            value = v["profit"] / clicks_n if clicks_n else 0.0
        elif metric == "profit":
            value = v["profit"]
        else:  # revenue
            value = v["revenue"]
        out[idx] = {**c, **v, "value": value}
    return out


# ---------------------------------------------------------------------------
# Optimization pass
# ---------------------------------------------------------------------------

def _flow_name(flow: dict, idx: int) -> str:
    return (flow.get("name") or "").strip() or f"flow #{idx + 1}"


def _is_regular(flow: dict) -> bool:
    return isinstance(flow, dict) and flow.get("type", "regular") not in ("forced", "default")


def run_optimization(campaign_id: int, caller: str = "", ip: str = "") -> dict:
    """One optimization pass for a campaign. All guards included — safe to call
    from the background loop and from POST /{cid}/run."""
    db = SessionLocal()
    try:
        row = db.execute(text(
            "SELECT id, name, status, redirect_mode, config FROM campaigns "
            "WHERE id = :i AND tenant_id = :tid FOR UPDATE"),
            {"i": int(campaign_id), "tid": current_tenant()}).fetchone()
        if not row:
            return {"ok": False, "reason": "campaign_not_found"}
        _, name, status, dmode, config = row
        config = config or {}
        settings = _settings(config.get("optimizer"))

        if not settings["enabled"]:
            return {"ok": False, "reason": "disabled", "campaign_id": int(campaign_id)}
        if status != "active":
            return {"ok": False, "reason": "campaign_not_active", "campaign_id": int(campaign_id)}
        if (dmode or "position") != "weight":
            return {"ok": False, "reason": "position_mode_not_supported",
                    "campaign_id": int(campaign_id)}

        sorted_flows = _sorted_flows(config)
        ch = get_clickhouse_client()
        try:
            metrics = compute_flow_metrics(ch, db, campaign_id, settings)
        finally:
            ch.close()

        min_clicks = int(settings["min_clicks"])
        max_shift = int(settings["max_shift_pct"])
        protect = int(settings["protect_clicks"])

        participants = []
        for idx, flow in enumerate(sorted_flows):
            if not _is_regular(flow):
                continue
            if not flow.get("enabled", True) or flow.get("disabled_by_monitor"):
                continue
            try:
                old_w = int(round(float(flow.get("weight", 100) or 0)))
            except (TypeError, ValueError):
                old_w = 100
            m = metrics.get(idx) or {"clicks": 0, "cost": 0.0, "convs": 0,
                                     "profit": 0.0, "revenue": 0.0, "value": 0.0}
            participants.append({"index": idx, "name": _flow_name(flow, idx),
                                 "old": max(old_w, 1), **m})

        if len(participants) < 2:
            return {"ok": False, "reason": "needs_at_least_2_eligible_flows",
                    "campaign_id": int(campaign_id)}
        total_clicks = sum(p["clicks"] for p in participants)
        if total_clicks < min_clicks * 2:
            return {"ok": False, "reason": "insufficient_clicks",
                    "total_clicks": total_clicks, "campaign_id": int(campaign_id)}

        avg_w = sum(p["old"] for p in participants) / len(participants)
        eligible = [p for p in participants if p["clicks"] >= min_clicks]
        floor_w = max(1, round(avg_w * 0.1))       # zero/negative-metric floor
        boost_w = max(1, round(avg_w * 0.2))       # exploration boost step
        target_best = avg_w * (1 + max_shift / 100)

        best = max((p["value"] for p in eligible), default=0.0)
        for p in participants:
            if p["clicks"] < min_clicks:
                # Exploration: keep current weight, add a floor boost so the
                # flow keeps collecting data instead of starving.
                p["new"] = p["old"] + boost_w
                continue
            if best <= 0:
                raw = floor_w
            else:
                raw = max(p["value"], 0.0) / best * target_best
            # Per-pass shift clamp: +-max_shift_pct of the old weight.
            lo = p["old"] * (1 - max_shift / 100)
            hi = p["old"] * (1 + max_shift / 100)
            new = min(max(raw, lo), hi)
            # protect_clicks: thin-but-eligible flows are never moved down.
            if p["clicks"] < min_clicks + protect:
                new = max(new, p["old"])
            p["new"] = max(1, int(round(new)))

        changed = {p["name"]: {"old": p["old"], "new": p["new"]}
                   for p in participants if p["new"] != p["old"]}
        per_flow = [{
            "index": p["index"], "name": p["name"], "clicks": p["clicks"],
            "cost": round(p["cost"], 4), "convs": p["convs"],
            "profit": round(p["profit"], 4), "revenue": round(p["revenue"], 4),
            "metric": settings["metric"], "value": round(p["value"], 6),
            "old_weight": p["old"], "new_weight": p["new"],
        } for p in participants]

        if not changed:
            return {"ok": True, "changed": False, "reason": "no_change",
                    "campaign_id": int(campaign_id), "metric": settings["metric"],
                    "lookback_hours": settings["lookback_hours"], "flows": per_flow}

        # sorted_flows holds references into config["flows"] — mutating weight
        # here updates the config document we persist below.
        for p in participants:
            sorted_flows[p["index"]]["weight"] = p["new"]

        block = dict(settings)
        runs = list((config.get("optimizer") or {}).get("last_runs") or [])
        runs.append({"at": datetime.utcnow().isoformat(), "reason": "optimized",
                     "metric": settings["metric"], "shifts": changed, "flows": per_flow})
        block["last_runs"] = runs[-MAX_RUN_HISTORY:]
        config["optimizer"] = block
        db.execute(text(
            "UPDATE campaigns SET config = CAST(:c AS JSONB), updated_at = now() "
            "WHERE id = :i AND tenant_id = :tid"),
            {"c": json.dumps(config), "i": int(campaign_id), "tid": current_tenant()})
        db.commit()

        from audit_logger import audit_event
        audit_event(caller or "optimizer_loop", "optimizer_run", "campaigns",
                    str(campaign_id),
                    {"campaign": name, "metric": settings["metric"],
                     "changed": changed},
                    ip)

        hit_cap = any(abs(c["new"] - c["old"]) / max(c["old"], 1) * 100 >= max_shift
                      for c in changed.values())
        return {"ok": True, "changed": True, "reason": "optimized",
                "campaign_id": int(campaign_id), "campaign_name": name,
                "metric": settings["metric"],
                "lookback_hours": settings["lookback_hours"],
                "hit_shift_cap": hit_cap, "shifts": changed, "flows": per_flow}
    finally:
        db.close()


async def _alert_on_big_shift(result: dict) -> None:
    """Telegram ping when a pass pushed some flow to its shift cap (the
    optimizer wanted to move harder than max_shift_pct allows). Optional —
    silently skipped when Telegram is not configured."""
    if not result.get("hit_shift_cap"):
        return
    try:
        from app_pages.monitor import send_telegram_alert
        import html as _html
        lines = "\n".join(
            f"• {_html.escape(str(n))}: {c['old']} → {c['new']}"
            for n, c in (result.get("shifts") or {}).items())
        msg = (f"⚡ <b>Auto-optimizer reweighted campaign "
               f"#{result.get('campaign_id')} ({_html.escape(str(result.get('campaign_name') or ''))})</b>\n\n"
               f"Metric: {_html.escape(str(result.get('metric')))} "
               f"({result.get('lookback_hours')}h lookback)\n{lines}")
        await asyncio.to_thread(send_telegram_alert, msg)
    except Exception as e:
        print("optimizer telegram alert:", e)


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def optimizer_loop():
    """Sweep every optimizer-enabled campaign every 15 minutes."""
    global _loop_last_run
    await asyncio.sleep(150)  # stagger vs monitor (60s) and rules (90s)
    while True:
        try:
            db = SessionLocal()
            try:
                rows = db.execute(text(
                    "SELECT id, config FROM campaigns "
                    "WHERE status = 'active' AND archived = false "
                    "AND tenant_id = :tid"), {"tid": current_tenant()}).fetchall()
            finally:
                db.close()
            for cid, config in rows:
                try:
                    block = _settings((config or {}).get("optimizer"))
                    if not block["enabled"]:
                        continue
                    result = await asyncio.to_thread(run_optimization, cid)
                    if result.get("reason") == "position_mode_not_supported":
                        print(f"Optimizer: campaign {cid} uses position mode — skipped")
                    elif result.get("changed"):
                        print(f"Optimizer: campaign {cid} reweighted:", result.get("shifts"))
                        await _alert_on_big_shift(result)
                except Exception as e:
                    print(f"Optimizer: campaign {cid} error:", e)
            _loop_last_run = datetime.utcnow()
        except Exception as e:
            print("Optimizer loop error:", e)
        await asyncio.sleep(LOOP_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _campaign_row(db: Session, campaign_id: int):
    row = db.execute(text(
        "SELECT id, name, status, redirect_mode, config FROM campaigns "
        "WHERE id = :i AND tenant_id = :tid"),
        {"i": int(campaign_id), "tid": current_tenant()}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return row


def _campaign_status_payload(ch, db: Session, row) -> dict:
    cid, name, status, dmode, config = row
    config = config or {}
    settings = _settings(config.get("optimizer"))
    supported = (dmode or "position") == "weight"
    flows = []
    if supported:
        metrics = compute_flow_metrics(ch, db, cid, settings)
        for idx, flow in enumerate(_sorted_flows(config)):
            m = metrics.get(idx) or {"clicks": 0, "cost": 0.0, "convs": 0,
                                     "profit": 0.0, "revenue": 0.0, "value": 0.0}
            flows.append({
                "index": idx, "name": _flow_name(flow, idx),
                "type": flow.get("type", "regular"),
                "enabled": bool(flow.get("enabled", True)),
                "disabled_by_monitor": bool(flow.get("disabled_by_monitor")),
                "weight": flow.get("weight", 100),
                "clicks": m["clicks"], "cost": round(m["cost"], 4),
                "convs": m["convs"], "profit": round(m["profit"], 4),
                "revenue": round(m["revenue"], 4), "value": round(m["value"], 6),
                "touched_by_optimizer": _is_regular(flow)
                    and bool(flow.get("enabled", True)) and not flow.get("disabled_by_monitor"),
            })
    return {"id": cid, "name": name, "status": status, "redirect_mode": dmode,
            "supported": supported, "optimizer": settings,
            "last_runs": (config.get("optimizer") or {}).get("last_runs") or [],
            "flows": flows}


@router.get("/status")
def optimizer_status(request: Request, db: Session = Depends(get_db)):
    ch = request.state.ch
    rows = db.execute(text(
        "SELECT id, name, status, redirect_mode, config FROM campaigns "
        "WHERE archived = false AND tenant_id = :tid ORDER BY id ASC"),
        {"tid": current_tenant()}).fetchall()
    return {"campaigns": [_campaign_status_payload(ch, db, r) for r in rows],
            "metrics": list(METRICS), "defaults": DEFAULTS,
            "loop_last_run": _loop_last_run.isoformat() if _loop_last_run else None,
            "interval_minutes": LOOP_INTERVAL_SECONDS // 60}


@router.get("/{campaign_id}")
def get_optimizer(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    return _campaign_status_payload(request.state.ch, db,
                                    _campaign_row(db, campaign_id))


@router.put("/{campaign_id}")
def put_optimizer(campaign_id: int, payload: dict, request: Request,
                  db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    _campaign_row(db, campaign_id)

    updates = {}
    data = payload or {}
    if "enabled" in data:
        updates["enabled"] = bool(data["enabled"])
    if "metric" in data:
        if data["metric"] not in METRICS:
            raise HTTPException(status_code=422,
                                detail=f"metric must be one of: {', '.join(METRICS)}")
        updates["metric"] = data["metric"]
    for key in ("lookback_hours", "min_clicks", "protect_clicks"):
        if key in data:
            try:
                updates[key] = int(data[key])
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail=f"{key} must be an integer")
            # protect_clicks=0 is meaningful: no young-flow downward protection
            floor = 0 if key == "protect_clicks" else 1
            if updates[key] < floor or (key == "lookback_hours" and updates[key] > 720):
                raise HTTPException(status_code=422,
                                    detail=f"{key} must be an integer >= {floor}"
                                           + (" (max 720)" if key == "lookback_hours" else ""))
    if "max_shift_pct" in data:
        try:
            updates["max_shift_pct"] = int(data["max_shift_pct"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="max_shift_pct must be an integer")
        if not 10 <= updates["max_shift_pct"] <= 95:
            raise HTTPException(status_code=422, detail="max_shift_pct must be 10-95")

    row = db.execute(text(
        "SELECT config FROM campaigns WHERE id = :i AND tenant_id = :tid FOR UPDATE"),
        {"i": int(campaign_id), "tid": current_tenant()}).fetchone()
    config = row[0] or {}
    # Start from the live optimizer block (defaults for the known settings,
    # keeping run history and any extra keys like last_runs) and overlay the
    # validated updates — a plain rebuild used to wipe last_runs.
    existing = config.get("optimizer")
    block = _settings(existing)
    if isinstance(existing, dict):
        block.update({k: v for k, v in existing.items() if k not in DEFAULTS})
    block.update(updates)
    db.execute(text(
        "UPDATE campaigns SET config = jsonb_set(COALESCE(config, '{}'::jsonb), '{optimizer}', CAST(:o AS JSONB)), "
        "updated_at = now() WHERE id = :i AND tenant_id = :tid"),
        {"o": json.dumps(block), "i": int(campaign_id), "tid": current_tenant()})
    db.commit()

    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "optimizer_update", "campaigns",
                str(campaign_id), updates, request.client.host if request.client else "")
    return {"optimizer": block}


@router.post("/{campaign_id}/run")
async def run_now(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    """Run one optimization pass synchronously (same guards as the loop)."""
    _campaign_row(db, campaign_id)
    from auth import get_caller
    caller, _ = get_caller(request)
    result = await asyncio.to_thread(
        run_optimization, int(campaign_id), caller or "api_token",
        request.client.host if request.client else "")
    if result.get("changed"):
        await _alert_on_big_shift(result)
    return result
