"""G69 — flow monitoring.

Every 15 minutes the monitor walks the flows of all ACTIVE campaigns, extracts
every destination URL (offer URLs, landing links, redirect URLs) and HTTP-checks
each one (HEAD with a GET fallback; 5xx / timeout / DNS / refused = dead).
Results land in monitor_state; after FAIL_THRESHOLD consecutive failures the
monitor can (setting 'monitoring.auto_disable_flows', default OFF) flip the
flow's `enabled` flag to false inside the campaign config — the tracking plane
skips disabled flows, so this is a pure data-level circuit breaker — and sends
a Telegram alert using the saved Telegram settings.
"""
import asyncio
import html
import json
import re
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db, SessionLocal
from models.settings import SettingsORM

router = APIRouter()

CHECK_INTERVAL_SECONDS = 15 * 60
CHECK_TIMEOUT_SECONDS = 10
FAIL_THRESHOLD = 3
RETENTION_DAYS = 7

# Tracking macros like {click_id} must not reach the HTTP checker as-is —
# %-encoded braces make some servers 5xx, false-positiving auto-disable.
_MACRO_RE = re.compile(r"\{[^{}]*\}")
_MACRO_DUMMY = "aaa"


def checkable_url(url: str) -> str:
    """URL with {macros} replaced by a dummy token, safe for httpx checks."""
    return _MACRO_RE.sub(_MACRO_DUMMY, url)


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def _load_main_settings() -> dict:
    db = SessionLocal()
    try:
        row = db.query(SettingsORM).filter_by(name="settings").first()
        if row and row.value:
            return json.loads(row.value) or {}
    except Exception:
        pass
    finally:
        db.close()
    return {}


def monitoring_settings() -> dict:
    cfg = _load_main_settings()
    mon = cfg.get("monitoring") or {}
    return {"auto_disable_flows": bool(mon.get("auto_disable_flows", False))}


def send_telegram_alert(message: str) -> bool:
    """Fire-and-forget Telegram notification via the saved bot (sync helper —
    call through asyncio.to_thread from async code)."""
    try:
        cfg = _load_main_settings()
        tg = cfg.get("telegram") or {}
        if not tg.get("enabled"):
            return False
        token = (tg.get("bot_token") or "").strip()
        chat_id = (tg.get("chat_id") or "").strip()
        if not token or not chat_id:
            return False
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10)
        return bool(resp.json().get("ok"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

def extract_targets(db: Session) -> list:
    """[{campaign_id, campaign_name, flow_name, entity, entity_id, url}] for
    every enabled flow of every active, non-archived campaign."""
    campaigns = db.execute(text(
        "SELECT id, name, config FROM campaigns "
        "WHERE status = 'active' AND archived = false")).fetchall()
    offers = {o[0]: o[1] for o in db.execute(text("SELECT id, url FROM offers")).fetchall()}
    landings = {l[0]: l[1] for l in db.execute(
        text("SELECT id, link FROM landings WHERE link IS NOT NULL AND link != ''")).fetchall()}

    targets = []
    seen = set()
    for cid, cname, config in campaigns:
        config = config or {}
        for flow in (config.get("flows") or []):
            if not isinstance(flow, dict) or not flow.get("enabled", True):
                continue
            fname = flow.get("name") or f"flow #{flow.get('position', '?')}"
            urls = []  # (entity, entity_id, url)

            redirect_url = (flow.get("redirect_url") or "").strip()
            if flow.get("schema") == "redirect" and redirect_url:
                urls.append(("redirect", None, redirect_url))

            offer_ids = []
            if flow.get("offer"):
                offer_ids.append(flow.get("offer"))
            for oid in (flow.get("offers") or []):
                if oid not in offer_ids:
                    offer_ids.append(oid)
            for oid in offer_ids:
                url = offers.get(int(oid)) if str(oid).isdigit() or isinstance(oid, int) else None
                if url:
                    urls.append(("offer", int(oid), url.strip()))

            landing_ids = []
            if flow.get("landing"):
                landing_ids.append(flow.get("landing"))
            for lid in (flow.get("landings") or []):
                if lid not in landing_ids:
                    landing_ids.append(lid)
            for lid in landing_ids:
                url = landings.get(int(lid)) if str(lid).isdigit() or isinstance(lid, int) else None
                if url and url.startswith(("http://", "https://")):
                    urls.append(("landing", int(lid), url.strip()))

            for entity, entity_id, url in urls:
                key = (cid, entity, entity_id, url)
                if not url or key in seen:
                    continue
                seen.add(key)
                targets.append({"campaign_id": cid, "campaign_name": cname,
                                "flow_name": fname, "entity": entity,
                                "entity_id": entity_id, "url": url})
    return targets


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

async def _check_url(client: httpx.AsyncClient, url: str):
    """-> (status, detail). 5xx / timeout / DNS / connection errors = dead;
    anything else that answers (2xx/3xx/4xx) counts as alive."""
    try:
        resp = await client.head(url, follow_redirects=False)
        if resp.status_code in (405, 501):
            resp = await client.get(url, follow_redirects=False)
        if resp.status_code >= 500:
            return "dead", f"HTTP {resp.status_code}"
        return "ok", f"HTTP {resp.status_code}"
    except httpx.TimeoutException:
        return "dead", "timeout"
    except httpx.TransportError as e:
        return "dead", str(e)[:140]


def _latest_fail_counts(db: Session) -> dict:
    rows = db.execute(text("""
        SELECT DISTINCT ON (url) url, fail_count
        FROM monitor_state ORDER BY url, checked_at DESC""")).fetchall()
    return {r[0]: (r[1] or 0) for r in rows}


def _auto_disable_flow(db: Session, target: dict) -> bool:
    """Flip the owning flow's enabled flag to false in campaign config.
    The tracking plane treats enabled=false flows as ineligible, so traffic
    immediately skips the dead destination. Returns True when flipped."""
    row = db.execute(text("SELECT config FROM campaigns WHERE id = :i"),
                     {"i": target["campaign_id"]}).fetchone()
    if not row:
        return False
    config = row[0] or {}
    changed = False
    for flow in (config.get("flows") or []):
        if not isinstance(flow, dict) or not flow.get("enabled", True):
            continue
        if _flow_matches_url(flow, target):
            flow["enabled"] = False
            flow["disabled_by_monitor"] = True
            changed = True
    if changed:
        db.execute(text("UPDATE campaigns SET config = CAST(:c AS JSONB), "
                        "updated_at = now() WHERE id = :i"),
                   {"c": json.dumps(config), "i": target["campaign_id"]})
        db.commit()
    return changed


def _flow_matches_url(flow: dict, target: dict) -> bool:
    """True when the flow owns the target URL (name match + entity-agnostic
    URL compare against the flow's configured destinations)."""
    if flow.get("name") and flow.get("name") != target["flow_name"]:
        return False
    candidates = [(flow.get("redirect_url") or "").strip()]
    for key in ("offer", "offers", "landing", "landings"):
        candidates.append(str(flow.get(key) or ""))
    return str(target.get("entity_id") or "") in candidates or \
        (target["url"] in [c for c in candidates if c.startswith("http")])


async def run_monitor_cycle() -> dict:
    db = SessionLocal()
    try:
        targets = extract_targets(db)
        prev_fails = _latest_fail_counts(db)
        auto_disable = monitoring_settings().get("auto_disable_flows")

        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        timeout = httpx.Timeout(CHECK_TIMEOUT_SECONDS)
        checked, dead = 0, 0
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
            results = await asyncio.gather(
                *[_check_url(client, checkable_url(t["url"])) for t in targets],
                return_exceptions=True)
        for target, res in zip(targets, results):
            if isinstance(res, Exception):
                status, detail = "dead", str(res)[:140]
            else:
                status, detail = res
            checked += 1
            if status == "dead":
                dead += 1
            fail_count = (prev_fails.get(target["url"], 0) + 1) if status == "dead" else 0
            db.execute(text("""
                INSERT INTO monitor_state (entity, entity_id, campaign_id, url, status, checked_at, fail_count)
                VALUES (:e, :eid, :cid, :u, :s, now(), :fc)"""),
                {"e": target["entity"], "eid": target["entity_id"],
                 "cid": target["campaign_id"], "u": target["url"],
                 "s": status, "fc": fail_count})
            db.commit()

            if fail_count == FAIL_THRESHOLD:
                flipped = False
                if auto_disable:
                    flipped = _auto_disable_flow(db, target)
                esc_url = html.escape(target["url"], quote=True)
                link = f'<a href="{esc_url}">{esc_url}</a>'
                msg = (f"🚨 <b>Monitor: destination down</b>\n\n"
                       f"Campaign: {html.escape(str(target['campaign_name']))} (#{target['campaign_id']})\n"
                       f"Flow: {html.escape(str(target['flow_name']))}\n"
                       f"URL: {link}\n"
                       f"Result: {html.escape(str(detail))} ({FAIL_THRESHOLD} consecutive failures)\n"
                       + ("Flow was <b>disabled automatically</b>."
                          if flipped else "Enable auto-disable in Settings → Monitoring to pause this flow."))
                await asyncio.to_thread(send_telegram_alert, msg)
        # retention: monitor_state grows one row per target per cycle — drop
        # anything older than RETENTION_DAYS each cycle.
        db.execute(text("DELETE FROM monitor_state WHERE checked_at < now() - make_interval(days => :d)"),
                   {"d": RETENTION_DAYS})
        db.commit()
        return {"checked": checked, "dead": dead, "targets": len(targets)}
    finally:
        db.close()


async def monitor_loop():
    """Background scheduler — first pass a minute after boot, then every 15 min."""
    await asyncio.sleep(60)
    while True:
        try:
            summary = await run_monitor_cycle()
            print(f"Monitor cycle: {summary}")
        except Exception as e:
            print("Monitor loop error:", e)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/status")
def monitor_status(db: Session = Depends(get_db)):
    """Latest check result per monitored URL."""
    rows = db.execute(text("""
        SELECT DISTINCT ON (m.url) m.id, m.entity, m.entity_id, m.campaign_id,
               m.url, m.status, m.checked_at, m.fail_count, c.name
        FROM monitor_state m
        LEFT JOIN campaigns c ON c.id = m.campaign_id
        ORDER BY m.url, m.checked_at DESC""")).fetchall()
    items = [{"id": r[0], "entity": r[1], "entity_id": r[2],
              "campaign_id": r[3], "campaign_name": r[8],
              "url": r[4], "status": r[5],
              "checked_at": r[6].isoformat() if r[6] else None,
              "fail_count": r[7]} for r in rows]
    items.sort(key=lambda x: (x["status"] != "dead", x["url"]))
    return {"items": items, "auto_disable_flows": monitoring_settings()["auto_disable_flows"],
            "interval_minutes": CHECK_INTERVAL_SECONDS // 60,
            "fail_threshold": FAIL_THRESHOLD}


@router.post("/check-now")
async def check_now(request: Request):
    """Run a monitoring cycle immediately (async, waits for completion)."""
    summary = await run_monitor_cycle()
    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "monitor_check", "monitoring", "",
                summary, request.client.host if request.client else "")
    return summary
