"""G70 — auto rules.

Administrators define rules like "ROI < -30% over the last 6 hours → pause the
campaign" or "conversions > 100 over 24h → alert". A background loop (every 15
minutes) evaluates every enabled rule against ClickHouse metrics for its own
lookback window and performs the action when ALL conditions match:
  - pause_campaign: flips campaigns.status to 'paused'
  - alert_telegram:   message via the saved Telegram bot
  - alert_email:      message via the saved email settings (Brevo/SMTP)

A rule may also carry a per-rule email recipient list. When it is set, an email
goes out on fire in addition to the action — and for the alert_email action the
per-rule list is used instead of the workspace-wide one. Recipients are never
SMTP credentials: delivery still uses the deployment's relay (env) plus the
workspace's email_reports block.

Scheduling: a rule with no ``schedule`` is evaluated on the loop's 15-minute
cadence, exactly as before. A rule may instead set a schedule mode of
``interval`` (every N minutes), ``days_times`` (chosen weekdays + clock times)
or ``date_range`` (only between two dates).

IMPORTANT (documented limit): the tracking plane currently routes traffic for
paused campaigns too — pausing is admin-plane state + an alert, it does NOT
stop clicks. Use flow auto-disable (Monitoring) for traffic-level cutoffs.
Alert actions fire once per continuous match (they re-arm after the condition
clears).
"""
import asyncio
import json
import re
from tenant_context import current_tenant
from tenant_settings import for_each_tenant
from datetime import datetime, timedelta

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
SCHEDULE_MODES = ("cadence", "interval", "days_times", "date_range")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{1,2}$")

# G78 — last cycle that ran at least one rule, surfaced on the status page.
_loop_last_run = None

# Additive columns are created lazily on first use (see _ensure_rule_columns).
_columns_ready = False


# ---------------------------------------------------------------------------
# Schema (additive)
# ---------------------------------------------------------------------------

def _ensure_rule_columns() -> None:
    """Add the email-target and schedule columns if they are missing.

    The table itself is created by the app's startup migration, but app.py is
    not ours to edit, so the two additive columns are ensured here — idempotent
    and cheap (guarded by ``_columns_ready``). Never raises: if the ALTER
    fails the engine keeps running on the legacy columns rather than taking
    the whole API down.
    """
    global _columns_ready
    if _columns_ready:
        return
    db = SessionLocal()
    try:
        db.execute(text("ALTER TABLE auto_rules ADD COLUMN IF NOT EXISTS "
                        "email_recipients JSONB NOT NULL DEFAULT '[]'::jsonb"))
        db.execute(text("ALTER TABLE auto_rules ADD COLUMN IF NOT EXISTS "
                        "schedule JSONB NOT NULL DEFAULT '{}'::jsonb"))
        db.commit()
        _columns_ready = True
    except Exception as e:
        db.rollback()
        print("Auto rules schema check failed:", e)
    finally:
        db.close()


def _row_json(v, default):
    """JSONB columns come back as native list/dict from SQLAlchemy; tolerate a
    raw string for older drivers/tests."""
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


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
    email_recipients: list = []
    schedule: dict = {}


def _clean_recipients(value) -> list:
    """Normalise a recipient list (list or comma string) and validate every
    address so a typo cannot silently produce an undeliverable rule."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail="email_recipients must be a list")
    out = []
    for item in value:
        addr = str(item or "").strip()
        if not addr:
            continue
        if not _EMAIL_RE.match(addr):
            raise HTTPException(status_code=400, detail=f"invalid email address: {addr[:80]}")
        if addr not in out:
            out.append(addr)
    return out


def _clean_schedule(value) -> dict:
    """Validate a rule's schedule block.

    An empty/absent value is the legacy 15-minute cadence, so existing stored
    rules keep their exact meaning.
    """
    if not value:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="schedule must be an object")
    mode = value.get("mode") or "cadence"
    if mode not in SCHEDULE_MODES:
        raise HTTPException(status_code=400,
                            detail=f"schedule mode must be one of: {', '.join(SCHEDULE_MODES)}")
    if mode == "cadence":
        return {}
    out = {"mode": mode}
    if mode == "interval":
        try:
            minutes = int(value.get("interval_minutes"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="interval_minutes must be an integer")
        if not 1 <= minutes <= 24 * 60 * 7:
            raise HTTPException(status_code=400, detail="interval_minutes must be 1-10080")
        out["interval_minutes"] = minutes
        return out
    if mode == "days_times":
        days = value.get("days")
        if not isinstance(days, list) or not days:
            raise HTTPException(status_code=400, detail="schedule days is required")
        clean_days = []
        for d in days:
            try:
                di = int(d)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400,
                                    detail="schedule days must be integers 0-6 (Mon-Sun)")
            if not 0 <= di <= 6:
                raise HTTPException(status_code=400,
                                    detail="schedule days must be 0-6 (Mon-Sun)")
            if di not in clean_days:
                clean_days.append(di)
        times = value.get("times")
        if isinstance(times, str):
            times = times.split(",")
        if not isinstance(times, list) or not times:
            raise HTTPException(status_code=400, detail="schedule times is required")
        clean_times = []
        for t in times:
            ts = str(t or "").strip()
            if not _TIME_RE.match(ts):
                raise HTTPException(status_code=400, detail=f"invalid time: {ts[:20]} (use HH:MM)")
            hh, mm = (int(x) for x in ts.split(":"))
            if hh > 23 or mm > 59:
                raise HTTPException(status_code=400, detail=f"invalid time: {ts[:20]} (use HH:MM)")
            norm = f"{hh:02d}:{mm:02d}"
            if norm not in clean_times:
                clean_times.append(norm)
        out["days"] = clean_days
        out["times"] = clean_times
        return out
    # date_range
    def _date(v, label):
        if v in (None, ""):
            return None
        s = str(v).strip()
        try:
            datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{label} must be YYYY-MM-DD")
        return s

    dfrom = _date(value.get("date_from"), "date_from")
    dto = _date(value.get("date_to"), "date_to")
    if dfrom and dto and dfrom > dto:
        raise HTTPException(status_code=400, detail="date_from must be on or before date_to")
    if not dfrom and not dto:
        raise HTTPException(status_code=400,
                            detail="date_range schedule needs date_from or date_to")
    if dfrom:
        out["date_from"] = dfrom
    if dto:
        out["date_to"] = dto
    return out


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
    # Additive fields: normalise/validate whenever the caller supplied them.
    if "email_recipients" in payload or not partial:
        payload["email_recipients"] = _clean_recipients(payload.get("email_recipients"))
    if "schedule" in payload or not partial:
        payload["schedule"] = _clean_schedule(payload.get("schedule"))
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
        where = ("tenant_id = %(tenant_id)s AND "
                 "received_at >= now() - toIntervalHour(%(hours)s)")
        params = {"hours": hours, "tenant_id": current_tenant()}
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
            "last_result": r[8],
            "email_recipients": _row_json(r[10] if len(r) > 10 else None, []),
            "schedule": _row_json(r[11] if len(r) > 11 else None, {})}


def _get_rule(db: Session, rule_id: int):
    _ensure_rule_columns()
    r = db.execute(text("SELECT * FROM auto_rules WHERE id = :i AND tenant_id = :tid"),
                   {"i": rule_id, "tid": current_tenant()}).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Rule not found")
    return r


def _creators_for(db: Session, ids) -> dict:
    """rule id -> {created_by, created_at}, read from the audit log.

    ``create_rule`` already calls ``audit_logger.audit_event`` with
    entity='auto_rules', action='create' and entity_id=<rule id>, so the
    creator and timestamp live in audit_log — no new table or column needed.
    """
    if not ids:
        return {}
    out = {}
    try:
        from audit_logger import ensure_audit_table
        ensure_audit_table()
        rows = db.execute(text(
            "SELECT entity_id, username, at FROM audit_log "
            "WHERE entity = 'auto_rules' AND action = 'create' AND tenant_id = :tid "
            "AND entity_id = ANY(:ids) ORDER BY at ASC"),
            {"tid": current_tenant(), "ids": [str(i) for i in ids]}).fetchall()
        for eid, username, at in rows:
            out.setdefault(str(eid), {"created_by": username,
                                      "created_at": at.isoformat() if at else None})
    except Exception:
        # Auditing is best-effort; a creator cell falls back to "—".
        pass
    return out


def send_rule_email(rule: dict, evaluation: dict) -> dict:
    """Send the rule's per-rule email target alongside a non-email action.

    Reuses the deployment relay through the same config path as
    ``alert_email``; the recipient list is the only rule-owned part.
    """
    try:
        import html as _html
        from email_reports import send_email
        from app_pages.monitor import _load_main_settings
        from env_config import email_configured
        recipients = list(rule.get("email_recipients") or [])
        if not recipients:
            return {"ok": False, "detail": "no per-rule email recipients"}
        cfg = (_load_main_settings().get("email_reports") or {})
        if not email_configured(cfg):
            return {"ok": False, "detail": "email delivery is not configured for this deployment"}
        cond_desc = ", ".join(f"{c['metric']} {c['comparator']} {c['value']} "
                              f"(actual {c['actual']})" for c in evaluation["conditions"])
        send_email(cfg, f"Auto rule fired: {rule.get('name')}",
                   f"<p>Rule <b>{_html.escape(str(rule.get('name')))}</b> matched:</p>"
                   f"<p>{_html.escape(cond_desc)}</p>", recipients)
        return {"ok": True, "detail": f"email sent to {', '.join(recipients)}"}
    except Exception as e:
        return {"ok": False, "detail": f"email failed: {str(e)[:120]}"}


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
                                  "WHERE id = :i AND status != 'paused' AND tenant_id = :tid"),
                             {"i": int(cid), "tid": current_tenant()})
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
            from env_config import email_configured
            cfg = (_load_main_settings().get("email_reports") or {})
            # A per-rule list wins over the workspace-wide recipients.
            recipients = [r.strip() for r in (rule.get("email_recipients") or []) if str(r).strip()]
            if not recipients:
                recipients = [r.strip() for r in (cfg.get("recipients") or "").split(",") if r.strip()]
            if not recipients:
                return {"ok": False, "detail": "no email recipients configured"}
            if not email_configured(cfg):
                return {"ok": False, "detail": "email delivery is not configured for this deployment"}
            send_email(cfg, f"Auto rule fired: {rule.get('name')}",
                       f"<p>Rule <b>{rule.get('name')}</b> matched:</p>"
                       f"<p>{cond_desc}</p>", recipients)
            return {"ok": True, "detail": f"email sent to {', '.join(recipients)}"}
        except Exception as e:
            return {"ok": False, "detail": f"email failed: {str(e)[:120]}"}
    return {"ok": False, "detail": f"unknown action {action}"}


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def rule_due(rule: dict, now: datetime) -> bool:
    """True when the scheduler should evaluate ``rule`` at ``now`` (UTC).

    No schedule (mode "cadence") is always due — the legacy 15-minute pass.
    A malformed stored schedule also falls back to cadence so one bad row can
    never stall the whole pass (failure isolation).
    """
    sched = rule.get("schedule") or {}
    mode = sched.get("mode") or "cadence"
    if mode == "cadence":
        return True
    last = None
    if rule.get("last_run"):
        try:
            last = datetime.fromisoformat(str(rule["last_run"]))
        except (TypeError, ValueError):
            last = None
    try:
        if mode == "interval":
            minutes = int(sched.get("interval_minutes") or 0)
            if minutes <= 0:
                return True
            return last is None or (now - last) >= timedelta(minutes=minutes)
        if mode == "date_range":
            today = now.strftime("%Y-%m-%d")
            if sched.get("date_from") and today < sched["date_from"]:
                return False
            if sched.get("date_to") and today > sched["date_to"]:
                return False
            return True
        if mode == "days_times":
            if now.weekday() not in (sched.get("days") or []):
                return False
            # The loop ticks every 15 min, so a chosen time is "hit" by the
            # first tick inside the following cadence window; last_run < slot
            # keeps it to once per slot.
            window = timedelta(seconds=LOOP_INTERVAL_SECONDS)
            for t in (sched.get("times") or []):
                hh, mm = (int(x) for x in str(t).split(":"))
                slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if slot <= now < slot + window and (last is None or last < slot):
                    return True
            return False
    except (TypeError, ValueError, KeyError):
        return True
    return True


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def _tenant_rules_pass(tenant_id: int) -> None:
    """Evaluate one tenant's due rules, then run its anomaly insights.

    Runs inside the tenant context set by ``for_each_tenant``, so the rule
    lookup, the ClickHouse metric query (through ``current_tenant()``), the
    persisted ``last_run`` and the Telegram/email alerts are all scoped to it.
    """
    global _loop_last_run
    _ensure_rule_columns()
    db = SessionLocal()
    try:
        rules = db.execute(text("SELECT * FROM auto_rules "
                                "WHERE enabled = true AND tenant_id = :tid"),
                           {"tid": current_tenant()}).fetchall()
    finally:
        db.close()
    if rules:
        ch = get_clickhouse_client()
        try:
            now = datetime.utcnow()
            for r in rules:
                try:
                    rule = dict(_rule_public(r))
                    if not rule_due(rule, now):
                        continue
                    await run_rule(rule, ch, persist=True)
                except Exception as e:
                    print(f"Auto rule {r[0]} (tenant {tenant_id}) error:", e)
        finally:
            ch.close()
    _loop_last_run = datetime.utcnow()
    # G56 — anomaly insights share this 15-min cadence (no separate loop)
    try:
        from app_pages import insights
        summary = await asyncio.to_thread(insights.run_analysis, "auto_rules_loop")
        if summary.get("telegram_sent"):
            print(f"Insights (tenant {tenant_id}): new critical findings alerted")
    except Exception as e:
        print(f"Insights run error (tenant {tenant_id}):", e)


async def auto_rules_loop():
    """Evaluate every enabled rule of every active tenant every 15 minutes."""
    await asyncio.sleep(90)  # stagger vs the monitor loop
    while True:
        try:
            await for_each_tenant(_tenant_rules_pass)
        except Exception as e:
            print("Auto rules loop error:", e)
        await asyncio.sleep(LOOP_INTERVAL_SECONDS)


async def run_rule(rule: dict, ch, persist: bool = False, execute: bool = True) -> dict:
    """Evaluate one rule; optionally execute + persist the outcome."""
    evaluation = evaluate_rule(ch, rule)
    result = {"matched": evaluation["matched"], "conditions": evaluation["conditions"],
              "action": rule.get("action"), "action_taken": None}
    if evaluation["matched"] and execute:
        # alert actions re-arm once the condition clears — don't nag every cycle
        prev = rule.get("last_result") or {}
        if rule.get("action") == "pause_campaign" or not prev.get("matched"):
            action_result = await asyncio.to_thread(perform_action, rule, evaluation)
            result["action_taken"] = action_result
            # The per-rule email target rides alongside any action except
            # alert_email (which already delivers to that same list).
            if rule.get("email_recipients") and rule.get("action") != "alert_email":
                result["notification"] = await asyncio.to_thread(
                    send_rule_email, rule, evaluation)
    if persist:
        db = SessionLocal()
        try:
            db.execute(text("UPDATE auto_rules SET last_run = now(), "
                            "last_result = CAST(:r AS JSONB) "
                            "WHERE id = :i AND tenant_id = :tid"),
                       {"r": json.dumps(result), "i": int(rule["id"]),
                        "tid": current_tenant()})
            db.commit()
        finally:
            db.close()
    return result


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/")
def list_rules(db: Session = Depends(get_db)):
    _ensure_rule_columns()
    rows = db.execute(text("SELECT * FROM auto_rules WHERE tenant_id = :tid "
                           "ORDER BY id ASC"), {"tid": current_tenant()}).fetchall()
    rules = [_rule_public(r) for r in rows]
    creators = _creators_for(db, [x["id"] for x in rules])
    for x in rules:
        x.update(creators.get(str(x["id"]), {"created_by": None, "created_at": None}))
    return {"rules": rules}


@router.post("/")
def create_rule(payload: RuleIn, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    _ensure_rule_columns()
    data = _validate(payload.dict())
    if data.get("action") == "pause_campaign" and not data.get("campaign_id"):
        raise HTTPException(status_code=400, detail="pause_campaign requires a campaign_id")
    r = db.execute(text("""
        INSERT INTO auto_rules (name, enabled, scope, campaign_id, conditions, action,
                                tenant_id, email_recipients, schedule)
        VALUES (:n, :e, 'campaign', :cid, CAST(:c AS JSONB), :a, :tid,
                CAST(:er AS JSONB), CAST(:s AS JSONB)) RETURNING id"""),
        {"n": data["name"].strip(), "e": bool(data.get("enabled", True)),
         "cid": data.get("campaign_id"), "c": json.dumps(data["conditions"]),
         "a": data["action"], "tid": current_tenant(),
         "er": json.dumps(data["email_recipients"]),
         "s": json.dumps(data["schedule"])}).fetchone()
    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "create", "auto_rules", str(r[0]),
                {"name": data["name"], "action": data["action"]},
                request.client.host if request.client else "")
    rule = _rule_public(_get_rule(db, r[0]))
    rule.update(_creators_for(db, [r[0]]).get(
        str(r[0]), {"created_by": caller or "api_token",
                    "created_at": datetime.utcnow().isoformat()}))
    return {"rule": rule}


@router.patch("/{rule_id}")
def update_rule(rule_id: int, payload: dict, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    r = _get_rule(db, rule_id)
    merged = _rule_public(r)
    merged.update({k: v for k, v in payload.items() if k in
                   ("name", "enabled", "campaign_id", "conditions", "action",
                    "email_recipients", "schedule")})
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
            conditions = CAST(:c AS JSONB), action = :a,
            email_recipients = CAST(:er AS JSONB), schedule = CAST(:s AS JSONB)
        WHERE id = :i AND tenant_id = :tid"""),
        {"n": merged["name"], "e": bool(merged["enabled"]), "cid": merged.get("campaign_id"),
         "c": json.dumps(merged["conditions"]), "a": merged["action"], "i": rule_id,
         "er": json.dumps(merged["email_recipients"]),
         "s": json.dumps(merged["schedule"]), "tid": current_tenant()})
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "auto_rules", str(rule_id),
                {"fields": list(payload.keys())},
                request.client.host if request.client else "")
    rule = _rule_public(_get_rule(db, rule_id))
    rule.update(_creators_for(db, [rule_id]).get(
        str(rule_id), {"created_by": caller or "api_token", "created_at": None}))
    return {"rule": rule}


@router.delete("/{rule_id}")
def delete_rule(rule_id: int, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    _get_rule(db, rule_id)
    db.execute(text("DELETE FROM auto_rules WHERE id = :i AND tenant_id = :tid"),
               {"i": rule_id, "tid": current_tenant()})
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "delete", "auto_rules", str(rule_id),
                ip=request.client.host if request.client else "")
    return {"message": "Rule deleted"}


@router.post("/{rule_id}/run")
async def test_run_rule(rule_id: int, db: Session = Depends(get_db)):
    """Evaluate without side effects — returns actual values + would-fire.

    Manual runs deliberately ignore the schedule: the button is how an
    operator checks a rule's metrics now, regardless of its due window."""
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
