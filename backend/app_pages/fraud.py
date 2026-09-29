"""Fraud & cloaking dashboard.

Admin-plane readout over ClickHouse fraud signals (is_bot, fraud_score) plus
the campaign-level "shield" cloaking blocks stored in campaigns.config:
  {"enabled": bool, "whitelists": {"ips": [CIDR], "referers": [substrings],
   "ua_regex": str}, "action": "blank"|"404"|"allow", "honeypot": bool}

Endpoints:
  GET /api/fraud/summary        — 24h cards, top fraud IPs / UAs, shield stats
  GET /api/fraud/feed?after=    — live feed of bot/high-score clicks (polling)
  GET/PUT /api/fraud/bot-lists  — global editable lists in settings "bot_lists"
  GET /api/fraud/honeypot-hits  — recent PG honeypot_hits rows
"""
import ipaddress
import json
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from tenant_context import current_tenant
from models.settings import SettingsORM

router = APIRouter()

FRAUD_SCORE_THRESHOLD = 50
FEED_LIMIT = 100


# ---------------------------------------------------------------------------
# Schema (idempotent — coordinates with the tracking-plane migration)
# ---------------------------------------------------------------------------

def ensure_fraud_schema(ch) -> None:
    """clicks_data.fraud_score (parallel agent's inserts rely on it existing);
    PG honeypot_hits landing table. Both are IF-NOT-EXISTS/no-op safe."""
    try:
        ch.command("ALTER TABLE clicks_data ADD COLUMN IF NOT EXISTS fraud_score UInt8 DEFAULT 0")
    except Exception as e:
        print("fraud schema (clickhouse):", e)
    from db import engine
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS honeypot_hits (
                    id BIGSERIAL PRIMARY KEY,
                    visitor_key TEXT,
                    ip VARCHAR(64),
                    ua TEXT,
                    campaign_id INTEGER,
                    received_at TIMESTAMP NOT NULL DEFAULT now(),
                    tenant_id INTEGER NOT NULL DEFAULT 1
                )"""))
            # A database created before multi-tenancy (or by an older build of
            # this module) needs the column added in place.
            conn.execute(text("ALTER TABLE honeypot_hits "
                              "ADD COLUMN IF NOT EXISTS tenant_id INTEGER"))
            conn.execute(text("UPDATE honeypot_hits SET tenant_id = 1 "
                              "WHERE tenant_id IS NULL"))
            conn.execute(text("ALTER TABLE honeypot_hits "
                              "ALTER COLUMN tenant_id SET NOT NULL"))
            conn.commit()
    except Exception as e:
        print("fraud schema (postgres):", e)


# ---------------------------------------------------------------------------
# Settings helpers (settings row 'settings', JSON block "bot_lists")
# ---------------------------------------------------------------------------

BOT_LIST_KEYS = ("ua_regex", "ip_cidrs", "referer_regex")


def _load_main_settings(db: Session) -> dict:
    row = db.query(SettingsORM).filter_by(name="settings").first()
    if row and row.value:
        try:
            return json.loads(row.value) or {}
        except Exception:
            return {}
    return {}


def _validate_bot_lists(block: dict) -> dict:
    if not isinstance(block, dict):
        raise HTTPException(status_code=422, detail="bot_lists must be an object")
    out = {}
    for key in BOT_LIST_KEYS:
        val = block.get(key)
        if val is None:
            val = []
        if not isinstance(val, list):
            raise HTTPException(status_code=422, detail=f"{key} must be a list")
        out[key] = [str(v).strip() for v in val if str(v).strip()]
    for key in ("ua_regex", "referer_regex"):
        for pat in out[key]:
            try:
                re.compile(pat)
            except re.error as e:
                raise HTTPException(status_code=422,
                                    detail=f"Invalid {key} pattern {pat!r}: {e}")
    for cidr in out["ip_cidrs"]:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"Invalid ip_cidr {cidr!r}: {e}")
    return out


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/summary")
def fraud_summary(request: Request, db: Session = Depends(get_db)):
    ch = request.state.ch
    # Interpolated (not parameterised): the value is an int from the request
    # context, never user input.
    period = (f"tenant_id = {int(current_tenant())} "
              "AND received_at >= now() - toIntervalHour(24)")

    row = ch.query(f"""
        SELECT count() AS total,
               countIf(is_bot = true) AS bot_clicks,
               avgOrNull(fraud_score) AS avg_fraud_score,
               avgOrNull(cost) AS avg_cost
        FROM clicks_data
        WHERE {period}""").result_rows
    total, bot_clicks, avg_fraud_score, avg_cost = (row[0] if row else (0, 0, None, None))
    total, bot_clicks = int(total or 0), int(bot_clicks or 0)

    # Est. savings: bot clicks x the average cost of a NON-bot click in the
    # same window (blocked/untracked clicks cannot be counted directly).
    row = ch.query(f"""
        SELECT avgOrNull(cost) AS avg_human_cost
        FROM clicks_data
        WHERE {period} AND NOT (is_bot = true)""").result_rows
    avg_human_cost = float(row[0][0] or 0) if row else 0.0
    est_savings = round(bot_clicks * avg_human_cost, 4)

    fraud_where = f"({period}) AND (is_bot = true OR fraud_score >= {FRAUD_SCORE_THRESHOLD})"
    ip_rows = ch.query(f"""
        SELECT if(empty(ip_full), toString(ip), ip_full) AS ip, count() AS hits,
               round(avg(fraud_score), 1) AS avg_score,
               max(received_at) AS last_seen
        FROM clicks_data
        WHERE {fraud_where}
        GROUP BY ip
        ORDER BY hits DESC
        LIMIT 10""").result_rows
    top_ips = [{"ip": r[0], "hits": int(r[1]), "avg_score": float(r[2] or 0),
                "last_seen": str(r[3])} for r in ip_rows]

    ua_rows = ch.query(f"""
        SELECT browser AS ua, count() AS hits,
               round(avg(fraud_score), 1) AS avg_score,
               max(received_at) AS last_seen
        FROM clicks_data
        WHERE {fraud_where}
        GROUP BY browser
        ORDER BY hits DESC
        LIMIT 10""").result_rows
    top_uas = [{"ua": r[0], "hits": int(r[1]), "avg_score": float(r[2] or 0),
                "last_seen": str(r[3])} for r in ua_rows]

    shield_rows = db.execute(text(
        "SELECT id, name, config FROM campaigns "
        "WHERE archived = false AND tenant_id = :tid"),
        {"tid": current_tenant()}).fetchall()
    shields = []
    for cid, cname, config in shield_rows:
        shield = (config or {}).get("shield") or {}
        if not isinstance(shield, dict) or not shield.get("enabled"):
            continue
        shields.append({
            "campaign_id": cid, "campaign_name": cname,
            "action": shield.get("action") or "blank",
            "honeypot": bool(shield.get("honeypot")),
            "whitelist_ips": len((shield.get("whitelists") or {}).get("ips") or []),
            "whitelist_referers": len((shield.get("whitelists") or {}).get("referers") or []),
            "whitelist_ua_regex": bool((shield.get("whitelists") or {}).get("ua_regex")),
        })

    return {
        "period_hours": 24,
        "total_clicks": total,
        "bot_clicks": bot_clicks,
        "bot_share_pct": round(bot_clicks / total * 100, 2) if total else 0.0,
        "avg_fraud_score": round(float(avg_fraud_score or 0), 2),
        "avg_cost_non_bot": round(avg_human_cost, 4),
        "est_savings": est_savings,
        "top_ips": top_ips,
        "top_uas": top_uas,
        "shields": shields,
    }


@router.get("/feed")
def fraud_feed(request: Request, after: str = None, limit: int = FEED_LIMIT):
    ch = request.state.ch
    limit = min(max(int(limit or FEED_LIMIT), 1), 500)
    conditions = ["tenant_id = %(tenant_id)s",
                  f"(is_bot = true OR fraud_score >= {FRAUD_SCORE_THRESHOLD})"]
    params = {"limit": limit, "tenant_id": current_tenant()}
    if after:
        conditions.append("received_at > %(after)s")
        params["after"] = after
    where_clause = f"WHERE {' AND '.join(conditions)}"

    query = f"""
        SELECT
            received_at, visitor_id,
            if(empty(ip_full), toString(ip), ip_full) AS ip,
            campaign_id, country, device_type,
            os, browser, referrer, url, status, is_bot, is_using_proxy,
            fraud_score
        FROM clicks_data
        {where_clause}
        ORDER BY received_at DESC
        LIMIT %(limit)s
    """
    result = ch.query(query, parameters=params)
    columns = result.column_names
    return [dict(zip(columns, row)) for row in result.result_rows]


@router.get("/bot-lists")
def get_bot_lists(db: Session = Depends(get_db)):
    block = (_load_main_settings(db).get("bot_lists") or {})
    out = {key: [] for key in BOT_LIST_KEYS}
    if isinstance(block, dict):
        for key in BOT_LIST_KEYS:
            val = block.get(key)
            if isinstance(val, list):
                out[key] = [str(v) for v in val]
    return {"bot_lists": out}


@router.put("/bot-lists")
def put_bot_lists(payload: dict, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    block = _validate_bot_lists((payload or {}).get("bot_lists") or payload or {})

    row = db.execute(
        text("SELECT id, value FROM settings "
             "WHERE name = 'settings' AND tenant_id = :tid FOR UPDATE"),
        {"tid": current_tenant()}
    ).fetchone()
    if row:
        try:
            existing = json.loads(row[1]) if row[1] else {}
        except Exception:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing["bot_lists"] = block
        db.execute(text("UPDATE settings SET value = :v WHERE id = :i"),
                   {"v": json.dumps(existing), "i": row[0]})
    else:
        db.add(SettingsORM(name="settings", value=json.dumps({"bot_lists": block})))
    db.commit()

    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "bot_lists", "",
                {k: len(v) for k, v in block.items()},
                request.client.host if request.client else "")
    return {"bot_lists": block}


@router.get("/honeypot-hits")
def honeypot_hits(request: Request, limit: int = 100, db: Session = Depends(get_db)):
    limit = min(max(int(limit or 100), 1), 500)
    rows = db.execute(text(
        "SELECT id, visitor_key, ip, ua, campaign_id, received_at "
        "FROM honeypot_hits WHERE tenant_id = :tid "
        "ORDER BY received_at DESC LIMIT :l"),
        {"l": limit, "tid": current_tenant()}).fetchall()
    return {"hits": [{
        "id": r[0], "visitor_key": r[1], "ip": r[2], "ua": r[3],
        "campaign_id": r[4],
        "received_at": r[5].isoformat() if r[5] else None,
    } for r in rows]}


# ---------------------------------------------------------------------------
# G44 — traffic-quality blacklists (settings block "blacklists")
# ---------------------------------------------------------------------------

BLACKLIST_FIELDS = ("sub_id_1", "sub_id_2", "sub_id_3", "sub_id_4", "sub_id_5",
                    "sub_id_6", "sub_id_7", "sub_id_8", "sub_id_9", "sub_id_10",
                    "country", "city", "device_type", "os", "browser", "ip")
BLACKLIST_ACTIONS = ("mark", "block")
BLACKLIST_SCOPES = ("global", "campaign")
BLACKLIST_MAX_VALUES = 5000


def _validate_blacklist(payload: dict, existing: dict = None) -> dict:
    out = dict(existing or {})
    name = str(payload.get("name", out.get("name") or "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Blacklist name is required")
    out["name"] = name[:255]

    field = payload.get("field", out.get("field"))
    if field not in BLACKLIST_FIELDS:
        raise HTTPException(status_code=400,
                            detail=f"field must be one of: {', '.join(BLACKLIST_FIELDS)}")
    out["field"] = field

    raw_values = payload.get("values", out.get("values"))
    if not isinstance(raw_values, list):
        raise HTTPException(status_code=400, detail="values must be a list")
    values = []
    for v in raw_values:
        v = str(v).strip()
        if v:
            values.append(v[:512])
    if not values:
        raise HTTPException(status_code=400, detail="At least one value is required")
    if len(values) > BLACKLIST_MAX_VALUES:
        raise HTTPException(status_code=400,
                            detail=f"Too many values (max {BLACKLIST_MAX_VALUES})")
    if field == "ip":
        import ipaddress
        for v in values:
            if "/" in v:
                try:
                    ipaddress.ip_network(v, strict=False)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"Invalid CIDR {v!r}: {e}")
            else:
                try:
                    ipaddress.ip_address(v)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"Invalid IP {v!r}: {e}")
    out["values"] = values

    scope = payload.get("scope", out.get("scope") or "global")
    if scope not in BLACKLIST_SCOPES:
        raise HTTPException(status_code=400,
                            detail=f"scope must be one of: {', '.join(BLACKLIST_SCOPES)}")
    out["scope"] = scope
    campaign_id = payload.get("campaign_id", out.get("campaign_id"))
    if scope == "campaign":
        try:
            campaign_id = int(campaign_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail="campaign_id is required for campaign scope")
        exists = db_campaign_exists(campaign_id)
        if not exists:
            raise HTTPException(status_code=400, detail=f"Campaign {campaign_id} not found")
    else:
        campaign_id = None
    out["campaign_id"] = campaign_id

    action = payload.get("action", out.get("action") or "mark")
    if action not in BLACKLIST_ACTIONS:
        raise HTTPException(status_code=400,
                            detail=f"action must be one of: {', '.join(BLACKLIST_ACTIONS)}")
    out["action"] = action

    out["enabled"] = bool(payload.get("enabled", out.get("enabled", True)))
    return out


def db_campaign_exists(campaign_id: int) -> bool:
    from db import SessionLocal
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT 1 FROM campaigns "
                              "WHERE id = :i AND tenant_id = :tid"),
                         {"i": campaign_id, "tid": current_tenant()}).fetchone()
        return row is not None
    finally:
        db.close()


def _save_blacklists_block(db: Session, lists: list) -> None:
    row = db.execute(
        text("SELECT id, value FROM settings "
             "WHERE name = 'settings' AND tenant_id = :tid FOR UPDATE"),
        {"tid": current_tenant()}
    ).fetchone()
    if row:
        try:
            existing = json.loads(row[1]) if row[1] else {}
        except Exception:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing["blacklists"] = lists
        db.execute(text("UPDATE settings SET value = :v WHERE id = :i"),
                   {"v": json.dumps(existing), "i": row[0]})
    else:
        db.add(SettingsORM(name="settings", value=json.dumps({"blacklists": lists})))


def _blacklist_audit(request: Request, action: str, bl: dict) -> None:
    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", action, "blacklist", bl.get("id") or "",
                {"name": bl.get("name"), "field": bl.get("field"),
                 "values": len(bl.get("values") or []), "scope": bl.get("scope"),
                 "campaign_id": bl.get("campaign_id"), "action": bl.get("action"),
                 "enabled": bl.get("enabled")},
                request.client.host if request.client else "")


@router.get("/blacklists")
def get_blacklists(db: Session = Depends(get_db)):
    block = _load_main_settings(db).get("blacklists")
    lists = block if isinstance(block, list) else []
    return {"blacklists": [b for b in lists if isinstance(b, dict)]}


@router.post("/blacklists")
def create_blacklist(payload: dict, request: Request, db: Session = Depends(get_db)):
    bl = _validate_blacklist(payload or {})
    bl["id"] = uuid.uuid4().hex[:12]
    bl["created_at"] = datetime.utcnow().isoformat()

    lists = get_blacklists(db)["blacklists"]
    lists.append(bl)
    _save_blacklists_block(db, lists)
    db.commit()
    _blacklist_audit(request, "create", bl)
    return bl


@router.put("/blacklists/{bl_id}")
def update_blacklist(bl_id: str, payload: dict, request: Request,
                     db: Session = Depends(get_db)):
    lists = get_blacklists(db)["blacklists"]
    for i, bl in enumerate(lists):
        if bl.get("id") == bl_id:
            updated = _validate_blacklist(payload or {}, existing=bl)
            updated["id"] = bl_id
            updated["created_at"] = bl.get("created_at")
            lists[i] = updated
            _save_blacklists_block(db, lists)
            db.commit()
            _blacklist_audit(request, "update", updated)
            return updated
    raise HTTPException(status_code=404, detail="Blacklist not found")


@router.delete("/blacklists/{bl_id}")
def delete_blacklist(bl_id: str, request: Request, db: Session = Depends(get_db)):
    lists = get_blacklists(db)["blacklists"]
    remaining = [b for b in lists if b.get("id") != bl_id]
    if len(remaining) == len(lists):
        raise HTTPException(status_code=404, detail="Blacklist not found")
    _save_blacklists_block(db, remaining)
    db.commit()
    _blacklist_audit(request, "delete", {"id": bl_id})
    return {"status": "deleted", "id": bl_id}
