"""G70 — auto rules.

Administrators define rules like "ROI < -30% over the last 6 hours → pause the
campaign" or "conversions > 100 over 24h → alert". A background loop (every 15
minutes) evaluates every enabled rule against ClickHouse metrics for its own
lookback window and performs the action when ALL conditions match:
  - pause_campaign: flips campaigns.status to 'paused'
  - alert_telegram:   message via the saved Telegram bot
  - alert_email:      message via the saved email settings (Brevo/SMTP)

IMPORTANT (documented limit): the tracking plane currently routes traffic for
paused campaigns too — pausing is admin-plane state + an alert, it does NOT
stop clicks. Use flow auto-disable (Monitoring) for traffic-level cutoffs.
Alert actions fire once per continuous match (they re-arm after the condition
clears).
"""
import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db, SessionLocal
from clickHouse import get_clickhouse_client

router = APIRouter()

LOOP_INTERVAL_SECONDS = 15 * 60
METRICS = ("roi", "profit", "cost", "conversions", "clicks", "revenue")
COMPARATORS = ("<", ">", "<=", ">=", "==", "!=")
ACTIONS = ("pause_campaign", "alert_telegram", "alert_email")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class RuleIn(BaseModel):
    name: str
    enabled: bool = True
    scope: str = "campaign"
    campaign_id: int = None
    conditions: list = []
    action: str = "alert_telegram"


def _validate(payload: dict, partial: bool = False) -> dict:
    name = str(payload.get("name") or "").strip()
    if not partial or name:
        if not name:
            raise HTTPException(status_code=400, detail="Rule name is required")
    conditions = payload.get("conditions")
    if conditions is not None or not partial:
        if not isinstance(conditions, list) or not conditions:
            raise HTTPException(status_code=400, detail="At least one condition is required")
        for c in conditions:
            if not isinstance(c, dict):
                raise HTTPException(status_code=400, detail="Condition must be an object")
            if c.get("metric") not in METRICS:
                raise HTTPException(status_code=400,
                                    detail=f"metric must be one of: {', '.join(METRICS)}")
            if c.get("comparator") not in COMPARATORS:
                raise HTTPException(status_code=400,
                                    detail=f"comparator must be one of: {', '.join(COMPARATORS)}")
            try:
                float(c.get("value"))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="condition value must be a number")
            try:
                hours = int(c.get("period_hours"))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="period_hours must be an integer")
            if not 1 <= hours <= 24 * 30:
                raise HTTPException(status_code=400, detail="period_hours must be 1-720")
    action = payload.get("action")
    if action is not None or not partial:
        if action not in ACTIONS:
            raise HTTPException(status_code=400, detail=f"action must be one of: {', '.join(ACTIONS)}")
    return payload


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_rule(ch, rule: dict) -> dict:
    """Compute every condition's actual value over its own lookback window.
    A rule matches when ALL conditions match (missing metric = no match)."""
    cid = rule.get("campaign_id")
    out_conditions = []
    for c in (rule.get("conditions") or []):
        hours = int(c.get("period_hours"))
        where = "received_at >= now() - toIntervalHour(%(hours)s)"
        params = {"hours": hours}
        if cid:
            where += " AND campaign_id = %(cid)s"
            params["cid"] = int(cid)
        row = ch.query(f"""
            SELECT countIf(click = true) AS clicks,
                   countIf(status IN ('sale', 'upsale')) AS conversions,
                   sumOrNull(toFloat64(cost)) AS cost,
                   sumOrNull(toFloat64(revenue)) AS revenue
            FROM clicks_data WHERE {where}""", parameters=params).result_rows
        clicks, conversions, cost, revenue = (row[0] if row else (0, 0, 0, 0))
        cost, revenue = float(cost or 0), float(revenue or 0)
        profit = revenue - cost
        metric = c["metric"]
        if metric == "roi":
            actual = round((revenue - cost) / cost * 100, 2) if cost else None
        elif metric == "profit":
            actual = round(profit, 4)
        elif metric == "cost":
            actual = round(cost, 4)
        elif metric == "revenue":
            actual = round(revenue, 4)
        elif metric == "conversions":
            actual = int(conversions)
        else:  # clicks
            actual = int(clicks)
        value = float(c.get("value"))
        matched = False
        if actual is not None:
            if c["comparator"] == "<":
                matched = actual < value
            elif c["comparator"] == ">":
                matched = actual > value
            elif c["comparator"] == "<=":
                matched = actual <= value
            elif c["comparator"] == ">=":
                matched = actual >= value
            elif c["comparator"] == "==":
                matched = actual == value
            else:
                matched = actual != value
        out_conditions.append({"metric": metric, "period_hours": hours,
                               "comparator": c["comparator"], "value": value,
                               "actual": actual, "matched": matched})
    return {"conditions": out_conditions,
            "matched": bool(out_conditions) and all(x["matched"] for x in out_conditions)}


def _rule_public(r) -> dict:
    return {"id": r[0], "name": r[1], "enabled": r[2], "scope": r[3],
            "campaign_id": r[4],
            "conditions": r[5] if isinstance(r[5], list) else json.loads(r[5] or "[]"),
            "action": r[6],
            "last_run": r[7].isoformat() if r[7] else None,
            "last_result": r[8]}


def _get_rule(db: Session, rule_id: int):
    r = db.execute(text("SELECT * FROM auto_rules WHERE id = :i"), {"i": rule_id}).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Rule not found")
    return r


def perform_action(rule: dict, evaluation: dict) -> dict:
    """Execute the rule's action. Returns a small result description."""
    action = rule.get("action")
    cid = rule.get("campaign_id")
    if action == "pause_campaign":
        if not cid:
            return {"ok": False, "detail": "pause_campaign requires a campaign_id"}
        db = SessionLocal()
        try:
            res = db.execute(text("UPDATE campaigns SET status = 'paused', updated_at = now() "
                                  "WHERE id = :i AND status != 'paused'"), {"i": int(cid)})
            db.commit()
            return {"ok": True, "detail": "campaign paused" if res.rowcount
                    else "campaign already paused"}
        finally:
            db.close()
    cond_desc = ", ".join(f"{c['metric']} {c['comparator']} {c['value']} "
                          f"(actual {c['actual']})" for c in evaluation["conditions"])
    if action == "alert_telegram":
        from app_pages.monitor import send_telegram_alert
        import html as _html
        ok = send_telegram_alert(
            f"⚙️ <b>Auto rule fired: {_html.escape(str(rule.get('name')))}</b>\n\n{_html.escape(cond_desc)}")
        return {"ok": ok, "detail": "telegram sent" if ok else "telegram not configured/failed"}
    if action == "alert_email":
        try:
            from email_reports import send_email
            from app_pages.monitor import _load_main_settings
            cfg = (_load_main_settings().get("email_reports") or {})
            recipients = [r.strip() for r in (cfg.get("recipients") or "").split(",") if r.strip()]
            if not recipients:
                return {"ok": False, "detail": "no email recipients configured"}
            send_email(cfg, f"Auto rule fired: {rule.get('name')}",
                       f"<p>Rule <b>{rule.get('name')}</b> matched:</p>"
                       f"<p>{cond_desc}</p>", recipients)
            return {"ok": True, "detail": "email sent"}
        except Exception as e:
            return {"ok": False, "detail": f"email failed: {str(e)[:120]}"}
    return {"ok": False, "detail": f"unknown action {action}"}


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def auto_rules_loop():
    """Evaluate every enabled rule every 15 minutes."""
    await asyncio.sleep(90)  # stagger vs the monitor loop
    while True:
        try:
            db = SessionLocal()
            try:
                rules = db.execute(text("SELECT * FROM auto_rules WHERE enabled = true")).fetchall()
            finally:
                db.close()
            if rules:
                ch = get_clickhouse_client()
                try:
                    for r in rules:
                        try:
                            await run_rule(dict(_rule_public(r)), ch, persist=True)
                        except Exception as e:
                            print(f"Auto rule {r[0]} error:", e)
                finally:
                    ch.close()
        except Exception as e:
            print("Auto rules loop error:", e)
        await asyncio.sleep(LOOP_INTERVAL_SECONDS)


async def run_rule(rule: dict, ch, persist: bool = False, execute: bool = True) -> dict:
    """Evaluate one rule; optionally execute + persist the outcome."""
    evaluation = evaluate_rule(ch, rule)
    result = {"matched": evaluation["matched"], "conditions": evaluation["conditions"],
              "action_taken": None}
    if evaluation["matched"] and execute:
        # alert actions re-arm once the condition clears — don't nag every cycle
        prev = rule.get("last_result") or {}
        if rule.get("action") == "pause_campaign" or not prev.get("matched"):
            action_result = await asyncio.to_thread(perform_action, rule, evaluation)
            result["action_taken"] = action_result
    if persist:
        db = SessionLocal()
        try:
            db.execute(text("UPDATE auto_rules SET last_run = now(), "
                            "last_result = CAST(:r AS JSONB) WHERE id = :i"),
                       {"r": json.dumps(result), "i": int(rule["id"])})
            db.commit()
        finally:
            db.close()
    return result


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/")
def list_rules(db: Session = Depends(get_db)):
    rows = db.execute(text("SELECT * FROM auto_rules ORDER BY id ASC")).fetchall()
    return {"rules": [_rule_public(r) for r in rows]}


@router.post("/")
def create_rule(payload: RuleIn, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    data = _validate(payload.dict())
    if data.get("action") == "pause_campaign" and not data.get("campaign_id"):
        raise HTTPException(status_code=400, detail="pause_campaign requires a campaign_id")
    r = db.execute(text("""
        INSERT INTO auto_rules (name, enabled, scope, campaign_id, conditions, action)
        VALUES (:n, :e, 'campaign', :cid, CAST(:c AS JSONB), :a) RETURNING id"""),
        {"n": data["name"].strip(), "e": bool(data.get("enabled", True)),
         "cid": data.get("campaign_id"), "c": json.dumps(data["conditions"]),
         "a": data["action"]}).fetchone()
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "create", "auto_rules", str(r[0]),
                {"name": data["name"], "action": data["action"]},
                request.client.host if request.client else "")
    return {"rule": _rule_public(_get_rule(db, r[0]))}


@router.patch("/{rule_id}")
def update_rule(rule_id: int, payload: dict, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    r = _get_rule(db, rule_id)
    merged = _rule_public(r)
    merged.update({k: v for k, v in payload.items() if k in
                   ("name", "enabled", "campaign_id", "conditions", "action")})
    # coerce campaign_id here (a raw string "3" must not blow up int() later)
    if merged.get("campaign_id") is not None:
        try:
            merged["campaign_id"] = int(merged["campaign_id"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="campaign_id must be an integer")
    _validate(merged)
    if merged.get("action") == "pause_campaign" and not merged.get("campaign_id"):
        raise HTTPException(status_code=400, detail="pause_campaign requires a campaign_id")
    db.execute(text("""
        UPDATE auto_rules SET name = :n, enabled = :e, campaign_id = :cid,
            conditions = CAST(:c AS JSONB), action = :a WHERE id = :i"""),
        {"n": merged["name"], "e": bool(merged["enabled"]), "cid": merged.get("campaign_id"),
         "c": json.dumps(merged["conditions"]), "a": merged["action"], "i": rule_id})
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "auto_rules", str(rule_id),
                {"fields": list(payload.keys())},
                request.client.host if request.client else "")
    return {"rule": _rule_public(_get_rule(db, rule_id))}


@router.delete("/{rule_id}")
def delete_rule(rule_id: int, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    _get_rule(db, rule_id)
    db.execute(text("DELETE FROM auto_rules WHERE id = :i"), {"i": rule_id})
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "delete", "auto_rules", str(rule_id),
                ip=request.client.host if request.client else "")
    return {"message": "Rule deleted"}


@router.post("/{rule_id}/run")
async def test_run_rule(rule_id: int, db: Session = Depends(get_db)):
    """Evaluate without side effects — returns actual values + would-fire."""
    r = _rule_public(_get_rule(db, rule_id))
    ch = get_clickhouse_client()
    try:
        result = await run_rule(r, ch, persist=False, execute=False)
    finally:
        ch.close()
    result["would_fire"] = result["matched"]
    return result


@router.post("/{rule_id}/execute")
async def execute_rule(rule_id: int, request: Request, db: Session = Depends(get_db)):
    """Evaluate AND perform the action (Run now). Deliberately does NOT persist
    last_result: a manual run must not mark the rule as matched, or the
    background loop's alert-once would be suppressed — only the loop persists."""
    from audit_logger import audit_event
    r = _rule_public(_get_rule(db, rule_id))
    ch = get_clickhouse_client()
    try:
        result = await run_rule(r, ch, persist=False, execute=True)
    finally:
        ch.close()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "rule_executed", "auto_rules", str(rule_id),
                {"matched": result["matched"]},
                request.client.host if request.client else "")
    result["would_fire"] = result["matched"]
    return result
