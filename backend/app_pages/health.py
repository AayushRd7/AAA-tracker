"""In-app health centre — what the backend can honestly report about itself.

GET /api/health (admin-only 'settings' section, mounted in app.py) is the one
call the Health centre page makes. It answers, from the backend's own vantage
point:

* database reachability + a small round-trip latency for Postgres and
  ClickHouse;
* the row counts that matter (clicks, conversions, campaigns, users) plus the
  largest Postgres tables by live-tuple estimate;
* the last run of every background loop the backend owns — monitor, rules,
  optimizer, insights, Meta cost-sync — and whether any is stale;
* the process's version and uptime;
* a bounded count of recent failed outbound sends recorded in ``meta_capi_log``.

It deliberately does not claim to inspect containers: the backend has no
Docker API access (only the narrow nginx-reload socket proxy), so container
state is outside what this page can see.
"""
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from tenant_context import current_tenant
from version import __version__

router = APIRouter()

# Captured once when the backend imports this module — the closest thing the
# app has to a process start time.
PROCESS_START = time.time()

# Per-loop cadence and the age past which a loop is called stale. The four
# 15-minute loops share a threshold of three cycles; the Meta cost-sync loop
# can be configured daily, so it only goes stale after a day plus slack.
LOOPS = (
    ("monitor", 15, 45),
    ("rules", 15, 45),
    ("optimizer", 15, 45),
    ("insights", 15, 45),
    ("meta_ads", 60, 26 * 60),
)

# A handful of recent failed sends is noise; a burst is worth surfacing.
CAPI_FAILURE_ATTENTION = 10


def _loop_last_runs() -> dict:
    from app_pages import insights, meta_ads, monitor, optimizer, rules
    return {
        "monitor": monitor._loop_last_run,
        "rules": rules._loop_last_run,
        "optimizer": optimizer._loop_last_run,
        "insights": insights._loop_last_run,
        "meta_ads": meta_ads._loop_last_run,
    }


@router.get("")
def health_centre(request: Request, db: Session = Depends(get_db)):
    tid = current_tenant()
    now = datetime.utcnow()

    # Postgres reachability + latency
    pg_ok, pg_ms = False, None
    try:
        start = time.perf_counter()
        db.execute(text("SELECT 1"))
        pg_ms = round((time.perf_counter() - start) * 1000, 2)
        pg_ok = True
    except Exception:
        pass

    # ClickHouse reachability + latency
    ch_ok, ch_ms = False, None
    ch = None
    try:
        ch = request.state.ch
        start = time.perf_counter()
        ch.query("SELECT 1")
        ch_ms = round((time.perf_counter() - start) * 1000, 2)
        ch_ok = True
    except Exception:
        pass

    counts = {}
    if ch_ok:
        try:
            row = ch.query(
                "SELECT count() FROM clicks_data WHERE tenant_id = %(tid)s",
                parameters={"tid": tid}).result_rows
            counts["clicks"] = int(row[0][0]) if row else 0
        except Exception:
            counts["clicks"] = None

    def _pg_count(sql, params=None):
        try:
            return int(db.execute(text(sql), params or {}).scalar() or 0)
        except Exception:
            return None

    counts["conversions"] = _pg_count(
        "SELECT count(*) FROM conversions_data WHERE tenant_id = :tid", {"tid": tid})
    counts["campaigns"] = _pg_count(
        "SELECT count(*) FROM campaigns WHERE tenant_id = :tid", {"tid": tid})
    # users is install-global (no tenant column), like the auth tables.
    counts["users"] = _pg_count("SELECT count(*) FROM users")
    counts["postbacks"] = _pg_count(
        "SELECT count(*) FROM postback_logs WHERE tenant_id = :tid", {"tid": tid})
    counts["click_forwarding"] = _pg_count(
        "SELECT count(*) FROM click_forward_logs WHERE tenant_id = :tid", {"tid": tid})
    counts["capi_attempts"] = _pg_count(
        "SELECT count(*) FROM meta_capi_log WHERE tenant_id = :tid", {"tid": tid})

    # Largest Postgres tables by live-tuple estimate — cheap catalog read, no
    # per-table COUNT on big tables.
    largest = []
    if pg_ok:
        try:
            rows = db.execute(text(
                "SELECT relname, n_live_tup FROM pg_stat_user_tables "
                "ORDER BY n_live_tup DESC NULLS LAST LIMIT 8")).fetchall()
            largest = [{"table": r[0], "rows": int(r[1] or 0)} for r in rows]
        except Exception:
            largest = []

    # Loop heartbeats
    last_runs = _loop_last_runs()
    loops = {}
    stale_loops = []
    for name, cadence, stale_after in LOOPS:
        last = last_runs.get(name)
        age_min = None
        stale = False
        if last is not None:
            age_min = round((now - last).total_seconds() / 60, 1)
            stale = age_min > stale_after
            if stale:
                stale_loops.append((name, age_min))
        loops[name] = {
            "last_run": last.replace(tzinfo=timezone.utc).isoformat() if last else None,
            "age_minutes": age_min,
            "cadence_minutes": cadence,
            "stale": stale,
            "never_run": last is None,
        }

    # Recent failed outbound sends, bounded to the last 24h.
    capi_failed_24h = _pg_count(
        "SELECT count(*) FROM meta_capi_log "
        "WHERE tenant_id = :tid AND outcome = 'failed' "
        "AND COALESCE(created_at, at) > now() - interval '24 hours'",
        {"tid": tid})

    attention = []
    if not pg_ok:
        attention.append("Postgres is not answering")
    if not ch_ok:
        attention.append("ClickHouse is not answering")
    for name, age_min in stale_loops:
        attention.append(f"{name} loop has not run for {age_min:.0f} min")
    if capi_failed_24h and capi_failed_24h > CAPI_FAILURE_ATTENTION:
        attention.append(f"{capi_failed_24h} outbound sends failed in the last 24h")

    uptime = int(time.time() - PROCESS_START)
    return {
        "ok": not attention,
        "verdict": "attention" if attention else "ok",
        "attention": attention,
        "version": __version__,
        "uptime_seconds": uptime,
        "started_at": datetime.fromtimestamp(PROCESS_START, tz=timezone.utc).isoformat(),
        "checked_at": now.replace(tzinfo=timezone.utc).isoformat(),
        "databases": {
            "postgres": {"ok": pg_ok, "latency_ms": pg_ms},
            "clickhouse": {"ok": ch_ok, "latency_ms": ch_ms},
        },
        "counts": counts,
        "largest_tables": largest,
        "loops": loops,
        "errors": {"capi_failed_24h": capi_failed_24h or 0},
    }
