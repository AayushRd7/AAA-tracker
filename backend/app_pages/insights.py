"""G56 — anomaly insights.

Read-only detectors that compare each ACTIVE campaign's last 24 hours against
the previous 7 full days (per-day average) and surface anomalies:

  ctr_drop              conversions-per-click drop > 50% (critical > 75%),
                        ≥ 20 click-outs in both windows
  cost_spike            cost > 3x the 7-day daily average with ≥ $5 baseline
  click_drop            click-outs down > 60% vs the 7-day daily average with
                        ≥ 50 baseline click-outs (possible dead flow/source
                        outage — cross-references monitor-disabled flows)
  bot_surge             bot share > 40% over ≥ 100 clicks (see Fraud page)
  revenue_stop          revenue on 6 of the last 7 days but none today,
                        ≥ 20 clicks today
  zero_conversion_spend > $50 spend and zero conversions over ≥ 100 clicks

Documented limits: the baseline is the trailing 7-day daily average (not an
hour-of-day-matched average) to keep one ClickHouse query per campaign; a
detector never fires when the baseline is zero (insufficient data).

The last result is persisted in the settings row as block "insights_last" so
the UI loads instantly. One Telegram alert per run fires when NEW critical
findings appear vs the previous run (silent when Telegram is not configured).

Auto-run rides the existing 15-minute auto-rules cadence (no separate loop —
see auto_rules_loop in rules.py).
"""
import asyncio
import html
import json
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db, SessionLocal
from tenant_context import current_tenant
from models.settings import SettingsORM
from clickHouse import get_clickhouse_client

router = APIRouter()

FINDINGS_LIMIT = 50
SETTINGS_BLOCK = "insights_last"
FINDING_TYPES = ("ctr_drop", "cost_spike", "click_drop", "bot_surge",
                 "revenue_stop", "zero_conversion_spend")
SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

# last completed background run (surfaceable on /status later if wanted)
_loop_last_run = None


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------

def _load_main_settings(db: Session) -> dict:
    row = db.query(SettingsORM).filter_by(name="settings").first()
    if row and row.value:
        try:
            return json.loads(row.value) or {}
        except Exception:
            return {}
    return {}


def _save_block(db: Session, block: dict) -> None:
    """Upsert the insights_last block inside the settings row (FOR UPDATE)."""
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
        existing[SETTINGS_BLOCK] = block
        db.execute(text("UPDATE settings SET value = :v WHERE id = :i"),
                   {"v": json.dumps(existing), "i": row[0]})
    else:
        db.add(SettingsORM(name="settings", value=json.dumps({SETTINGS_BLOCK: block})))


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _clickhouse_windows(ch, campaign_id: int) -> dict:
    """One query per campaign: last-24h vs the 7 full days before it."""
    rows = ch.query(f"""
        SELECT
            countIf(received_at >= now() - toIntervalHour(24)) AS cur_clicks,
            countIf(received_at >= now() - toIntervalHour(24) AND click = true) AS cur_click_outs,
            countIf(received_at >= now() - toIntervalHour(24) AND is_bot = true) AS cur_bots,
            sumIf(toFloat64(cost), received_at >= now() - toIntervalHour(24)) AS cur_cost,
            sumIf(toFloat64(revenue), received_at >= now() - toIntervalHour(24)) AS cur_revenue,
            countIf(received_at <  now() - toIntervalHour(24)) AS base_clicks,
            countIf(received_at <  now() - toIntervalHour(24) AND click = true) AS base_click_outs,
            sumIf(toFloat64(cost), received_at < now() - toIntervalHour(24)) AS base_cost,
            sumIf(toFloat64(revenue), received_at < now() - toIntervalHour(24)) AS base_revenue
        FROM clicks_data
        WHERE campaign_id = %(cid)s AND tenant_id = %(tenant_id)s
          AND received_at >= now() - toIntervalDay(8)""",
        parameters={"cid": int(campaign_id), "tenant_id": current_tenant()}).result_rows
    row = rows[0] if rows else (0,) * 9
    return {
        "cur_clicks": int(row[0] or 0), "cur_click_outs": int(row[1] or 0),
        "cur_bots": int(row[2] or 0),
        "cur_cost": float(row[3] or 0), "cur_revenue": float(row[4] or 0),
        "base_clicks": int(row[5] or 0), "base_click_outs": int(row[6] or 0),
        "base_cost": float(row[7] or 0), "base_revenue": float(row[8] or 0),
    }


def _conversions_by_day(db: Session, campaign_id: int) -> dict:
    """PG conversions per calendar day, last 8 days (rejected/trash excluded).

    Returns {"YYYY-MM-DD": {"convs": n, "revenue": float}, ...} in server time."""
    rows = db.execute(text("""
        SELECT to_char(received_at, 'YYYY-MM-DD') AS day, count(*) AS convs,
               COALESCE(sum(revenue), 0) AS revenue
        FROM conversions_data
        WHERE tenant_id = :tid
          AND campaign_id = :cid
          AND received_at >= date_trunc('day', now()) - interval '7 days'
          AND status NOT IN ('rejected', 'trash')
        GROUP BY day"""), {"cid": int(campaign_id), "tid": current_tenant()}).fetchall()
    return {r[0]: {"convs": int(r[1] or 0), "revenue": float(r[2] or 0)}
            for r in rows}


def _monitor_disabled_flows(config: dict) -> list:
    out = []
    for flow in ((config or {}).get("flows") or []):
        if not isinstance(flow, dict):
            continue
        if flow.get("disabled_by_monitor") or flow.get("enabled") is False:
            out.append((flow.get("name") or "").strip() or
                       f"flow #{flow.get('position', '?')}")
    return out


def _detect(campaign_id: int, campaign_name: str, config: dict,
            ch_w: dict, conv_days: dict, today: str) -> list:
    findings = []

    def add(ftype, severity, message, current, baseline):
        change_pct = round((current - baseline) / baseline * 100, 2) if baseline else 0.0
        findings.append({
            "campaign_id": int(campaign_id), "campaign_name": campaign_name,
            "type": ftype, "severity": severity, "message": message,
            "detail": {"current": round(float(current), 4),
                       "baseline": round(float(baseline), 4),
                       "change_pct": change_pct},
            "detected_at": datetime.utcnow().isoformat(),
        })

    base_clicks_avg = ch_w["base_clicks"] / 7.0
    base_click_outs_avg = ch_w["base_click_outs"] / 7.0
    base_cost_avg = ch_w["base_cost"] / 7.0
    base_convs = sum(d["convs"] for day, d in conv_days.items() if day < today)
    base_convs_avg = base_convs / 7.0
    # revenue lives in PG conversions_data, not clicks_data — average it there
    base_revenue_avg = sum(d["revenue"] for day, d in conv_days.items()
                           if day < today) / 7.0

    # ctr_drop — conversions per click-out, both windows need ≥ 20 click-outs
    if ch_w["cur_click_outs"] >= 20 and ch_w["base_click_outs"] >= 20 * 7:
        cur_cr = conv_days.get(today, {}).get("convs", 0) / ch_w["cur_click_outs"]
        base_cr = base_convs / ch_w["base_click_outs"] if ch_w["base_click_outs"] else 0
        if base_cr > 0 and cur_cr < base_cr * 0.5:
            drop_pct = round((1 - cur_cr / base_cr) * 100, 1)
            add("ctr_drop", "critical" if drop_pct > 75 else "warning",
                f"Conversion rate fell {drop_pct}% vs the 7-day average",
                cur_cr * 100, base_cr * 100)

    # cost_spike — cost > 3x the 7-day daily average with ≥ $5 baseline
    if base_cost_avg >= 5 and ch_w["cur_cost"] > base_cost_avg * 3:
        add("cost_spike", "warning",
            f"Spend {round(ch_w['cur_cost'] / base_cost_avg, 1)}x the 7-day daily average",
            ch_w["cur_cost"], base_cost_avg)

    # click_drop — click-outs down > 60% vs the 7-day daily average
    if base_click_outs_avg >= 50 and ch_w["cur_click_outs"] < base_click_outs_avg * 0.4:
        disabled = _monitor_disabled_flows(config)
        message = "Click-outs dropped > 60% vs the 7-day average"
        if disabled:
            message += " — flows disabled by monitoring: " + ", ".join(disabled)
        add("click_drop", "warning", message,
            ch_w["cur_click_outs"], base_click_outs_avg)

    # bot_surge — bot share > 40% over ≥ 100 clicks
    if ch_w["cur_clicks"] >= 100 and ch_w["cur_bots"] / ch_w["cur_clicks"] > 0.4:
        share = round(ch_w["cur_bots"] / ch_w["cur_clicks"] * 100, 1)
        add("bot_surge", "warning",
            f"Bot share {share}% in the last 24h — review on the Fraud page",
            share, 0)

    # revenue_stop — revenue on 6 of the last 7 days but none today
    last7 = [(datetime.strptime(today, "%Y-%m-%d").fromordinal(
        datetime.strptime(today, "%Y-%m-%d").toordinal() - i)).strftime("%Y-%m-%d")
             for i in range(1, 8)]
    revenue_days = sum(1 for d in last7 if conv_days.get(d, {}).get("revenue", 0) > 0)
    today_revenue = conv_days.get(today, {}).get("revenue", 0)
    if revenue_days >= 6 and today_revenue == 0 and ch_w["cur_clicks"] >= 20:
        add("revenue_stop", "info",
            "Revenue stopped today after 6+ revenue days this week",
            0, base_revenue_avg)

    # zero_conversion_spend — > $50 spend, zero conversions, ≥ 100 clicks
    today_convs = conv_days.get(today, {}).get("convs", 0)
    if ch_w["cur_cost"] > 50 and today_convs == 0 and ch_w["cur_clicks"] >= 100:
        add("zero_conversion_spend", "warning",
            f"${round(ch_w['cur_cost'], 2)} spent with zero conversions in 24h",
            ch_w["cur_cost"], base_cost_avg)

    return findings


def _alert_new_criticals(prev_block: dict, findings: list) -> bool:
    """One Telegram message when critical findings are new vs the previous run."""
    try:
        from app_pages.monitor import send_telegram_alert
        prev_ids = set((prev_block or {}).get("critical_ids") or [])
        new_criticals = [f for f in findings
                         if f["severity"] == "critical"
                         and f"{f['type']}:{f['campaign_id']}" not in prev_ids]
        if not new_criticals:
            return False
        lines = "\n".join(
            f"• {_html.escape(str(f['campaign_name']))} (#{f['campaign_id']}): "
            f"{_html.escape(str(f['message']))}" for f in new_criticals[:10])
        msg = (f"🧠 <b>Insights: {len(new_criticals)} new critical "
               f"anomal{'y' if len(new_criticals) == 1 else 'ies'}</b>\n\n{lines}")
        # send_telegram_alert is a sync helper — we're already off the loop here
        return bool(send_telegram_alert(msg))
    except Exception as e:
        print("insights telegram alert:", e)
        return False


def run_analysis(caller: str = "insights") -> dict:
    """Full sweep over ACTIVE campaigns. Persists block "insights_last"."""
    global _loop_last_run
    db = SessionLocal()
    try:
        prev_block = _load_main_settings(db).get(SETTINGS_BLOCK) or {}
        campaigns = db.execute(text(
            "SELECT id, name, config FROM campaigns "
            "WHERE status = 'active' AND archived = false AND tenant_id = :tid "
            "ORDER BY id ASC"), {"tid": current_tenant()}).fetchall()

        ch = get_clickhouse_client()
        findings, analyzed, skipped = [], 0, 0
        today = datetime.utcnow().strftime("%Y-%m-%d")
        try:
            for cid, cname, config in campaigns:
                try:
                    ch_w = _clickhouse_windows(ch, cid)
                    conv_days = _conversions_by_day(db, cid)
                    # skip campaigns with essentially no current traffic
                    if ch_w["cur_clicks"] < 10 and ch_w["base_clicks"] < 10:
                        skipped += 1
                        continue
                    analyzed += 1
                    findings.extend(_detect(cid, cname, config or {}, ch_w,
                                            conv_days, today))
                except Exception as e:
                    skipped += 1
                    print(f"Insights: campaign {cid} error:", e)
        finally:
            ch.close()

        findings.sort(key=lambda f: (SEVERITY_RANK.get(f["severity"], 9),
                                     -abs(f["detail"]["change_pct"])))
        findings = findings[:FINDINGS_LIMIT]
        block = {
            "run_at": datetime.utcnow().isoformat(),
            "findings": findings,
            "campaigns_analyzed": analyzed,
            "campaigns_skipped": skipped,
            "critical_ids": [f"{f['type']}:{f['campaign_id']}" for f in findings
                             if f["severity"] == "critical"],
        }
        _save_block(db, block)
        db.commit()

        telegram_sent = False
        if caller != "manual_no_telegram":
            telegram_sent = _alert_new_criticals(prev_block, findings)
        block["telegram_sent"] = telegram_sent
        _loop_last_run = datetime.utcnow()
        return block
    finally:
        db.close()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/latest")
def latest(db: Session = Depends(get_db)):
    """Cached last run; computed on the fly when absent."""
    block = (_load_main_settings(db).get(SETTINGS_BLOCK) or {})
    if not block.get("run_at"):
        block = run_analysis("latest_endpoint")
    block.pop("critical_ids", None)
    return block


@router.post("/run")
async def run_now(request: Request):
    """Run the detectors immediately (Run now button)."""
    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)
    result = await asyncio.to_thread(run_analysis, caller or "api_token")
    audit_event(caller or "api_token", "insights_run", "insights", "",
                {"findings": len(result.get("findings") or []),
                 "critical": sum(1 for f in result.get("findings") or []
                                 if f["severity"] == "critical")},
                request.client.host if request.client else "")
    result.pop("critical_ids", None)
    return result
