"""Prometheus-style operational metrics.

`GET /api/metrics` renders the process uptime, the Postgres pool saturation, the
last-run age of each background loop and per-tenant usage counts (24h clicks and
conversions, plus row totals) in the text exposition format, so an external
scraper can alert on a stalled loop, a saturated pool or a runaway tenant. It is
admin-gated like the status/health endpoints and never names the deployment's
storage engine to tenants.
"""
import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import engine, get_db

router = APIRouter()

_STARTED = time.time()


def _prom(lines, name, value, labels=None):
    if labels:
        tag = ",".join(f'{k}="{v}"' for k, v in labels.items())
        lines.append(f"{name}{{{tag}}} {value}")
    else:
        lines.append(f"{name} {value}")


def _pool_metrics(lines):
    try:
        pool = engine.pool
        _prom(lines, "aaa_db_pool_size", pool.size())
        _prom(lines, "aaa_db_pool_checked_in", pool.checkedin())
        _prom(lines, "aaa_db_pool_checked_out", pool.checkedout())
        # overflow() is checked_out - size and goes negative when idle; clamp it.
        overflow = max(0, getattr(pool, "overflow", lambda: 0)())
        _prom(lines, "aaa_db_pool_overflow", overflow)
    except Exception:
        pass


def _loop_metrics(lines):
    try:
        from app_pages.health import _loop_last_runs
        now = time.time()
        for name, last in _loop_last_runs().items():
            if last is None:
                continue
            age = max(0.0, now - last.replace(tzinfo=None).timestamp()) \
                if last.tzinfo is None else max(0.0, now - last.timestamp())
            _prom(lines, "aaa_loop_last_run_age_seconds", round(age, 1), {"loop": name})
    except Exception:
        pass


def _tenant_metrics(lines, db: Session, request: Request):
    # Row totals per tenant (PG).
    for metric, table in (("aaa_campaigns_total", "campaigns"),
                          ("aaa_conversions_total", "conversions_data"),
                          ("aaa_users_total", "tenant_memberships")):
        try:
            rows = db.execute(text(
                f"SELECT tenant_id, count(*) FROM {table} GROUP BY tenant_id")).fetchall()
            for tenant_id, count in rows:
                _prom(lines, metric, int(count), {"tenant": int(tenant_id)})
        except Exception:
            pass
    # 24h traffic per tenant (analytics store).
    try:
        ch = request.state.ch
        res = ch.query(
            "SELECT tenant_id, countIf(click = true), "
            "       countIf(status IN ('sale', 'upsale')) "
            "FROM clicks_data WHERE received_at >= now() - INTERVAL 24 HOUR "
            "GROUP BY tenant_id")
        for tenant_id, clicks, convs in res.result_rows:
            _prom(lines, "aaa_clicks_24h", int(clicks), {"tenant": int(tenant_id)})
            _prom(lines, "aaa_conversions_24h", int(convs), {"tenant": int(tenant_id)})
    except Exception:
        pass


@router.get("")
def metrics(request: Request, db: Session = Depends(get_db)):
    lines = []
    _prom(lines, "aaa_up", 1)
    _prom(lines, "aaa_uptime_seconds", round(time.time() - _STARTED, 1))
    _pool_metrics(lines)
    _loop_metrics(lines)
    _tenant_metrics(lines, db, request)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines) + "\n",
                             media_type="text/plain; version=0.0.4")
