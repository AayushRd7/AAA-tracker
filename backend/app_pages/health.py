"""In-app health centre — what the backend can honestly report about itself.

GET /api/health (admin-only 'settings' section, mounted in app.py) is the one
call the Health centre page makes. It answers, from the backend's own vantage
point:

* reachability + a small round-trip latency for the primary store and the
  analytics store (reported under neutral labels — the deployment's stack is not
  named to tenants);
* the row counts that matter (clicks, conversions, campaigns, users);
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

# How far back a source's last good postback is looked for. A source that has
# not delivered an accepted postback in this window is not usefully "healthy",
# and the bound keeps the join off the full history as the trail grows.
SOURCE_SUCCESS_WINDOW_DAYS = 30
# Most traffic sources shown on the health card; one row each, never N+1.
SOURCE_SUCCESS_LIMIT = 20

# A source that has not delivered an accepted postback for this long is called
# out on the incident console even while it is still inside the 30-day fetch
# window above — the window only bounds the query, not what "healthy" means.
SOURCE_STALE_DAYS = 7
# An OAuth token this close to expiry is a warning; once past it, an incident.
INTEGRATION_EXPIRY_WARN_DAYS = 7
# TLS grading, mirroring the domains page: <=7 days is critical, <=30 warning.
SSL_CRITICAL_DAYS = 7
SSL_WARNING_DAYS = 30
# DNS/HTTP are network calls, so only this many newest domains are probed per
# health read; the incident list must never turn a quick page load into a sweep.
DOMAIN_SCAN_LIMIT = 25
# A handful of failed sends is noise (the same threshold the attention banner
# uses); more than this is worth an incident row.
CAPI_FAILURE_INCIDENT_THRESHOLD = CAPI_FAILURE_ATTENTION

# The page where each background loop is configured or observed. "monitor" lives
# in Settings, insights shares the Fraud page's Insights tab.
LOOP_LINKS = {
    "monitor": "/backend/settings",
    "rules": "/backend/rules",
    "optimizer": "/backend/optimizer",
    "insights": "/backend/fraud",
    "meta_ads": "/backend/logs-costs",
}
LOOP_LABELS = {
    "monitor": "Monitor loop", "rules": "Auto rules loop",
    "optimizer": "Optimizer loop", "insights": "Insights loop",
    "meta_ads": "Meta cost sync",
}

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _incident(severity: str, area: str, subject: str, detail: str, link: str,
              since=None) -> dict:
    """One incident-console row. ``since`` is an ISO timestamp when the state
    began, or None when the check cannot date it (a certificate's age, say)."""
    return {"severity": severity, "area": area, "subject": subject,
            "detail": detail, "link": link, "since": since}


def _iso(dt):
    if dt is None:
        return None
    try:
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        return None


def _platform_label(platform: str) -> str:
    """The display name for a platform, imported lazily so health does not pull
    the whole integrations module (and its HTTP client) in at import time."""
    try:
        from app_pages.integrations import PLATFORMS
        return (PLATFORMS.get(platform) or {}).get("label") or str(platform).title()
    except Exception:
        return str(platform).title()


def _integration_incidents(db, tid: int) -> list:
    """Expired/expiring tokens, connections flagged in error, and platforms this
    deployment cannot connect at all. A failing read yields [] for this check."""
    out = []
    try:
        rows = db.execute(text(
            "SELECT platform, expires_at, updated_at, raw "
            "FROM integration_connections WHERE tenant_id = :tid"),
            {"tid": tid}).fetchall()
    except Exception:
        rows = []
    now = datetime.utcnow()
    for platform, expires_at, updated_at, raw in rows:
        label = _platform_label(platform)
        raw = raw if isinstance(raw, dict) else {}
        if expires_at is not None and expires_at < now:
            days = max((now - expires_at).days, 1)
            out.append(_incident(
                "critical", "integrations", label,
                f"The stored token expired {days} day(s) ago; reconnect the platform.",
                "/backend/integrations", _iso(expires_at)))
        elif expires_at is not None and (expires_at - now).days <= INTEGRATION_EXPIRY_WARN_DAYS:
            days = max((expires_at - now).days, 0)
            out.append(_incident(
                "warning", "integrations", label,
                f"The stored token expires in {days} day(s); reconnect it soon.",
                "/backend/integrations", None))
        # No producer writes this today, but a connection a previous job marked
        # failed should surface rather than sit silently in the raw document.
        if raw.get("status") == "error" or raw.get("error"):
            out.append(_incident(
                "warning", "integrations", label,
                "The connection is flagged in an error state; reconnect the platform.",
                "/backend/integrations", _iso(updated_at)))
    # Deployment-level, not workspace-level: app credentials are environment
    # variables, so an implemented platform with none set cannot be connected by
    # anyone here. Only implemented platforms count — the declared "coming soon"
    # ones are not an incident.
    try:
        from app_pages.integrations import PLATFORM_ORDER, PLATFORMS, platform_credentials
        for name in PLATFORM_ORDER:
            if PLATFORMS.get(name, {}).get("implemented") and not platform_credentials(name)["configured"]:
                out.append(_incident(
                    "warning", "integrations", PLATFORMS[name]["label"],
                    "Not configured by this deployment; an administrator must set its app credentials.",
                    "/backend/integrations", None))
    except Exception:
        pass
    return out


def _domain_incidents(db, tid: int) -> list:
    """TLS expiry (via the domains module's own grader), DNS state and the last
    HTTP reachability check. Bounded to the newest domains; any single domain
    that misbehaves contributes nothing rather than breaking the list."""
    try:
        rows = db.execute(text(
            "SELECT domain, status, updated_at FROM domains "
            "WHERE tenant_id = :tid ORDER BY id LIMIT :lim"),
            {"tid": tid, "lim": DOMAIN_SCAN_LIMIT}).fetchall()
    except Exception:
        return []
    if not rows:
        return []
    try:
        from app_pages.domains import _cert_expiry_days, resolve_domain, server_public_ip
        server_ip = server_public_ip()
    except Exception:
        _cert_expiry_days = None
        resolve_domain = None
        server_ip = None

    out = []
    for domain, status, updated_at in rows:
        days = None
        if _cert_expiry_days is not None:
            try:
                days = _cert_expiry_days(domain)
            except Exception:
                days = None
        if days is None:
            out.append(_incident(
                "info", "domains", domain,
                "Certificate state is unknown; no readable certificate was found.",
                "/backend/domains", None))
        elif days < 0:
            out.append(_incident(
                "critical", "domains", domain,
                f"The TLS certificate expired {abs(days)} day(s) ago.",
                "/backend/domains", None))
        elif days <= SSL_CRITICAL_DAYS:
            out.append(_incident(
                "critical", "domains", domain,
                f"The TLS certificate expires in {days} day(s).",
                "/backend/domains", None))
        elif days <= SSL_WARNING_DAYS:
            out.append(_incident(
                "warning", "domains", domain,
                f"The TLS certificate expires in {days} day(s).",
                "/backend/domains", None))

        # A pending domain is not expected to serve yet, so its DNS is not an
        # incident — only domains that should already be live are probed.
        if status != "pending" and resolve_domain is not None:
            try:
                addresses = resolve_domain(domain)
            except Exception:
                addresses = []
            if not addresses:
                out.append(_incident(
                    "warning", "domains", domain,
                    "The domain does not resolve in DNS.",
                    "/backend/domains", None))
            elif server_ip and server_ip not in addresses:
                # A proxy/CDN in front is legitimate, so this is only informational.
                out.append(_incident(
                    "info", "domains", domain,
                    "The domain resolves, but not to this server's address.",
                    "/backend/domains", None))

        if status == "error":
            out.append(_incident(
                "critical", "domains", domain,
                "The last reachability check for this domain failed.",
                "/backend/domains", _iso(updated_at)))
    return out


def _delivery_incidents(last_success_by_source, capi_failed_24h) -> list:
    """Outbound-send failures and sources whose last accepted postback is stale.
    Both inputs are already tenant-scoped and bounded by the payload builder."""
    out = []
    try:
        failed = int(capi_failed_24h or 0)
    except Exception:
        failed = 0
    if failed > CAPI_FAILURE_INCIDENT_THRESHOLD:
        out.append(_incident(
            "warning", "delivery", "Outbound conversions",
            f"{failed} outbound sends failed in the last 24 hours.",
            "/backend/capi-integrations", None))
    now = datetime.utcnow()
    for row in last_success_by_source or []:
        last = row.get("last_success")
        if not last:
            continue
        try:
            when = datetime.fromisoformat(str(last).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue
        age_days = (now - when).days
        if age_days > SOURCE_STALE_DAYS:
            out.append(_incident(
                "warning", "delivery", row.get("source") or "Unknown source",
                f"No accepted postback in {age_days} day(s).",
                "/backend/logs-postbacks", last))
    return out


def _loop_incidents(loops: dict) -> list:
    """Loops whose heartbeat is far older than their cadence. A loop that has
    simply not run since boot stays ``never_run``, not stale — no incident."""
    out = []
    for name, cadence, _stale_after in LOOPS:
        row = (loops or {}).get(name) or {}
        if not row.get("stale"):
            continue
        age = row.get("age_minutes")
        detail = (f"Has not run for {age:.0f} min (expected every {cadence} min)."
                  if isinstance(age, (int, float))
                  else "Has not run for far longer than its cadence.")
        out.append(_incident(
            "warning", "loops", LOOP_LABELS.get(name, name), detail,
            LOOP_LINKS.get(name, "/backend/health-center"), row.get("last_run")))
    return out


def build_incidents(db, tid: int, *, pg_ok: bool, ch_ok: bool, loops: dict,
                    last_success_by_source, capi_failed_24h) -> list:
    """The derived incident list for the health console, most severe first.

    Everything here comes from state the app already holds; no new table backs
    it. Each source is gathered behind its own try/except so one failing check
    degrades to [] for that check — the health payload is never broken by an
    incident probe. Nothing returned carries a credential, token or stack name.
    """
    incidents = []
    if not pg_ok:
        incidents.append(_incident(
            "critical", "system", "Main database", "The database is not answering.",
            "/backend/health-center", None))
    if not ch_ok:
        incidents.append(_incident(
            "critical", "system", "Analytics store",
            "The analytics store is not answering.", "/backend/health-center", None))
    incidents.extend(_integration_incidents(db, tid))
    incidents.extend(_domain_incidents(db, tid))
    incidents.extend(_delivery_incidents(last_success_by_source, capi_failed_24h))
    incidents.extend(_loop_incidents(loops))
    incidents.sort(key=lambda i: (_SEVERITY_ORDER.get(i["severity"], 9),
                                  i.get("area") or "", i.get("subject") or ""))
    return incidents


def _loop_last_runs() -> dict:
    from app_pages import insights, meta_ads, monitor, optimizer, rules
    return {
        "monitor": monitor._loop_last_run,
        "rules": rules._loop_last_run,
        "optimizer": optimizer._loop_last_run,
        "insights": insights._loop_last_run,
        "meta_ads": meta_ads._loop_last_run,
    }


def _last_success_by_source(db, tid: int) -> list:
    """Most recent accepted inbound postback for each traffic source.

    A postback row carries no source of its own, so attribution goes through its
    click's conversion: the denormalised ``traffic_source_name`` when the click
    had one, else the campaign's configured source. ``DISTINCT ON`` keeps it to
    one bounded query — no per-source round trip — and tenant scoping is applied
    on every leg of the join. Returns [] rather than failing the endpoint.
    """
    try:
        rows = db.execute(text(
            """
            SELECT source, click_id, last_success FROM (
                SELECT DISTINCT ON (source) source, click_id, last_success
                FROM (
                    SELECT COALESCE(NULLIF(c.traffic_source_name, ''), s.name) AS source,
                           p.click_id AS click_id,
                           p.received_at AS last_success
                    FROM postback_logs p
                    JOIN conversions_data c
                      ON c.click_id = p.click_id AND c.tenant_id = p.tenant_id
                    LEFT JOIN campaigns ca ON ca.id = c.campaign_id
                    LEFT JOIN sources s ON s.id = ca.traffic_source_id
                                        AND s.tenant_id = p.tenant_id
                    WHERE p.tenant_id = :tid
                      AND p.result = 'accepted'
                      AND p.received_at > now() - make_interval(days => :days)
                      AND COALESCE(NULLIF(c.traffic_source_name, ''), s.name) IS NOT NULL
                ) joined
                ORDER BY source, last_success DESC
            ) latest
            ORDER BY last_success DESC
            LIMIT :lim
            """), {"tid": tid, "days": SOURCE_SUCCESS_WINDOW_DAYS,
                   "lim": SOURCE_SUCCESS_LIMIT}).fetchall()
    except Exception:
        return []
    return [{
        "source": row[0],
        "click_id": row[1],
        "last_success": (row[2].replace(tzinfo=timezone.utc).isoformat()
                         if row[2] else None),
    } for row in rows]


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
        attention.append("The database is not answering")
    if not ch_ok:
        attention.append("The analytics store is not answering")
    for name, age_min in stale_loops:
        attention.append(f"{name} loop has not run for {age_min:.0f} min")
    if capi_failed_24h and capi_failed_24h > CAPI_FAILURE_ATTENTION:
        attention.append(f"{capi_failed_24h} outbound sends failed in the last 24h")

    # Derived incident console. Built last and behind its own guards so a slow
    # or failing probe can never take down the health report itself.
    last_success = _last_success_by_source(db, tid)
    incidents = build_incidents(
        db, tid, pg_ok=pg_ok, ch_ok=ch_ok, loops=loops,
        last_success_by_source=last_success, capi_failed_24h=capi_failed_24h)
    incident_counts = {"critical": 0, "warning": 0, "info": 0}
    for inc in incidents:
        if inc["severity"] in incident_counts:
            incident_counts[inc["severity"]] += 1

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
            "database": {"ok": pg_ok, "latency_ms": pg_ms},
            "analytics": {"ok": ch_ok, "latency_ms": ch_ms},
        },
        "counts": counts,
        "last_success_by_source": last_success,
        "loops": loops,
        "errors": {"capi_failed_24h": capi_failed_24h or 0},
        # Additive incident console: every key above keeps its exact shape.
        "incidents": incidents,
        "incident_counts": incident_counts,
    }
