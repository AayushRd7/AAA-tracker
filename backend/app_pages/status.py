"""G78 — in-UI system status.

GET /api/status (admin-only 'settings' section, mounted in app.py) reports:
app version, Postgres and ClickHouse health (SELECT 1 + latency), the last
run of each background loop (monitor / auto-rules / optimizer), 24h click
counts from ClickHouse, 24h conversions from Postgres, and — when the
landings volume is visible to this container — its disk usage.
"""
import os
import shutil
import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from version import __version__

router = APIRouter()

# The landings volume is only mounted into the frontend/nginx containers; the
# backend sees it only when the deployment happens to share the filesystem.
LANDINGS_DIR = os.environ.get("LANDINGS_DIR", "/app/landings")


@router.get("")
def system_status(request: Request, db: Session = Depends(get_db)):
    # Postgres latency
    pg_ok, pg_ms = False, None
    try:
        start = time.perf_counter()
        db.execute(text("SELECT 1"))
        pg_ms = round((time.perf_counter() - start) * 1000, 2)
        pg_ok = True
    except Exception:
        pass

    # ClickHouse latency (per-request client from the middleware)
    ch_ok, ch_ms, clicks_24h = False, None, 0
    try:
        ch = request.state.ch
        start = time.perf_counter()
        ch.query("SELECT 1")
        ch_ms = round((time.perf_counter() - start) * 1000, 2)
        ch_ok = True
        row = ch.query(
            "SELECT count() AS n, countIf(click = true) AS clicks "
            "FROM clicks_data WHERE received_at >= now() - toIntervalHour(24)"
        ).result_rows
        if row:
            clicks_24h = int(row[0][1] or 0)
    except Exception:
        pass

    try:
        conversions_24h = int(db.execute(text(
            "SELECT count(*) FROM conversions_data "
            "WHERE received_at > now() - interval '24 hours'")).scalar() or 0)
    except Exception:
        conversions_24h = 0

    from app_pages import monitor, rules, optimizer
    loops = {
        "monitor": monitor._loop_last_run.isoformat() if monitor._loop_last_run else None,
        "rules": rules._loop_last_run.isoformat() if rules._loop_last_run else None,
        "optimizer": (optimizer._loop_last_run.isoformat()
                      if optimizer._loop_last_run else None),
    }

    status = {
        "version": __version__,
        "postgres": {"ok": pg_ok, "latency_ms": pg_ms},
        "clickhouse": {"ok": ch_ok, "latency_ms": ch_ms},
        "loops": loops,
        "clicks_24h": clicks_24h,
        "conversions_24h": conversions_24h,
    }
    if os.path.isdir(LANDINGS_DIR):
        try:
            usage = shutil.disk_usage(LANDINGS_DIR)
            status["landings_disk"] = {
                "total_gb": round(usage.total / (1024 ** 3), 2),
                "used_gb": round(usage.used / (1024 ** 3), 2),
                "free_gb": round(usage.free / (1024 ** 3), 2),
                "path": LANDINGS_DIR,
            }
        except Exception:
            pass
    return status
