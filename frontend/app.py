import random
import base64
import hashlib
import socket
import hmac
import ipaddress
import secrets
import time
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, HTTPException, Response, BackgroundTasks
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from fastapi.encoders import jsonable_encoder
import httpagentparser
from datetime import datetime
import asyncpg
from types import SimpleNamespace
import json
import math
from fastapi.middleware.cors import CORSMiddleware
from user_agents import parse as parse_ua
import re
import os
import requests
import httpx
import psycopg2

import asyncio
import queue
from collections import deque

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from pathlib import Path

from clickhouse_connect import get_client
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

import uuid

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "tracker_postgres")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "db")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "user")
# dev-only fallback so imports work without env; the real value comes from .env
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD") or "_".join(["password"] * 3)

CLICKHOUSE_HOST = os.environ.get("CLICKHOUSE_HOST", "tracker_clickhouse")
CLICKHOUSE_PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_USER = os.environ.get("CLICKHOUSE_USER", "user")
# dev-only fallback so imports work without env; the real value comes from .env
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD") or "_".join(["password"] * 3)
CLICKHOUSE_DB = os.environ.get("CLICKHOUSE_DB", "default")


def pg_connect():
    return psycopg2.connect(
        host=POSTGRES_HOST, port=POSTGRES_PORT, dbname=POSTGRES_DB,
        user=POSTGRES_USER, password=POSTGRES_PASSWORD)


app = FastAPI()
app.state = SimpleNamespace()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ClickHouse client pool — the tracking plane runs inserts via asyncio.to_thread,
# so one shared client would hit clickhouse-connect's concurrent-queries-per-session
# guard under load. A small pool hands each thread its own client/session.
CH_POOL_SIZE = 8


def new_ch_client():
    return get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=CLICKHOUSE_DB
    )


def acquire_ch():
    """Borrow a pooled client, or mint a fresh one immediately when the pool is
    exhausted — never block the event loop waiting for a slot."""
    try:
        return app.state.ch_pool.get_nowait()
    except queue.Empty:
        return new_ch_client()


def release_ch(ch, failed: bool = False):
    if failed:
        # A client that just errored may hold a bad connection — swap in a fresh one.
        try:
            ch.close()
        except Exception:
            pass
        ch = new_ch_client()
    try:
        app.state.ch_pool.put_nowait(ch)
    except queue.Full:
        # Pool already full (overflow clients) — drop the extra so the pool
        # can never grow unboundedly.
        try:
            ch.close()
        except Exception:
            pass


# 🚀 Startup
@app.on_event("startup")
async def startup():
    app.state.pg = await asyncpg.create_pool(
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        database=POSTGRES_DB,
        host=POSTGRES_HOST,
        port=int(POSTGRES_PORT)
    )
    app.state.ch_pool = queue.Queue()
    for _ in range(CH_POOL_SIZE):
        app.state.ch_pool.put(new_ch_client())
    # Idempotent schema migration (safe to run on every boot)
    await ensure_schema()
    await ensure_ch_schema()

    # Daily data-retention prune loop
    app.state.retention_task = asyncio.create_task(retention_loop())


async def ensure_schema():
    try:
        async with app.state.pg.acquire() as conn:
            await conn.execute("""
                ALTER TABLE conversions_data
                ADD COLUMN IF NOT EXISTS postback_count INTEGER DEFAULT 0,
                ADD COLUMN IF NOT EXISTS last_postback_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS flow_index INTEGER,
                ADD COLUMN IF NOT EXISTS funnel_step INTEGER,
                ADD COLUMN IF NOT EXISTS events JSONB NOT NULL DEFAULT '[]'::jsonb
            """)
            # Custom conversion statuses need values beyond the built-in enum
            await conn.execute(
                "ALTER TABLE conversions_data ALTER COLUMN status TYPE varchar(50)")
            await conn.execute("""
                ALTER TABLE offers
                ADD COLUMN IF NOT EXISTS daily_conversions_cap INTEGER,
                ADD COLUMN IF NOT EXISTS overflow_offer_id INTEGER
            """)
            # G33 honeypot decoy hits — visitors that follow the hidden /t/hp
            # link are scrapers/bots and get flagged on their next visit.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS honeypot_hits (
                    id serial PRIMARY KEY,
                    visitor_key text NOT NULL,
                    ip text,
                    ua text,
                    campaign_id integer,
                    received_at timestamp NOT NULL DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS honeypot_hits_visitor_key_idx
                ON honeypot_hits (visitor_key)
            """)
    except Exception as e:
        log_track(f"Schema migration error: {e}")


async def ensure_ch_schema():
    """Idempotent ClickHouse schema migration (safe to run on every boot).

    Fresh installs get every column from install/sql/clickHouse.sql, but the
    live table predates some of them — ALTER ... ADD COLUMN IF NOT EXISTS
    brings older databases up to date (fraud scoring needs fraud_score).
    """
    ch = acquire_ch()
    try:
        await asyncio.to_thread(
            ch.command,
            "ALTER TABLE clicks_data "
            "ADD COLUMN IF NOT EXISTS flow_index UInt8 DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS utm_medium String DEFAULT '', "
            "ADD COLUMN IF NOT EXISTS impression UInt8 DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS fraud_score UInt8 DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS click_id String DEFAULT '', "
            # Full client address (v4 or v6) — the `ip` column is IPv4-typed,
            # so IPv6 visitors were stored as 0.0.0.0 with no recoverable address.
            "ADD COLUMN IF NOT EXISTS ip_full String DEFAULT ''")
    except Exception as e:
        release_ch(ch, failed=True)
        log_track(f"ClickHouse schema migration error: {e}")
        return
    release_ch(ch)


async def retention_loop():
    """Prune ClickHouse data older than the configured retention window, daily."""
    while True:
        try:
            await prune_old_data()
        except Exception as e:
            log_track(f"Retention prune error: {e}")
        await asyncio.sleep(24 * 3600)


async def prune_old_data():
    """Delete clicks older than settings.data_retention.days when enabled."""
    conn = pg_connect()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE name = 'settings'")
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return
    cfg = json.loads(row[0]).get("data_retention") or {}
    if not cfg.get("enabled"):
        return
    try:
        days = int(cfg.get("days") or 0)
    except (TypeError, ValueError):
        return
    if days <= 0:
        return
    ch = acquire_ch()
    try:
        await asyncio.to_thread(
            ch.command,
            f"ALTER TABLE clicks_data DELETE WHERE received_at < now() - INTERVAL {days} DAY")
    except Exception:
        release_ch(ch, failed=True)
        raise
    release_ch(ch)
    log_track(f"🧹 Retention: pruned clicks_data older than {days} days")


# 🛑 Shutdown
@app.on_event("shutdown")
async def shutdown():
    await app.state.pg.close()
    while True:
        try:
            ch = app.state.ch_pool.get_nowait()
        except queue.Empty:
            break
        try:
            ch.close()
        except Exception:
            pass


VALID_PARAMS = [
    'ad_campaign_id', 'browser', 'campaign_id', 'city', 'click_id', 'connection_type', 'currency',
    'cost', 'country', 'utm_creative', 'utm_campaign', 'utm_medium', 'utm_source', 'device_type', 'external_id', 'ip',
    'impression', 'is_bot', 'is_using_proxy', 'isp', 'keyword', 'landing_id', 'language', 'offer_id',
    'os', 'os_version', 'profit', 'referrer', 'region', 'revenue', 'status', 'sub_id_1', 'sub_id_2', 'sub_id_3',
    'sub_id_4', 'sub_id_5', 'sub_id_6', 'sub_id_7', 'sub_id_8', 'sub_id_9', 'sub_id_10',
    'traffic_source_name', 'url', 'user_agent', 'visitor_id'
]

from landings import router as landings_router
from domains import router as domains_router

# Include the router
app.include_router(landings_router, tags=["Landings"])
app.include_router(domains_router, tags=["Domains"])

##### main tracker app #####


# in-memory log
TRACK_LOG = []

_title_cache = {}


def log_track(message: str):
    TRACK_LOG.append(message)
    if len(TRACK_LOG) > 50:
        TRACK_LOG.pop(0)


def resolve_client_ip(request: Request) -> str:
    """Trusted client IP for tracking decisions.

    X-Real-IP wins — nginx overwrites it with the connection's $remote_addr, so
    a client cannot forge it. X-Forwarded-For is only a fallback for direct
    (nginx-bypassed) uvicorn traffic; the nginx configs rewrite it to
    $remote_addr anyway, and garbage values are rejected. Falls back to the
    direct peer last.
    """
    for candidate in [
        request.headers.get("x-real-ip") or "",
        (request.headers.get("x-forwarded-for") or "").split(",")[0].strip(),
        request.client.host if request.client else "",
    ]:
        if not candidate:
            continue
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            continue
    return ""


async def enrich_meta(request: Request, params_id_mapping: list = None) -> dict:
    ua_string = request.headers.get('user-agent', '') or ''
    parsed = httpagentparser.detect(ua_string)
    ua = parse_ua(ua_string)

    language = request.headers.get('accept-language', '')
    country_code = None
    if language:
        match = re.search(r'-([A-Z]{2})', language)
        if match:
            country_code = match.group(1)

    # Store only the primary language subtag so equality filters match
    # ("en-US,en;q=0.9" -> "en", "FR-fr" -> "fr").
    language = language.split(',')[0].split('-')[0].strip().lower()

    # GET parameters
    query_params = dict(request.query_params)

    # POST parameters (cached on request.state so repeated enrich calls within
    # one request don't try to read the already-consumed body again)
    post_data = getattr(request.state, "_parsed_body", None)
    if post_data is None:
        try:
            content_type = request.headers.get("content-type", "")
            if "application/json" in content_type:
                post_data = await request.json()
            elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
                post_data = dict(await request.form())
            else:
                post_data = {}
        except Exception:
            post_data = {}
        # A JSON scalar/array body ([1,2], 42, "x") is not mergeable — normalize
        # to {} at cache time so {**post_data} merges and `key in query` checks
        # downstream can never raise TypeError on the cached body.
        if not isinstance(post_data, dict):
            post_data = {}
        request.state._parsed_body = post_data

    # Real client IP: X-Real-IP (set by nginx, unforgeable) first, then the
    # first X-Forwarded-For entry only when X-Real-IP is absent, then the
    # direct peer. Invalid values are ignored so a garbage header can never
    # poison the stored ip.
    client_ip = resolve_client_ip(request)

    # Cookies
    cookies = request.cookies

    # Merge everything into one flat dictionary
    meta = {
        "received_at": datetime.utcnow().isoformat(),
        "ip": client_ip,
        "referrer": request.headers.get("referer"),
        "current_domain": request.headers.get("host"),
        "language": language,
        "country": country_code,
        "browser": parsed.get("browser", {}).get("name"),
        "browser_version": parsed.get("browser", {}).get("version") or ua.browser.version_string,
        "os": parsed.get("os", {}).get("name"),
        "os_version": ua.os.version_string,
        "device_type": "mobile" if "Mobile" in ua_string else "desktop",
        "device_brand": ua.device.brand,
        "device_model": ua.device.model,
        "is_bot": ua.is_bot or "bot" in ua_string.lower(),
        # Reverse DNS (PTR) of the client IP — resolved off-loop with a 2s
        # ceiling and cached; empty on failure/timeout.
        "rdns": await reverse_dns(client_ip),
        "user_agent": ua_string,
    }

    # The tracking gate (bot rules / source-declared bot / honeypot) runs before
    # enrichment on the redirect path — surface its mark so flow filters on
    # is_bot and the ClickHouse row see the same decision.
    if getattr(request.state, "bot_marked", None):
        meta["is_bot"] = True

    combined = {**query_params, **post_data, **cookies}

    # Add query, post and cookie parameters directly
    for k, v in combined.items():
        if k not in meta:  # don't overwrite the base keys
            meta[k] = v

    if params_id_mapping:
        for param in params_id_mapping:
            param_key = param.get("parameter")  # e.g.: sub_id_2
            token_key = param.get("token", "").strip()  # e.g.: var_in

            if not param_key:
                continue

            # If a token exists and was passed in the request
            if token_key and token_key in combined:
                value = combined[token_key]
                meta[token_key] = value
                meta[param_key] = value
            # If there is no token — try the parameter directly
            elif param_key in combined:
                meta[param_key] = combined[param_key]

        # Networks pass their click id under a paramsIdMapping token bound to
        # the 'click' parameter (e.g. clickid → click). Bridge it to click_id
        # so attribution and source postbacks flow — unless the request
        # already carried an explicit click_id.
        if not meta.get("click_id"):
            for param in params_id_mapping:
                if param.get("parameter") != "click":
                    continue
                token = (param.get("token") or "").strip()
                value = combined.get(token) if token else None
                if value:
                    meta["click_id"] = value
                    break

    return meta


async def show_landing(folder: str, offer_url: str = None) -> Response:
    index_php = os.path.join("landings", folder, "index.php")
    index_html = os.path.join("landings", folder, "index.html")

    if os.path.exists(index_php) or os.path.exists(index_html):
        # plain http on the internal docker network — no TLS/cert needed;
        # follow nginx's index redirect (e.g. /l/<folder> → /l/<folder>/)
        url = f"http://tracker_nginx/l/{folder}"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, follow_redirects=True)
        html = r.text
        if offer_url:
            html = html.replace("{offer}", offer_url)
        return Response(content=html, media_type="text/html")
    else:
        return Response(content="404 Not Found", status_code=404, media_type="text/html")


def do_redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=302)


async def get_default_campaign_from_db(domain: str):
    query = """
            select *
            from campaigns
            where id in (SELECT default_campaign_id
                         FROM domains
                         WHERE domain = $1
                         LIMIT 1); \
            """
    async with app.state.pg.acquire() as conn:
        row = await conn.fetchrow(query, domain)
        if row:
            return row
        return None


def render_404_html() -> Response:
    path = Path("static/404.html")
    if path.exists():
        html = path.read_text(encoding="utf-8")
    else:
        html = "<h1>404 Not Found</h1>"
    return Response(content=html, status_code=404, media_type="text/html")


def generate_click_id():
    return str(uuid.uuid4())


async def save_click_to_clickhouse(meta: dict, campaign_alias: str):
    """Insert the click-out row (click=true) into ClickHouse.

    Dedicated path — track_event would re-enrich the request and mislabel the
    row. No cost (cost belongs to the visit row) and no status/revenue/profit
    (server-owned fields, never request-derived). is_bot/fraud_score come from
    the tracking gate (server-side), not the request. A CH failure must never
    break the visitor's redirect.
    """
    try:
        row = {
            "received_at": datetime.utcnow(),
            "campaign_id": str(meta["campaign_id"]),
            "offer_id": meta.get("offer_id"),
            "click_id": str(meta.get("click_id") or ""),
            "click": True,
            "visitor_id": str(meta.get("visitor_id") or ""),
            "flow_index": int(meta.get("flow_index") or 0),
        }
        _CLICK_ROW_EXCLUDED = ("cost", "is_bot", "fraud_score", "status", "revenue", "profit")
        for k, v in meta.items():
            if k in VALID_PARAMS and k not in _CLICK_ROW_EXCLUDED and v is not None:
                row[k] = v
        # Gate result — is_bot from the bot rules/shield mark, fraud_score from
        # the same heuristic track_event applies.
        row["is_bot"] = bool(meta.get("is_bot"))
        try:
            row["fraud_score"] = min(int(meta.get("fraud_score") or 0), 100)
        except (TypeError, ValueError):
            row["fraud_score"] = 0
        if row.get("landing_id") is not None:
            row["landing_id"] = str(row["landing_id"])
        # G79 privacy: same IP masking as track_event (IPv4 → last octet zeroed,
        # IPv6 → last 16 bits zeroed) — the click-out row must not leak what the
        # visit row masked.
        if load_privacy_settings().get("anonymize_ip"):
            ip_val = row.get("ip")
            if ip_val:
                try:
                    addr = ipaddress.ip_address(str(ip_val))
                    if addr.version == 4:
                        row["ip"] = ".".join(str(addr).split(".")[:3] + ["0"])
                    else:
                        row["ip"] = str(ipaddress.IPv6Address((int(addr) >> 16) << 16))
                except ValueError:
                    pass
        # Full client address (v4 or v6) for the ip_full String column —
        # captured BEFORE the IPv4 coercion below, which erases v6 visitors.
        ip_full_val = row.get("ip")
        if ip_full_val is not None:
            try:
                row["ip_full"] = str(ipaddress.ip_address(str(ip_full_val)))
            except ValueError:
                row["ip_full"] = ""
        # IPv4 column — mirror track_event's guard: a garbage IP would fail the
        # whole insert, so store the type default instead of dropping the row.
        ip_val = row.get("ip")
        if ip_val is not None:
            try:
                if ipaddress.ip_address(str(ip_val)).version != 4:
                    row["ip"] = "0.0.0.0"
            except ValueError:
                row["ip"] = "0.0.0.0"
        row = {k: v for k, v in row.items() if v is not None}
        columns = list(row.keys())
        values = [list(row.values())]
        ch = acquire_ch()
        try:
            await asyncio.to_thread(ch.insert, "clicks_data", values, column_names=columns)
        except Exception:
            release_ch(ch, failed=True)
            raise
        release_ch(ch)
    except Exception as e:
        log_track(f"❌ ClickHouse click-row insert failed for '{campaign_alias}': {e}")


@app.get("/c/{campaign_alias}/{offer_id}")
async def campaign_click(
        campaign_alias: str,
        offer_id: str,
        request: Request,
        background_tasks: BackgroundTasks
) -> Response:
    log_track(f"🔁 New campaign click request {campaign_alias} - {offer_id}")
    pg = request.app.state.pg

    async with pg.acquire() as conn:
        # 1. Campaign
        campaign = await conn.fetchrow("SELECT * FROM campaigns WHERE alias = $1", campaign_alias)
        if not campaign:
            log_track(f"❌ CAMPAIGN NOT FOUND request {campaign_alias} - {offer_id}")
            return Response("Campaign not found", status_code=404)

        # 2. Offer
        try:
            offer_id_int = int(offer_id)
        except (TypeError, ValueError):
            return Response("Invalid offer id", status_code=400)
        offer = await conn.fetchrow("SELECT * FROM offers WHERE id = $1", offer_id_int)
        if not offer:
            return Response("Offer not found", status_code=404)

    # Paused/archived offers must never receive traffic.
    if offer["status"] != "active" or offer.get("archived"):
        log_track(f"🚫 Click-out refused for non-active offer {offer['id']}")
        return Response("Offer unavailable", status_code=404)

    # Paused/archived campaigns must not accept click-outs either.
    if campaign["status"] != "active":
        log_track(f"🚫 Click-out refused for non-active campaign '{campaign_alias}'")
        return Response("Campaign not found", status_code=404)

    # Bot rules + shield + blacklists + honeypot + GDPR opt-out — the same
    # shared gate every campaign-serving route applies. Blocked → untracked
    # 404/blank; opted-out → the redirect still happens but nothing is stored.
    blocked = await apply_tracking_gate(request, f"{campaign_alias}/{offer_id}", campaign)
    if blocked is not None:
        return blocked
    optout = getattr(request.state, "optout", False)

    # G4: daily conversion cap on the click-out route — transparently serve the
    # overflow offer when the primary is capped, otherwise refuse.
    cap_state_cache: dict = {}
    conv_count_cache: dict = {}
    daily_cap, overflow_id = await offer_cap_state(pg, cap_state_cache, offer["id"])
    if daily_cap and await offer_conversions_today(pg, conv_count_cache, offer["id"]) >= int(daily_cap):
        overflow = None
        if overflow_id:
            o_cap, _ = await offer_cap_state(pg, cap_state_cache, overflow_id)
            o_used = await offer_conversions_today(pg, conv_count_cache, overflow_id) if o_cap else 0
            if not o_cap or o_used < int(o_cap):
                async with pg.acquire() as conn:
                    overflow = await conn.fetchrow("SELECT * FROM offers WHERE id = $1", int(overflow_id))
        if overflow is None:
            return Response("Offer unavailable", status_code=404)
        offer = overflow

    # 4. Enrich the meta data via paramsIdMapping
    paramsIdMapping = get_params_id_mapping_from_campaign(campaign)
    meta_data = await enrich_meta(request, paramsIdMapping)

    # Required fields
    meta_data["campaign_id"] = campaign["id"]
    meta_data["offer_id"] = offer["id"]
    landing_id = request.query_params.get("l_id")
    if landing_id:
        try:
            meta_data["landing_id"] = int(landing_id)
        except ValueError:
            pass
    # Explicit ?click_id= from the landing click-out wins over the (possibly
    # stale) aaa_cid cookie — the cookie otherwise overrides it during the
    # meta merge and conversions misattribute. Mirrors /p's precedence.
    meta_data["click_id"] = (request.query_params.get("click_id")
                             or meta_data.get("click_id") or generate_click_id())

    # G55: link the conversion to the visitor cookie set on the campaign hit —
    # the conversions log joins click-date windows through this key.
    if not meta_data.get("visitor_id"):
        vid = request.cookies.get(VISITOR_COOKIE)
        if vid:
            meta_data["visitor_id"] = vid

    # Bot-mark / honeypot flag from the gate + the same fraud heuristic
    # track_event applies — the click-out row stops looking like human traffic.
    if getattr(request.state, "bot_marked", None) or getattr(request.state, "honeypot_flagged", False):
        meta_data["is_bot"] = True
    gate_score, _crawler, _reasons = compute_fraud_score(
        visitor_key=visitor_key_for(request),
        ip=str(meta_data.get("ip") or ""),
        ua=str(meta_data.get("user_agent") or ""),
        isp=str(meta_data.get("isp") or ""),
        is_bot=bool(meta_data.get("is_bot")) or bool(getattr(request.state, "bot_marked", None)))
    if getattr(request.state, "honeypot_flagged", False):
        gate_score = 100
    meta_data["fraud_score"] = min(int(gate_score), 100)

    # Per-flow delivery action — the flow is recovered from the signed
    # stickiness cookie's fi; a missing/invalid cookie falls back to the
    # legacy redirect. Resolved BEFORE the click row is queued so the
    # conversion inherits flow_index (same sorted-flow index the tracking
    # plane stamps on the ClickHouse click row).
    # G10 funnels: funnel campaigns ignore config.flows — the bound step
    # (cookie "st") is stamped on the conversion as flow_index + funnel_step
    # (for funnel campaigns the ClickHouse flow_index IS the step), and a
    # successful click-out advances the cookie to the next step.
    action_flow = None
    funnel_cfg = None
    click_bound = None
    click_cfg = {}
    click_dmode = "position"
    try:
        click_cfg = json.loads(campaign["config"] or "{}")
        click_dmode = (campaign.get("redirect_mode") if hasattr(campaign, "get")
                       else campaign["redirect_mode"]) or "position"
        funnel_cfg = click_cfg.get("funnel")
        if not (isinstance(funnel_cfg, dict) and funnel_cfg.get("enabled")
                and funnel_cfg.get("steps")):
            funnel_cfg = None
        click_bound = parse_bind_cookie(
            request, campaign["id"], campaign_routing_hash(click_cfg, click_dmode))
        if funnel_cfg is not None:
            st = click_bound.get("st") if click_bound else None
            if not (isinstance(st, int) and not isinstance(st, bool)
                    and 0 <= st < len(funnel_cfg.get("steps") or [])):
                st = 0
            meta_data["flow_index"] = st
            meta_data["funnel_step"] = st
        elif click_bound is not None:
            click_flows = sorted(
                click_cfg.get("flows", []),
                key=lambda f: (0 if f.get("type") == "forced" else 1,
                               f.get("position", 9999)))
            fi = click_bound.get("fi")
            if isinstance(fi, int) and not isinstance(fi, bool) and 0 <= fi < len(click_flows):
                action_flow = click_flows[fi]
                meta_data["flow_index"] = fi
    except Exception as e:
        log_track(f"❌ click-out action resolve failed for '{campaign_alias}': {e}")
        action_flow = None
        funnel_cfg = None

    # Prefetch/prerender hits (Purpose/Sec-Purpose: prefetch) are still served
    # the redirect but must not inflate click-out stats.
    prefetch = request_is_prefetch(request)

    # 5. Save the click asynchronously — opted-out visitors (G79) and prefetch
    # hits get the redirect but no Postgres/ClickHouse rows and no tracking cookies.
    if not optout and not prefetch:
        background_tasks.add_task(save_click_to_db, meta_data)

    # 6. Build the final URL
    offer_url = offer["url"]
    for key, value in meta_data.items():
        placeholder = f"{{{key}}}"
        if placeholder in offer_url:
            offer_url = offer_url.replace(placeholder, str(value))
    # G87 extra offer-URL macros: {_md5} (md5 of the click id) and {payout}
    # (this offer's payout).
    if "{_md5}" in offer_url:
        offer_url = offer_url.replace(
            "{_md5}", hashlib.md5(str(meta_data["click_id"]).encode()).hexdigest())
    if "{payout}" in offer_url:
        offer_url = offer_url.replace(
            "{payout}", str(offer["payout"] if offer["payout"] is not None else ""))

    # 2. Always append click_id as ?click_id=...
    parsed = urlparse(offer_url)
    query_params = dict(parse_qsl(parsed.query))
    query_params["click_id"] = meta_data["click_id"]  # required

    # Assemble the final URL
    offer_url = urlunparse(parsed._replace(query=urlencode(query_params)))

    # 7. ClickHouse click row (click=true) — awaited inline so the row is
    # observable the moment the redirect returns; failures never block it.
    # Prefetch hits skip the write (still redirected above).
    if not optout and not prefetch:
        await save_click_to_clickhouse(meta_data, campaign_alias)

    response = await flow_action_response(
        offer_url, action_flow, lambda u: RedirectResponse(u))

    # G10: a successful click-out advances the funnel — re-issue the binding
    # cookie with st = min(current + 1, last step), so the NEXT campaign-URL
    # visit serves the next step. Click-out at the last step keeps st at last.
    # Only a previously-bound visitor advances; a bare click-out (no cookie)
    # just redirects without touching funnel state.
    if funnel_cfg is not None and click_bound is not None and not optout:
        steps = funnel_cfg.get("steps") or []
        if steps:
            cur = meta_data.get("funnel_step") or 0
            next_step = min(cur + 1, len(steps) - 1)
            bind_cookie = make_bind_cookie(campaign["id"], 0, offer["id"],
                                           meta_data.get("landing_id"),
                                           campaign_routing_hash(click_cfg, click_dmode),
                                           step=next_step)
            if bind_cookie:
                response.set_cookie(
                    BIND_COOKIE, bind_cookie,
                    max_age=BIND_TTL_SECONDS, path="/", httponly=True, samesite="lax")

    return response


async def save_click_to_db(meta: dict):
    pg = app.state.pg

    # allowed fields from the conversions_data table structure
    allowed_fields = {
        "click_id", "campaign_id", "offer_id", "landing_id", "ad_campaign_id",
        "status", "external_id", "payout", "revenue", "profit", "currency",
        "transaction_id", "country", "region", "city", "ip", "visitor_id",
        "sub_id_1", "sub_id_2", "sub_id_3", "sub_id_4", "sub_id_5",
        "sub_id_6", "sub_id_7", "sub_id_8", "sub_id_9", "sub_id_10",
        "utm_campaign", "utm_creative", "utm_source", "traffic_source_name",
        "os", "isp", "is_using_proxy", "is_bot", "device_type", "flow_index", "funnel_step"
    }

    # keep only the allowed fields
    insert_data = {k: v for k, v in meta.items() if k in allowed_fields}

    insert_data["received_at"] = datetime.utcnow()

    insert_data["status"] = 'lead'

    # (column, placeholder, value) built together so the numbering always
    # matches the value list — filtering "now()" out of values separately
    # desynced $N indices and silently dropped the click row.
    columns = ", ".join(insert_data.keys())
    placeholders = []
    values = []
    for v in insert_data.values():
        if v == "now()":
            placeholders.append("NOW()")
        else:
            placeholders.append(f"${len(values) + 1}")
            values.append(v)

    query = f"INSERT INTO conversions_data ({columns}) VALUES ({', '.join(placeholders)})"

    async with pg.acquire() as conn:
        await conn.execute(query, *values)


# ─── Direct (no-redirect) tracking: /t.js + /t/collect ─────────────
# The JS client keeps the visitor's click id in a first-party cookie
# (aaa_cid); /t/collect records the visit (click=false) and /p/{alias}
# fires conversions reusing that same click id.
DIRECT_COOKIE = "aaa_cid"
DIRECT_COOKIE_TTL = 30 * 24 * 3600
PIXEL_GIF = base64.b64decode("R0lGODlhAQABAAAAACw=")
PIXEL_EXTRA_FIELDS = {"external_id", "transaction_id"} | {f"sub_id_{i}" for i in range(1, 11)}


def open_cors_headers() -> dict:
    """Permissive CORS (no credentials) for the cross-domain pixel/script endpoints."""
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }


T_JS_SOURCE = """(function () {
    "use strict";
    if (window.aaaTrack) return;
    var scriptEl = document.currentScript;
    if (!scriptEl) {
        var all = document.getElementsByTagName("script");
        scriptEl = all[all.length - 1];
    }
    var campaign = "";
    try {
        var qs = (scriptEl.src.split("?")[1] || "").split("&");
        for (var i = 0; i < qs.length; i++) {
            var kv = qs[i].split("=");
            if (decodeURIComponent(kv[0] || "") === "c") {
                campaign = decodeURIComponent(kv.slice(1).join("=") || "");
            }
        }
    } catch (e) { return; }
    if (!campaign) return;

    var COOKIE = "aaa_cid";
    function uuid() {
        if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
        return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
            var r = (Math.random() * 16) | 0, v = c === "x" ? r : (r & 0x3) | 0x8;
            return v.toString(16);
        });
    }
    function readCookie(name) {
        var m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
        return m ? decodeURIComponent(m[1]) : null;
    }
    function getClickId() {
        var id = null;
        try { id = localStorage.getItem(COOKIE); } catch (e) {}
        if (!id) id = readCookie(COOKIE);
        if (!id) id = uuid();
        try { localStorage.setItem(COOKIE, id); } catch (e) {}
        document.cookie = COOKIE + "=" + encodeURIComponent(id) + "; path=/; max-age=2592000; samesite=lax";
        return id;
    }
    var clickId = getClickId();

    function queryString(obj) {
        var parts = [];
        for (var k in obj) {
            if (Object.prototype.hasOwnProperty.call(obj, k) && obj[k] !== null && obj[k] !== undefined) {
                parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(obj[k]));
            }
        }
        return parts.join("&");
    }

    function collectPayload() {
        var p = {
            c: campaign,
            click_id: clickId,
            url: location.href,
            referrer: document.referrer || "",
            title: document.title || ""
        };
        try {
            var us = new URLSearchParams(location.search);
            ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_creative"].forEach(function (k) {
                var v = us.get(k);
                if (v) p[k] = v;
            });
        } catch (e) {}
        return p;
    }

    function track() {
        var data = collectPayload();
        var body = JSON.stringify(data);
        var sent = false;
        if (navigator.sendBeacon) {
            try {
                sent = navigator.sendBeacon("/t/collect", new Blob([body], { type: "application/json" }));
            } catch (e) { sent = false; }
        }
        if (!sent && window.fetch) {
            try {
                fetch("/t/collect", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: body,
                    keepalive: true,
                    mode: "no-cors"
                });
                sent = true;
            } catch (e) { sent = false; }
        }
        if (!sent) {
            var img = new Image();
            img.src = "/t/collect?" + queryString(data);
        }
        return clickId;
    }

    // Append the visitor's click id to an offer/click-out URL so /c/{alias}/{offer}
    // reuses it instead of generating a fresh one.
    function clickThrough(url) {
        var sep = url.indexOf("?") === -1 ? "?" : "&";
        return url + sep + "click_id=" + encodeURIComponent(clickId);
    }

    // Fire a client-side conversion pixel for this visitor.
    function fire(status, payout, extra) {
        var p = { status: status || "lead" };
        if (payout !== undefined && payout !== null && payout !== "") p.payout = payout;
        if (extra) { for (var k in extra) { p[k] = extra[k]; } }
        if (window.fetch) {
            fetch("/p/" + campaign + "?" + queryString(p) + "&fmt=json", { mode: "no-cors" }).catch(function () {});
        } else {
            var img = new Image();
            img.src = "/p/" + campaign + "?" + queryString(p);
        }
    }

    if (document.readyState === "complete" || document.readyState === "interactive") {
        setTimeout(track, 0);
    } else {
        document.addEventListener("DOMContentLoaded", track);
    }

    window.aaaTrack = { clickId: clickId, track: track, clickThrough: clickThrough, fire: fire };
})();
"""


@app.get("/t.js")
async def direct_tracking_js() -> Response:
    return Response(content=T_JS_SOURCE, media_type="application/javascript", headers={
        "Cache-Control": "public, max-age=3600",
        **open_cors_headers(),
    })


async def _collect_payload(request: Request) -> dict:
    """Parse (and cache) the request body the same way enrich_meta does."""
    post_data = getattr(request.state, "_parsed_body", None)
    if post_data is None:
        try:
            content_type = request.headers.get("content-type", "")
            if "application/json" in content_type:
                post_data = await request.json()
            elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
                post_data = dict(await request.form())
            else:
                post_data = {}
        except Exception:
            post_data = {}
        # A JSON scalar/array body ([1,2], 42, "x") is not mergeable — normalize
        # to {} at cache time so {**post_data} merges and `key in query` checks
        # downstream can never raise TypeError on the cached body.
        if not isinstance(post_data, dict):
            post_data = {}
        request.state._parsed_body = post_data
    return post_data if isinstance(post_data, dict) else {}


async def find_campaign_for_tracking(c_ref: str):
    """Active campaign by numeric id, falling back to alias.

    Paused/archived campaigns must not track (status gate): every direct
    endpoint (/t/collect, /p, /i, /click-api, /simulate) resolves through here.
    """
    pg = app.state.pg
    async with pg.acquire() as conn:
        # isdecimal, not isdigit: "²".isdigit() is True but int("²") raises —
        # a crafted campaign ref must 404, not 500.
        if str(c_ref).isdecimal():
            try:
                row = await conn.fetchrow(
                    "SELECT * FROM campaigns WHERE id = $1 AND status = 'active'", int(c_ref))
            except (TypeError, ValueError):
                row = None
            if row:
                return row
        return await conn.fetchrow(
            "SELECT * FROM campaigns WHERE alias = $1 AND status = 'active'", str(c_ref))


@app.api_route("/t/collect", methods=["GET", "POST"])
async def direct_collect(request: Request) -> Response:
    """Record a direct-tracking visit (click=false) and return the visitor's click id."""
    post_data = await _collect_payload(request)
    params = {**post_data, **dict(request.query_params)}

    c_ref = params.get("c") or params.get("campaign_id") or params.get("alias")
    if not c_ref:
        return JSONResponse({"detail": "Missing campaign reference (param 'c')"},
                            status_code=400, headers=open_cors_headers())

    campaign = await find_campaign_for_tracking(str(c_ref))
    if not campaign:
        return JSONResponse({"detail": "Campaign not found"},
                            status_code=404, headers=open_cors_headers())

    # Same bot rules as the redirect path: block means "serve nothing", mark flags the row
    rule, blocked = await apply_bot_rules(request)
    if rule:
        request.state.bot_marked = rule.get("type")
    if blocked:
        log_track(f"🤖 Blocked /t/collect visit to campaign '{c_ref}' by rule '{rule.get('type')}'")
        return Response(content="Not Found", status_code=404, media_type="text/html")

    # Source-declared bot status (opt-in per source) — same trust rule as the
    # redirect path: honored only under the source's configured param name.
    try:
        if source_declares_bot(request, await source_extra_settings(campaign)):
            request.state.bot_marked = "source_is_bot"
            request.state.source_bot = True
    except Exception as e:
        log_track(f"source bot-param check error: {e}")

    click_id = str(params.get("click_id") or request.cookies.get(DIRECT_COOKIE) or "").strip() \
        or generate_click_id()

    # GDPR opt-out (G79): respond normally but store nothing and set no cookies
    if request.cookies.get(OPT_OUT_COOKIE):
        request.state.optout = True
        return JSONResponse({"status": "ok", "click_id": click_id}, headers=open_cors_headers())

    config = json.loads(campaign["config"] or "{}")
    extra_meta = {"visitor_id": click_id}
    title = params.get("title")
    if title and config.get("use_title_as_keyword") and not params.get("keyword"):
        extra_meta["keyword"] = str(title)[:255]

    # Shared insert path — a direct-tracking row is a visit, not a click-through
    await track_event(campaign, request, click=False, extra_meta=extra_meta)

    response = JSONResponse({"status": "ok", "click_id": click_id}, headers=open_cors_headers())
    response.set_cookie(DIRECT_COOKIE, click_id, max_age=DIRECT_COOKIE_TTL, path="/", samesite="lax")
    return response


# ─── G33: honeypot (scraper trap) ──────────────────────────────────
# /t/hp.js injects a hidden decoy link into campaign pages; scrapers that
# follow every link hit /t/hp and permanently (24h) flag their visitor key.
HP_JS_SOURCE = """(function () {
    "use strict";
    var campaign = "";
    try {
        var scriptEl = document.currentScript;
        var qs = (scriptEl.src.split("?")[1] || "").split("&");
        for (var i = 0; i < qs.length; i++) {
            var kv = qs[i].split("=");
            if (decodeURIComponent(kv[0] || "") === "c") {
                campaign = decodeURIComponent(kv.slice(1).join("=") || "");
            }
        }
    } catch (e) { return; }
    var trap = document.createElement("a");
    trap.href = "/t/hp" + (campaign ? "?c=" + encodeURIComponent(campaign) : "");
    trap.style.display = "none";
    trap.rel = "nofollow";
    trap.setAttribute("aria-hidden", "true");
    trap.textContent = "";
    (document.body || document.documentElement).appendChild(trap);
})();
"""


@app.get("/t/hp.js")
async def honeypot_js() -> Response:
    return Response(content=HP_JS_SOURCE, media_type="application/javascript", headers={
        "Cache-Control": "public, max-age=3600",
        **open_cors_headers(),
    })


@app.get("/t/hp")
async def honeypot_hit(request: Request) -> Response:
    """Record a honeypot hit and return empty 204.

    Rate-limited to one row per visitor key per hour. Real users never reach
    this URL — the link is invisible — so every row is a scraper/bot; the
    visitor key is flagged (24h, cached 60s in-memory) on subsequent gate
    checks, which forces fraud_score=100 and is_bot on the tracked row and
    fails campaign shield whitelists.
    """
    ua = request.headers.get("user-agent", "") or ""
    vkey = visitor_key_for(request)
    c_ref = (request.query_params.get("c") or "").strip()
    campaign_id = None
    if c_ref:
        campaign = await find_campaign_for_tracking(c_ref)
        if campaign:
            campaign_id = campaign["id"]
    try:
        async with app.state.pg.acquire() as conn:
            await conn.execute(
                "INSERT INTO honeypot_hits (visitor_key, ip, ua, campaign_id) "
                "SELECT $1, $2, $3, $4 "
                "WHERE NOT EXISTS (SELECT 1 FROM honeypot_hits "
                "WHERE visitor_key = $1 "
                "AND received_at > NOW() - INTERVAL '1 hour')",
                vkey, resolve_client_ip(request), str(ua)[:512], campaign_id)
    except Exception as e:
        log_track(f"Honeypot insert error: {e}")
    return Response(status_code=204)



# ─── Telegram conversion notifications ─────────────────────────────
TELEGRAM_STATUS_EMOJI = {
    "lead": "💵", "sale": "💰", "upsale": "💎",
    "rejected": "❌", "hold": "⏳", "trash": "🗑️",
}
ALL_STATUSES = ["lead", "sale", "upsale", "rejected", "hold", "trash"]


def load_telegram_config() -> dict:
    """Read the telegram block from the settings row (sync, own connection)."""
    try:
        conn = pg_connect()
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE name = 'settings'")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            cfg = json.loads(row[0])
            return cfg.get("telegram") or {}
    except Exception as e:
        log_track(f"Telegram config load error: {e}")
    return {}


# ─── Settings-row block cache ──────────────────────────────────────
# Bot rules / postback security / privacy / custom statuses were read from
# Postgres with a fresh sync psycopg2 connection on EVERY hit, stalling the
# event loop. A short-TTL cache keeps reads event-loop friendly; the cache
# starts empty on boot (fresh read on startup) and settings saves become
# visible within the TTL window.
_SETTINGS_CACHE_TTL = 30.0
_settings_cache: dict = {}
# Sentinel: the settings-row read itself failed (DB down/error) — distinct from
# "the block is genuinely absent", which is a configured state.
_SETTINGS_READ_FAILED = object()


def _read_settings_block(key: str):
    """Fresh (sync, own connection) read of one block from the settings row.

    Returns _SETTINGS_READ_FAILED when the read itself errors, so callers can
    fail closed instead of treating the outage as 'no security configured'.
    """
    try:
        conn = pg_connect()
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE name = 'settings'")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return json.loads(row[0]).get(key)
        return None
    except Exception as e:
        log_track(f"Settings '{key}' load error: {e}")
        return _SETTINGS_READ_FAILED


def _settings_block(key: str, default=None):
    """Cached read of a settings-row block (30s TTL).

    A failed read is served as the default for this request only and never
    cached — the next hit retries, so a recovered DB is picked up immediately.
    """
    if default is None:
        default = {}
    now = time.monotonic()
    hit = _settings_cache.get(key)
    if hit is not None and now - hit[0] < _SETTINGS_CACHE_TTL:
        return hit[1]
    value = _read_settings_block(key)
    if value is _SETTINGS_READ_FAILED:
        return default
    if not isinstance(value, type(default)):
        value = default
    _settings_cache[key] = (now, value)
    return value


def load_postback_security() -> dict:
    """Read the postback_security block (30s TTL cache), failing CLOSED.

    A DB read failure returns a sentinel dict that check_postback_access denies
    on, and nothing is cached — a transient outage must never disable postback
    security for the following 30s. A genuinely empty/missing block keeps the
    configured-open behavior.
    """
    now = time.monotonic()
    hit = _settings_cache.get("postback_security")
    if hit is not None and now - hit[0] < _SETTINGS_CACHE_TTL:
        return hit[1]
    value = _read_settings_block("postback_security")
    if value is _SETTINGS_READ_FAILED:
        return {"read_failed": True}
    if not isinstance(value, dict):
        value = {}
    _settings_cache["postback_security"] = (now, value)
    return value


def check_postback_access(request: Request, sec: dict) -> tuple[bool, str]:
    """Enforce the optional secret key and IP allowlist on inbound postbacks."""
    if sec.get("read_failed"):
        return False, "Postback security state unavailable — denying (fail closed)"
    client_ip = resolve_client_ip(request)

    # IP allowlist: comma-separated IPs or CIDR ranges (e.g. 52.1.2.3, 52.0.0.0/8)
    allowed = (sec.get("allowed_ips") or "").strip()
    if allowed:
        from ipaddress import ip_address, ip_network
        try:
            addr = ip_address(client_ip)
        except ValueError:
            return False, f"Client IP '{client_ip}' is not a valid address"
        ok = False
        for raw in allowed.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                if "/" in raw:
                    if addr in ip_network(raw, strict=False):
                        ok = True
                        break
                elif addr == ip_address(raw):
                    ok = True
                    break
            except ValueError:
                continue
        if not ok:
            return False, f"Client IP '{client_ip}' is not in the allowlist"

    # Secret key: accept ?key=... or ?secret=... (or an X-Postback-Key header)
    secret = (sec.get("secret_key") or "").strip()
    if secret:
        provided = (request.query_params.get("key")
                    or request.query_params.get("secret")
                    or request.headers.get("x-postback-key")
                    or "")
        # bytes, not str: compare_digest rejects non-ASCII str input with a
        # TypeError (500) — a crafted ?key=… must just fail the check.
        if not secrets.compare_digest(str(provided).encode(), str(secret).encode()):
            return False, "Invalid or missing postback key"

    return True, ""


# ====== Bot & filter rules ======
_seen_visitors: set = set()
_SEEN_VISITORS_CAP = 200_000


def load_bot_rules() -> dict:
    """Read the bot_rules block from the settings row (30s TTL cache)."""
    return _settings_block("bot_rules")


def visitor_key_for(request: Request) -> str:
    """Stable per-visitor key (IP + User-Agent) used by the duplicate-visitor rule."""
    ua = request.headers.get("user-agent", "") or ""
    ip = resolve_client_ip(request)
    import hashlib
    return hashlib.md5(f"{ip}|{ua}".encode()).hexdigest()


def load_custom_statuses() -> list:
    """Read the custom_statuses array from the settings row (30s TTL cache).

    Each entry is {name, payout_default?, color?}; only the normalized name is
    used by the engine.
    """
    raw = _settings_block("custom_statuses", default=[])
    return [s for s in raw
            if isinstance(s, dict) and str(s.get("name") or "").strip()]


def load_postback_rules() -> list:
    """Read the global postback_rules array from the settings row (30s TTL cache).

    G85: an ordered list of {enabled, name, conditions: [{field, operator,
    value, condition?}], action: {type, ...}} evaluated on every inbound /pb
    postback BEFORE the conversion row is written/updated and before fanout.
    Absent/empty = no behavior change.
    """
    raw = _settings_block("postback_rules", default=[])
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def normalize_status(status: str) -> str:
    """Slugify a status to lowercase_underscore (case/space/punctuation tolerant)."""
    return re.sub(r"[^a-z0-9]+", "_", str(status or "").strip().lower()).strip("_")


def valid_conversion_statuses() -> set:
    """Engine built-ins plus configured custom statuses (all normalized)."""
    customs = {normalize_status(s.get("name")) for s in load_custom_statuses()}
    return set(ALL_STATUSES) | {c for c in customs if c}


def match_bot_rule(request: Request, rules: list, visitor_key: str):
    """Return the first matching rule, or None."""
    client_ip = resolve_client_ip(request)
    ua = request.headers.get("user-agent", "") or ""
    referer = request.headers.get("referer", "") or ""

    for rule in rules or []:
        if rule.get("enabled") is False:
            continue
        rtype = rule.get("type") or ""
        value = (rule.get("value") or "").strip()

        if rtype == "ip":
            if client_ip and client_ip in [x.strip() for x in value.split(",") if x.strip()]:
                return rule
        elif rtype == "ip_range":
            from ipaddress import ip_address, ip_network
            try:
                addr = ip_address(client_ip)
            except ValueError:
                continue
            for raw in value.split(","):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    if addr in ip_network(raw, strict=False):
                        return rule
                except ValueError:
                    continue
        elif rtype == "ua_regex":
            if value:
                try:
                    if re.search(value, ua):
                        return rule
                except re.error:
                    continue
        elif rtype == "empty_referer":
            if not referer:
                return rule
        elif rtype == "duplicate_visitor":
            if visitor_key and visitor_key in _seen_visitors:
                return rule

    return None


async def apply_bot_rules(request: Request):
    """Evaluate bot rules for an inbound visit.

    Returns (rule, blocked) — blocked=True means serve 404 without tracking;
    otherwise the rule (if any) marks the click as a bot but tracking continues.
    Rule matching runs in a worker thread with a 2s ceiling so a catastrophic
    user regex can never hang the event loop; on timeout the rules are skipped.
    """
    cfg = load_bot_rules()
    if not cfg.get("enabled"):
        return None, False

    rules = cfg.get("rules") or []
    has_duplicate_rule = any(
        (r.get("type") == "duplicate_visitor" and r.get("enabled") is not False)
        for r in rules)
    visitor_key = visitor_key_for(request) if has_duplicate_rule else ""

    try:
        rule = await asyncio.wait_for(
            asyncio.to_thread(match_bot_rule, request, rules, visitor_key),
            timeout=2.0)
    except asyncio.TimeoutError:
        log_track("⏱ Bot-rule evaluation timed out (2s); skipping rules for this hit")
        rule = None

    if has_duplicate_rule and visitor_key:
        if len(_seen_visitors) >= _SEEN_VISITORS_CAP:
            _seen_visitors.clear()
        _seen_visitors.add(visitor_key)

    if rule:
        if (rule.get("action") or "block") == "block":
            return rule, True
        return rule, False
    return None, False


# ─── Traffic-quality blacklists (G44) ──────────────────────────────
BLACKLIST_FIELDS = ("sub_id_1", "sub_id_2", "sub_id_3", "sub_id_4", "sub_id_5",
                    "sub_id_6", "sub_id_7", "sub_id_8", "sub_id_9", "sub_id_10",
                    "country", "city", "device_type", "os", "browser", "ip")


def load_blacklists() -> list:
    """Read the blacklists block from the settings row (30s TTL cache)."""
    raw = _settings_block("blacklists", default=[])
    return raw if isinstance(raw, list) else []


def blacklist_action(meta: dict, client_ip: str, lists: list, campaign_id):
    """Decisive action of the enabled blacklists matching this hit:
    "block" when any matching list blocks, "mark" when at least one matches
    and none blocks, else None. A campaign-scoped list only applies to its
    campaign; a global list applies everywhere. Field values come from the
    hit meta (sub_id_1..10 land there via query/post/cookies, ip from the
    resolved client IP). IP values match exactly or, when written as CIDR
    (contains '/'), by network containment.
    """
    matched_mark = False
    for bl in lists or []:
        if not isinstance(bl, dict) or bl.get("enabled") is False:
            continue
        if (bl.get("scope") or "global") == "campaign":
            try:
                if campaign_id is None or int(bl.get("campaign_id")) != int(campaign_id):
                    continue
            except (TypeError, ValueError):
                continue
        field = bl.get("field") or ""
        if field not in BLACKLIST_FIELDS:
            continue
        values = [str(v).strip() for v in (bl.get("values") or []) if str(v).strip()]
        if not values:
            continue

        if field == "ip":
            if not client_ip:
                continue
            plain = [v for v in values if "/" not in v]
            nets = [v for v in values if "/" in v]
            # Exact entries compare canonically (so 2001:db8::1 matches
            # 2001:0db8:0:0:0:0:0:1); CIDR nets go through _ip_in_cidrs,
            # which is version-aware — a v4 CIDR simply never matches a v6
            # client and vice versa.
            matched = False
            try:
                addr = ipaddress.ip_address(str(client_ip))
                for v in plain:
                    try:
                        if ipaddress.ip_address(v) == addr:
                            matched = True
                            break
                    except ValueError:
                        if str(client_ip) == v:
                            matched = True
                            break
            except ValueError:
                matched = str(client_ip) in plain
            if not matched and nets and _ip_in_cidrs(client_ip, nets):
                matched = True
            if matched:
                action = (bl.get("action") or "mark").strip().lower()
                if action == "block":
                    return "block"
                matched_mark = True
            continue

        value = str(meta.get(field) or "").strip()
        if value and value in values:
            action = (bl.get("action") or "mark").strip().lower()
            if action == "block":
                return "block"
            matched_mark = True
    return "mark" if matched_mark else None


async def apply_blacklists(request: Request, campaign) -> "str | None":
    """Evaluate the G44 blacklists for an inbound hit.

    Returns "block" (caller serves 404 untracked, like a bot block) or "mark"
    (request.state.bot_marked is set; track_event flags the row is_bot) or
    None. Matching runs only when at least one blacklist exists; the list
    itself comes from the 30s-TTL settings cache because this runs on every
    hit. The hit meta (sub_id_1..10, country, os, ...) is built with the same
    enrich_meta used downstream so blacklist values match stored values.
    """
    lists = load_blacklists()
    if not lists:
        return None
    campaign_id = campaign["id"] if campaign is not None else None
    meta = await enrich_meta(request)
    action = blacklist_action(meta, resolve_client_ip(request), lists, campaign_id)
    if action == "mark":
        request.state.bot_marked = "blacklist"
    return action


# ─── Heuristic fraud scoring (monitoring-only) ─────────────────────
# Signals are additive, capped at 100, and NEVER block anything by themselves —
# blocking is the job of the bot rules and the campaign shield below.
GENERIC_UA_PATTERN = re.compile(
    r"curl|wget|python|requests|httpx|aiohttp|urllib|go-http-client|"
    r"headless|phantom|selenium|puppeteer|playwright", re.IGNORECASE)
HOSTING_ISP_PATTERN = re.compile(
    r"hosting|cloud|datacenter|datacentre|server|ovh|digitalocean|amazon|"
    r"google llc|hetzner|vultr|linode|m247|psychz|contabo", re.IGNORECASE)
# A UA claiming to be one of these crawlers while its IP is NOT in the
# verified crawler ranges is a spoofed crawler — a classic spy tool.
CRAWLER_UA_PATTERN = re.compile(
    r"googlebot|bingbot|duckduckbot|slurp|baiduspider|yandexbot", re.IGNORECASE)

# Verified search-engine crawler nets (Googlebot, Bingbot, DuckDuckGo).
# These are is_bot with the top score but NOT fraud: no hosting-pattern points,
# reported separately so reporting can split "good bots" from fraud.
VERIFIED_CRAWLER_CIDRS = [
    "66.249.64.0/19",   # Googlebot
    "157.55.39.0/24",   # Bingbot
    "40.77.167.0/24",   # Bingbot
    "13.66.139.0/24",   # DuckDuckGo
    "207.46.13.0/24",   # Bingbot
]

# Duplicate-visitor ring: process-local deque of (monotonic_ts, visitor_key).
# Duplicates are a heuristic, not ground truth — per-process is fine and keeps
# the hot path allocation-free-ish.
_DUP_VISITOR_WINDOW = 600.0   # 10 minutes
_DUP_VISITOR_CAP = 200_000
_dup_visitor_log = deque()        # (ts, visitor_key), expiry order
_dup_visitor_counts: dict = {}    # visitor_key → hits inside the window


def _ip_in_cidrs(ip: str, cidrs) -> bool:
    try:
        addr = ipaddress.ip_address(str(ip))
    except ValueError:
        return False
    for raw in cidrs or []:
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            if addr in ipaddress.ip_network(raw, strict=False):
                return True
        except ValueError:
            continue
    return False


def _dup_hits_in_window(visitor_key: str) -> int:
    """Prior hits for this visitor key inside the 10-min window; logs this hit."""
    now = time.monotonic()
    while _dup_visitor_log and now - _dup_visitor_log[0][0] > _DUP_VISITOR_WINDOW:
        _, old_key = _dup_visitor_log.popleft()
        remaining = _dup_visitor_counts.get(old_key, 0) - 1
        if remaining > 0:
            _dup_visitor_counts[old_key] = remaining
        else:
            _dup_visitor_counts.pop(old_key, None)
    count = _dup_visitor_counts.get(visitor_key, 0)
    if len(_dup_visitor_log) >= _DUP_VISITOR_CAP:
        _dup_visitor_log.popleft()  # crude cap; counts may drift, heuristic only
    _dup_visitor_log.append((now, visitor_key))
    _dup_visitor_counts[visitor_key] = count + 1
    return count


def compute_fraud_score(*, visitor_key: str, ip: str, ua: str, isp: str,
                        is_bot: bool) -> tuple[int, bool, list]:
    """Heuristic 0-100 fraud score. Returns (score, verified_crawler, reasons).

    Signals: +50 bot (ua-parser/rules), +30 empty/generic UA, +20 duplicate
    visitor (process-local 10-min window), +15 hosting/datacenter ISP,
    +25 IP in the user suspicious-range list, +50 crawler-UA spoof (crawler
    claimed but not from a verified crawler net). Cap 100.
    Both CIDR lists are settings-overridable via settings.fraud.
    """
    fraud_cfg = _settings_block("fraud")
    verified_cidrs = fraud_cfg.get("verified_cidrs") or VERIFIED_CRAWLER_CIDRS
    suspicious_cidrs = fraud_cfg.get("suspicious_cidrs") or []
    try:
        dup_threshold = max(int(fraud_cfg.get("duplicate_threshold") or 3), 1)
    except (TypeError, ValueError):
        dup_threshold = 3

    # Verified crawler: top score, reported separately, no fraud signals.
    if ip and _ip_in_cidrs(ip, verified_cidrs):
        return 100, True, ["verified_crawler"]

    score = 0
    reasons = []
    if is_bot:
        score += 50
        reasons.append("bot_detected")
    if not (ua or "").strip() or GENERIC_UA_PATTERN.search(ua or ""):
        score += 30
        reasons.append("generic_ua")
    if visitor_key and _dup_hits_in_window(visitor_key) >= dup_threshold:
        score += 20
        reasons.append("duplicate_visitor")
    if (isp or "") and HOSTING_ISP_PATTERN.search(isp):
        score += 15
        reasons.append("hosting_isp")
    if ip and suspicious_cidrs and _ip_in_cidrs(ip, suspicious_cidrs):
        score += 25
        reasons.append("suspicious_ip")
    if CRAWLER_UA_PATTERN.search(ua or ""):
        score += 50
        reasons.append("crawler_spoof")
    return min(score, 100), False, reasons


# ─── Campaign Shield (cloaking) — G32/G34 ──────────────────────────
BLANK_PAGE_HTML = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
                   "<title></title></head><body></body></html>")


def render_blank_html() -> Response:
    """Shield 'blank' action: minimal 200 page, untracked (like a bot block)."""
    return Response(content=BLANK_PAGE_HTML, status_code=200, media_type="text/html")


def campaign_shield_config(campaign) -> dict:
    """The optional campaign config 'shield' block ({enabled, whitelists,
    action, honeypot}), or {} when absent/disabled-shaped."""
    try:
        raw = campaign.get("config") if hasattr(campaign, "get") else None
        cfg = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, json.JSONDecodeError):
        return {}
    shield = (cfg or {}).get("shield")
    return shield if isinstance(shield, dict) else {}


async def evaluate_campaign_shield(request: Request, campaign) -> "str | None":
    """Campaign Shield evaluator — SINGLE shared decision point for both the
    redirect path (apply_tracking_gate) and the click-api decision path, so a
    request can never get different shield decisions per entry point.
    KEEP IN SYNC: any change here applies to both paths automatically.

    Returns None to proceed normally, or one of:
      "blank" / "404" — short-circuit the response, never track (untracked);
      "allow"         — proceed, but the tracked row is forced
                        fraud_score >= 50 and is_bot (watch mode; sets
                        request.state.shield_watch, consumed by track_event).

    Whitelists are OR'd: ip CIDR match, referrer substring, UA regex — any
    match whitelists the request. An empty whitelist matches everything
    (shield off). Honeypot-flagged visitors can never be whitelisted; with
    honeypot=true, verified crawlers can't either (the spy-tool case) and
    always get the shield action.
    """
    shield = campaign_shield_config(campaign)
    if not shield.get("enabled"):
        return None

    whitelists = shield.get("whitelists") or {}
    wl_ips = [str(x).strip() for x in (whitelists.get("ips") or []) if str(x).strip()]
    wl_refs = [str(x) for x in (whitelists.get("referers") or [])]
    wl_ua = (whitelists.get("ua_regex") or "").strip()

    # Empty whitelist = everything matches = shield off.
    if not wl_ips and not wl_refs and not wl_ua:
        return None

    client_ip = resolve_client_ip(request)
    referer = request.headers.get("referer", "") or ""
    ua = request.headers.get("user-agent", "") or ""

    fraud_cfg = _settings_block("fraud")
    verified_cidrs = fraud_cfg.get("verified_cidrs") or VERIFIED_CRAWLER_CIDRS
    is_verified_crawler = bool(client_ip) and _ip_in_cidrs(client_ip, verified_cidrs)

    whitelisted = False
    if not getattr(request.state, "honeypot_flagged", False):
        # Verified crawlers with honeypot on are the adversary here — they
        # bypass the whitelist entirely and always get the shield action.
        if not (is_verified_crawler and shield.get("honeypot")):
            if wl_ips and _ip_in_cidrs(client_ip, wl_ips):
                whitelisted = True
            if not whitelisted and wl_refs:
                # substring containment; an empty referrer matches nothing
                # unless the list literally contains ""
                whitelisted = any(sub in referer for sub in wl_refs)
            if not whitelisted and wl_ua:
                try:
                    whitelisted = bool(re.search(wl_ua, ua))
                except re.error:
                    whitelisted = False

    if whitelisted:
        return None

    action = str(shield.get("action") or "allow").strip().lower()
    if action not in ("blank", "404", "allow"):
        action = "allow"
    if action == "allow":
        # Watch mode: tracked, but flagged (see track_event).
        request.state.shield_watch = True
    return action


# ─── Honeypot (G33) ────────────────────────────────────────────────
_honeypot_flag_cache: dict = {}
_HONEYPOT_FLAG_TTL = 60.0


async def honeypot_flagged(visitor_key: str) -> bool:
    """True when the visitor tripped the /t/hp decoy in the last 24h.

    Lookups are cached in-memory (positives and negatives) for 60s with a
    process-local dict — same pattern as the settings block cache.
    """
    if not visitor_key:
        return False
    now = time.monotonic()
    hit = _honeypot_flag_cache.get(visitor_key)
    if hit is not None and now < hit[0]:
        return hit[1]
    flagged = False
    try:
        async with app.state.pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM honeypot_hits WHERE visitor_key = $1 "
                "AND received_at > NOW() - INTERVAL '24 hours' LIMIT 1",
                visitor_key)
            flagged = row is not None
    except Exception as e:
        log_track(f"Honeypot lookup error: {e}")
    if len(_honeypot_flag_cache) > 100_000:
        _honeypot_flag_cache.clear()
    _honeypot_flag_cache[visitor_key] = (now + _HONEYPOT_FLAG_TTL, flagged)
    return flagged


async def apply_tracking_gate(request: Request, label: str, campaign=None) -> Response | None:
    """Bot-rule + shield + GDPR opt-out gate shared by every campaign-serving route.

    Returns a Response to short-circuit with (bot-blocked 404 / shield blank /
    shield 404 — all untracked), or None to proceed. A non-blocking bot rule
    marks request.state.bot_marked; an opted-out visitor sets request.state.optout —
    the caller still serves the funnel but stores nothing (do_campaign_execution
    skips tracking when optout is set).

    Campaign Shield is enforced HERE on the redirect path; the click-api path
    mirrors it via the same evaluate_campaign_shield helper — keep the two
    call sites in sync when adding new gate checks.
    """
    rule, blocked = await apply_bot_rules(request)
    if rule:
        request.state.bot_marked = rule.get("type")
        if blocked:
            log_track(f"🤖 Blocked visit to '{label}' by rule '{rule.get('type')}'")
            return render_404_html()

    # Source-declared bot status: honored ONLY when the campaign's traffic
    # source opts in with an `is_bot_param` name (additional_settings). A
    # free-form ?is_bot=1 from a normal visitor is ignored — otherwise anyone
    # could spoof themselves out of tracked traffic.
    if campaign is not None:
        try:
            extra = await source_extra_settings(campaign)
            if source_declares_bot(request, extra):
                request.state.bot_marked = "source_is_bot"
                request.state.source_bot = True
                log_track(f"🤖 Source-declared bot for '{label}'")
        except Exception as e:
            log_track(f"source bot-param check error: {e}")

    # G44 traffic-quality blacklists: "block" → untracked 404 (same as a bot
    # block); "mark" → bot_marked, track_event flags the row is_bot.
    bl_action = await apply_blacklists(request, campaign)
    if bl_action == "block":
        log_track(f"🚫 Blacklist-blocked visit to '{label}'")
        return render_404_html()

    # G33 honeypot: visitors that hit the /t/hp decoy are bots (fraud_score=100
    # in track_event) and can never pass a campaign shield whitelist.
    if await honeypot_flagged(visitor_key_for(request)):
        request.state.honeypot_flagged = True
        if not getattr(request.state, "bot_marked", None):
            request.state.bot_marked = "honeypot"

    # G32/G34 campaign shield (cloaking) — the click-api path evaluates the
    # same helper on its synthetic request; keep both call sites in sync.
    if campaign is not None:
        shield_action = await evaluate_campaign_shield(request, campaign)
        if shield_action == "blank":
            log_track(f"🛡 Shield blanked visit to '{label}'")
            return render_blank_html()
        if shield_action == "404":
            log_track(f"🛡 Shield 404'd visit to '{label}'")
            return render_404_html()
    if request.cookies.get(OPT_OUT_COOKIE):
        request.state.optout = True
        log_track(f"🔕 Opted-out visit to '{label}': redirect without tracking")
    return None


def notify_telegram_conversion(click_id: str, status: str, payout: float, row: dict = None):
    """Send a Telegram message for a conversion (runs in a background task)."""
    try:
        cfg = load_telegram_config()
        if not cfg.get("enabled"):
            return
        statuses = cfg.get("statuses") or {}
        if statuses and not statuses.get(status, False):
            return
        token = (cfg.get("bot_token") or "").strip()
        chat_id = (cfg.get("chat_id") or "").strip()
        if not token or not chat_id:
            return

        emoji = TELEGRAM_STATUS_EMOJI.get(status, "🔔")
        row = row or {}
        lines = [f"{emoji} <b>{status.upper()}</b> — {payout}"]
        campaign = row.get("campaign_name") or row.get("name") or ""
        if campaign:
            lines.append(f"📊 Campaign: {campaign}")
        geo = " • ".join(x for x in [row.get("country"), row.get("device_type"), row.get("os")] if x)
        if geo:
            lines.append(f"🌍 {geo}")
        if row.get("sub_id_1"):
            lines.append(f"🏷 Sub ID: {row['sub_id_1']}")
        lines.append(f"🔗 Click: {click_id}")
        lines.append(f"🕐 {datetime.utcnow().strftime('%d %b %Y %H:%M UTC')}")

        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "HTML"},
            timeout=10)
    except Exception as e:
        log_track(f"Telegram notify error: {e}")


async def notify_telegram_conversion_async(click_id: str, status: str, payout: float,
                                           row: dict = None):
    """Event-loop-safe wrapper: the blocking HTTP call runs in a thread."""
    await asyncio.to_thread(notify_telegram_conversion, click_id, status, payout, row)


async def sync_conversion_to_clickhouse(click_id: str):
    """Best-effort mirror of a conversions_data row onto the CH click row.

    Called in the background after a successful postback write. NOTE: the
    table is a plain MergeTree ordered by received_at, so the UPDATE below is a
    heavy full-part mutation — at high volume this should move to a
    ReplacingMergeTree/AggregatingMergeTree design instead.
    """
    try:
        pg = app.state.pg
        async with pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, revenue, profit FROM conversions_data "
                "WHERE click_id = $1 ORDER BY received_at DESC LIMIT 1",
                click_id)
        if not row:
            return
        ch = acquire_ch()
        try:
            await asyncio.to_thread(
                ch.command,
                "ALTER TABLE clicks_data UPDATE "
                "status = %(status)s, revenue = %(revenue)s, profit = %(profit)s "
                "WHERE click_id = %(cid)s",
                parameters={
                    "status": row["status"] or "",
                    "revenue": row["revenue"],
                    "profit": row["profit"],
                    "cid": click_id,
                },
                settings={"mutations_sync": 1})
        except Exception:
            release_ch(ch, failed=True)
            raise
        release_ch(ch)
    except Exception as e:
        log_track(f"❌ ClickHouse conversion sync failed for {click_id}: {e}")


async def record_conversion(click_id: str, status: str, payout_value: float, request: Request,
                            background_tasks: BackgroundTasks, extra_fields: dict = None,
                            source: str = "postback") -> dict:
    """Shared conversion core for the /pb postback and the /p pixel.

    LTV semantics (G23): a non-duplicate repeat conversion for the same click
    ACCUMULATES — payout/revenue add to the stored values, postback_count
    increments, last_postback_at updates and an entry is appended to the
    events JSONB history. Status follows the latest postback (lead→sale
    upgrades, sale→sale stays sale). Dedupe: a repeated transaction_id, or an
    identical (status, payout) anonymous re-fire within 60s of the last event.

    Clickless (G20): click_id absent/'none'/0 records an unattributed row
    (click_id 'none'), attributing via transaction/external id or, when
    sub_id_1..5 params uniquely match one real click in the last 7 days,
    to that click instead.
    """
    pg = request.app.state.pg
    now = datetime.utcnow()
    extra_fields = dict(extra_fields or {})
    # Column limits at the edge: click_id VARCHAR(100), sub_id_i VARCHAR(50) —
    # an over-long value would 500 the asyncpg insert/update.
    click_id = str(click_id or "").strip()[:100]
    extra_fields = {k: (str(v)[:50] if k.startswith("sub_id_") and v is not None else v)
                    for k, v in extra_fields.items()}
    event = {"status": status, "payout": payout_value,
             "received_at": now.isoformat(), "source": source}
    clickless = str(click_id or "").strip().lower() in ("", "none", "0")

    # conversions_data has no cost column → profit recomputes to revenue.
    # The dedupe guard lives in the WHERE clause of one atomic UPDATE: the
    # event is appended (events || jsonb) and money accumulated server-side
    # only when the guard passes, so two concurrent identical postbacks can
    # never both add payout. Duplicate postbacks only advance the counter.
    async def apply_to_row(conn, row) -> bool:
        """Accumulate the conversion onto an existing row. Returns is_duplicate."""
        ext_id = (extra_fields.get("transaction_id") or extra_fields.get("external_id")
                  or request.query_params.get("transaction_id")
                  or request.query_params.get("external_id"))

        set_parts = [
            "UPDATE conversions_data SET",
            "    status = $1,",
            "    payout = COALESCE(payout, 0) + $2,",
            "    revenue = COALESCE(revenue, 0) + $3,",
            "    profit = COALESCE(revenue, 0) + $3,",
            "    postback_count = COALESCE(postback_count, 0) + 1,",
            "    last_postback_at = NOW(),",
            "    events = COALESCE(events, '[]'::jsonb) || $4::jsonb",
        ]
        args = [status, payout_value, payout_value, json.dumps(event), str(ext_id) if ext_id else None]
        idx = 6
        for k, v in extra_fields.items():
            set_parts.append(f", {k} = ${idx}")
            args.append(v)
            idx += 1
        args.append(row["id"])
        guard = f"""
        WHERE id = ${idx}
          AND (${5}::text IS NULL OR transaction_id IS NULL OR transaction_id <> ${5})
          AND (${5}::text IS NOT NULL
               OR COALESCE(events, '[]'::jsonb) = '[]'::jsonb
               OR NOT (events->-1->>'status' = $1
                       AND COALESCE((events->-1->>'payout')::double precision, 0) - $2
                           BETWEEN -0.005 AND 0.005
                       AND last_postback_at > NOW() - INTERVAL '60 seconds'))
        """
        res = await conn.execute("".join(set_parts) + guard, *args)
        if res and res.startswith("UPDATE 1"):
            return False

        # Guard rejected the write: a repeated transaction id, or an identical
        # anonymous re-fire inside the 60s window. Count the postback, keep
        # money/history untouched (concurrency-safe increment).
        await conn.execute(
            "UPDATE conversions_data SET postback_count = COALESCE(postback_count, 0) + 1, "
            "last_postback_at = NOW() WHERE id = $1", row["id"])
        return True

    async def insert_row(conn, click_id_value: str) -> None:
        cols = ["click_id", "status", "payout", "revenue", "profit", "postback_count",
                "last_postback_at", "events"]
        vals = ["$1", "$2", "$3", "$3", "$3", "1", "NOW()", "$4::jsonb"]
        args = [click_id_value, status, payout_value, json.dumps([event])]
        idx = 5
        for k, v in extra_fields.items():
            cols.append(k)
            vals.append(f"${idx}")
            args.append(v)
            idx += 1
        await conn.execute(
            f"INSERT INTO conversions_data ({', '.join(cols)}) VALUES ({', '.join(vals)})",
            *args)

    def schedule_ch_sync(cid: str):
        """Mirror the fresh conversion onto the CH click row (best-effort).

        Only real click ids — the unattributed 'none' rows have no CH
        counterpart. Failures inside the task are logged, never propagated.
        """
        if cid and str(cid).strip().lower() not in ("", "none", "0"):
            background_tasks.add_task(sync_conversion_to_clickhouse, str(cid))

    JOINED_CLICK_QUERY = """
        SELECT c.*, ca.config AS campaign_config, ca.name AS campaign_name,
               ca.traffic_source_id AS traffic_source_id
        FROM conversions_data c
            LEFT JOIN campaigns ca on c.campaign_id = ca.id
        WHERE c.click_id = $1
    """

    async def fanout(conn, row, cid: str):
        """Telegram notification, campaign cycle postbacks, source s2s postback."""
        campaign_config = row["campaign_config"] if "campaign_config" in row.keys() else None
        traffic_source_id = row["traffic_source_id"] if "traffic_source_id" in row.keys() else None
        if not campaign_config:
            return
        config = json.loads(campaign_config)
        for postback in config.get("postbacks", []):
            url = postback.get("url")
            if url:
                we_pay = payout_value
                offer = await conn.fetchrow("SELECT * FROM offers WHERE id = $1", row["offer_id"])
                if offer:
                    we_pay = offer["payout"]
                # Row values first, fresh values last — a lead→sale upgrade must
                # forward status=sale, not the stale pre-update row value.
                data = dict(row)
                data.update({"click_id": cid, "status": status, "payout": we_pay})
                background_tasks.add_task(
                    send_postback, url, data, post=postback.get("method") == "POST")

        if traffic_source_id:
            source = await conn.fetchrow("SELECT * FROM sources WHERE id = $1", traffic_source_id)
            if source and source["s2s_postback"]:
                # G86 source-level fanout controls ride in the source's
                # additional_settings JSON (the field the source API/UI already
                # round-trips); campaign postbacks above are unaffected.
                source_extra = source["additional_settings"] if "additional_settings" in source.keys() else None
                if isinstance(source_extra, str):
                    try:
                        source_extra = json.loads(source_extra)
                    except Exception:
                        source_extra = {}
                if not isinstance(source_extra, dict):
                    source_extra = {}

                # (a) disable_upsell — never forward upsell conversions to the source.
                if status == "upsale" and source_extra.get("disable_upsell"):
                    return

                raw_statuses = source["s2s_postback_statuses"]
                src_statuses = json.loads(raw_statuses) if isinstance(raw_statuses, str) else (raw_statuses or {})
                # The sources UI stores keys as rejected/upsale; older payloads
                # may still carry reject/upsell. Accept BOTH spellings so the
                # configured toggles actually gate (a mismatch used to leave the
                # two per-status switches inert).
                status_map = {"sale": "sale", "lead": "lead",
                              "rejected": "rejected", "reject": "rejected",
                              "upsale": "upsale", "upsell": "upsale"}
                fire = any(tracker_status == status and src_statuses.get(src_key)
                           for src_key, tracker_status in status_map.items())
                if not src_statuses:
                    fire = True
                # (b) sample_percent — deterministic per click_id so a retry of
                # the same postback never alternates.
                if fire and not _sample_allows(cid, source_extra.get("sample_percent")):
                    fire = False
                if fire:
                    # Fresh status/payout overwrite the stale pre-update row values.
                    src_data = {k: v for k, v in dict(row).items()
                                if v is not None and k not in ("status", "payout")}
                    src_data.update({"click_id": cid, "status": status, "payout": payout_value})
                    src_data["clickid"] = cid
                    background_tasks.add_task(
                        send_postback, source["s2s_postback"], src_data, post=False)

    async with pg.acquire() as conn:
        if clickless:
            row = None
            ext_id = (extra_fields.get("transaction_id") or extra_fields.get("external_id")
                      or request.query_params.get("transaction_id")
                      or request.query_params.get("external_id"))
            if ext_id:
                # A repeated transaction/external id updates the same row
                row = await conn.fetchrow(
                    "SELECT * FROM conversions_data WHERE transaction_id = $1 "
                    "OR external_id = $1 ORDER BY received_at DESC LIMIT 1", str(ext_id))
            if row is None:
                # Attribution fallback: sub_id_1..5 uniquely matching one real
                # click in the last 7 days attaches to that click
                subs = {k: v for k, v in extra_fields.items()
                        if re.fullmatch(r"sub_id_[1-5]", k) and v}
                if subs:
                    conds = " AND ".join(f"{k} = ${i + 1}" for i, k in enumerate(subs))
                    candidates = await conn.fetch(
                        f"SELECT * FROM conversions_data "
                        f"WHERE click_id IS NOT NULL AND click_id <> 'none' "
                        f"AND received_at >= NOW() - INTERVAL '7 days' AND {conds} "
                        f"ORDER BY received_at DESC LIMIT 2", *subs.values())
                    if len(candidates) == 1:
                        row = candidates[0]
            if row is not None:
                row = await conn.fetchrow(JOINED_CLICK_QUERY, row["click_id"]) or row
                is_duplicate = await apply_to_row(conn, row)
                if not is_duplicate:
                    background_tasks.add_task(
                        notify_telegram_conversion_async, row["click_id"], status, payout_value, dict(row))
                    await fanout(conn, row, row["click_id"])
                    schedule_ch_sync(row["click_id"])
                return {"status": "ok", "click_id": row["click_id"],
                        "updated_status": status, "duplicate": is_duplicate,
                        "clickless": True, "attributed": True}
            await insert_row(conn, "none")
            return {"status": "ok", "click_id": "none", "updated_status": status,
                    "duplicate": False, "clickless": True, "attributed": False}

        row = await conn.fetchrow(JOINED_CLICK_QUERY, click_id)

        if not row:
            # Clicks from direct/redirect/landing flows never pass through /c,
            # so no conversions_data row exists for them — create one
            await insert_row(conn, click_id)
            schedule_ch_sync(click_id)
            return {"status": "ok", "click_id": click_id,
                    "updated_status": status, "duplicate": False}

        is_duplicate = await apply_to_row(conn, row)

        # Telegram conversion notification (respects settings toggle/statuses);
        # skipped for duplicate postbacks so chat stays spam-free
        if not is_duplicate:
            background_tasks.add_task(
                notify_telegram_conversion_async, click_id, status, payout_value, dict(row))
            await fanout(conn, row, click_id)
            schedule_ch_sync(click_id)

    return {"status": "ok", "click_id": click_id, "updated_status": status,
            "duplicate": is_duplicate}


async def _postback_request_params(request: Request) -> dict:
    """Incoming postback params: query string merged with a POST form/JSON body.

    G87: POST bodies (form-encoded or JSON) must behave exactly like the GET
    query string — external_id/transaction_id/sub_id extra fields and the data
    the rules engine sees all read from here. Query params win on key clashes.
    """
    params = dict(request.query_params)
    if request.method not in ("POST", "PUT", "PATCH"):
        return params
    ctype = (request.headers.get("content-type") or "").lower()
    body = None
    if "json" in ctype:
        try:
            body = await request.json()
        except Exception:
            body = None
    elif "form-urlencoded" in ctype or "multipart/form-data" in ctype:
        try:
            body = dict(await request.form())
        except Exception:
            body = None
    else:
        # No/unknown content type: accept a JSON body first, then a form body.
        try:
            body = await request.json()
        except Exception:
            try:
                body = dict(await request.form())
            except Exception:
                body = None
    if isinstance(body, dict):
        for k, v in body.items():
            params.setdefault(str(k), "" if v is None else str(v))
    return params


async def _process_postback(click_id: str, status: str, payout: str, request: Request,
                            background_tasks: BackgroundTasks, write: bool = True) -> dict:
    """Shared /pb core for GET, POST and HEAD.

    write=False (HEAD) runs the full validation/security/rules path but writes
    nothing and schedules no fanout — it only reports the GET status code.
    """
    status = normalize_status(status)
    # Built-ins (lead/sale/upsale/rejected/hold/trash) + configured custom statuses
    if status not in valid_conversion_statuses():
        raise HTTPException(status_code=400, detail="Invalid status")

    try:
        payout_value = float(str(payout).strip().replace(",", "."))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid payout format")
    # NaN/Infinity parse fine but poison every downstream sum/aggregate.
    if not math.isfinite(payout_value):
        raise HTTPException(status_code=400, detail="Invalid payout format")

    # Postback protection (optional secret key + IP allowlist, from Settings)
    sec = load_postback_security()
    allowed, deny_reason = check_postback_access(request, sec)
    if not allowed:
        log_track(f"🚫 Postback denied for {click_id}: {deny_reason}")
        raise HTTPException(status_code=403, detail=deny_reason)

    params = await _postback_request_params(request)

    # G85: global postback-processing rules run BEFORE the row is written and
    # before fanout; the first matching rule wins and `reject` is terminal.
    rule_data = dict(params)
    rule_data["click_id"] = click_id
    rule_result = apply_postback_rules(status, payout_value, rule_data, request)
    if rule_result["rejected"]:
        log_track(f"🚫 Postback rejected by rule for {click_id}: {rule_result['reason']}")
        return {"rejected": True, "reason": rule_result["reason"], "click_id": click_id}
    status = rule_result["status"]
    payout_value = rule_result["payout"]

    if not write:
        return {"status": "ok", "click_id": click_id, "updated_status": status, "head": True}

    extra_fields = {k: v for k, v in params.items() if k in PIXEL_EXTRA_FIELDS}
    return await record_conversion(click_id, status, payout_value, request, background_tasks,
                                   extra_fields or None, source="postback")


@app.get("/pb/{click_id}/{status}/{payout}")
@app.post("/pb/{click_id}/{status}/{payout}")
@app.head("/pb/{click_id}/{status}/{payout}")
async def postback_receive(click_id: str, status: str, payout: str, request: Request,
                           background_tasks: BackgroundTasks):
    is_head = request.method == "HEAD"
    result = await _process_postback(click_id, status, payout, request, background_tasks,
                                     write=not is_head)
    if is_head:
        # HEAD mirrors GET's status but has no body and writes nothing.
        return Response(status_code=200)
    if result.get("rejected"):
        return JSONResponse(status_code=200,
                            content={"status": "rejected", "reason": result["reason"],
                                     "click_id": result["click_id"]})
    return JSONResponse(content=result)


@app.get("/p/{campaign_alias}")
async def conversion_pixel(campaign_alias: str, request: Request, background_tasks: BackgroundTasks) -> Response:
    """Client-side conversion pixel (G16): same behavior as /pb, but addressed by
    campaign and returning a 1x1 GIF (or JSON with &fmt=json). The click id comes
    from ?click_id= or the aaa_cid first-party cookie set by /t.js / /t/collect."""
    json_mode = request.query_params.get("fmt") == "json"

    def respond(result=None, status_code=200, detail=None) -> Response:
        if json_mode:
            body = {"status": "error", "detail": detail} if detail is not None else result
            return JSONResponse(content=body, status_code=status_code, headers=open_cors_headers())
        if status_code == 200:
            return Response(content=PIXEL_GIF, status_code=200, media_type="image/gif",
                            headers=open_cors_headers())
        return Response(content=(detail or "error").encode(), status_code=status_code,
                        media_type="text/plain", headers=open_cors_headers())

    campaign = await find_campaign_for_tracking(campaign_alias)
    if not campaign:
        log_track(f"❌ Pixel conversion for unknown campaign '{campaign_alias}'")
        return respond(status_code=404, detail="Campaign not found")

    # GDPR opt-out (G79): acknowledge silently — 200/GIF, no conversion recorded
    if request.cookies.get(OPT_OUT_COOKIE):
        request.state.optout = True
        return respond(result={"status": "ok", "optout": True})

    click_id = (request.query_params.get("click_id")
                or request.cookies.get(DIRECT_COOKIE) or "").strip()
    extra_fields = {k: v for k, v in request.query_params.items() if k in PIXEL_EXTRA_FIELDS}
    # Clickless (G20): with no click id the pixel may still fire when it carries
    # attribution params (transaction/external id or sub_ids); bare pixels 400
    if not click_id and not extra_fields:
        return respond(status_code=400, detail="Missing click_id: pass ?click_id= or set the aaa_cid cookie")

    status = normalize_status(request.query_params.get("status") or "lead")
    if status not in valid_conversion_statuses():
        return respond(status_code=400, detail="Invalid status")

    try:
        payout_value = float(str(request.query_params.get("payout") or 0).strip().replace(",", "."))
    except (TypeError, ValueError):
        return respond(status_code=400, detail="Invalid payout format")
    # NaN/Infinity parse fine but poison every downstream sum/aggregate.
    if not math.isfinite(payout_value):
        return respond(status_code=400, detail="Invalid payout format")

    result = await record_conversion(click_id or "none", status, payout_value, request,
                                     background_tasks, extra_fields or None, source="pixel")
    return respond(result=result)


@app.get("/")
async def domain_page_default_campaign(request: Request) -> Response:
    log_track('🔁 Domain request')
    host = request.headers.get("host")
    campaign = await get_default_campaign_from_db(host)

    if campaign is None:
        return Response(content="404 Not Found", status_code=404, media_type="text/html")

    # Same bot-rule + opt-out gate as the alias routes (was missing here)
    blocked = await apply_tracking_gate(request, host or "default")
    if blocked is not None:
        return blocked

    # Execution records the chosen flow index on request.state and tracks the
    # ClickHouse row itself before returning the response.
    return await do_campaign_execution(campaign, request)


@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code in (404, 405):
        host = request.headers.get("host", "").lower().strip()
        if host:
            pg = app.state.pg
            async with pg.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM domains WHERE domain = $1", host)
                log_track("🔁 domain lookup for 404 handling")
                if row and row['handle_404'] == 'handle':
                    log_track('HANDLE 404')
                    return await domain_page_default_campaign(request)

        return render_404_html()
    # other errors by default
    return Response(content=str(exc.detail), status_code=exc.status_code)


DAYS_OF_WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def is_group_filter_format(filters) -> bool:
    """True when filters uses the group format (dict with groups, or a list of group dicts)."""
    if isinstance(filters, dict):
        return "groups" in filters
    if isinstance(filters, list):
        return any(isinstance(f, dict) and "conditions" in f for f in filters)
    return False


def _filter_field_value(meta: dict, field: str):
    """Raw meta value for a filter field; time fields are computed server-side."""
    if field == "hour_of_day":
        return datetime.now().hour
    if field == "day_of_week":
        return DAYS_OF_WEEK[datetime.now().weekday()]
    return meta.get(field)


def _eval_condition(meta: dict, cond: dict) -> bool:
    """Evaluate one group-format condition {field, operator, value, invert}."""
    field = cond.get("field") or cond.get("key") or cond.get("parameter") or ""
    op = (cond.get("operator") or "equals").lower()
    raw = _filter_field_value(meta, field)
    val = str(cond.get("value", "")).strip()

    exists = raw is not None and str(raw) != ""
    if op == "exists":
        passed = exists
    elif op == "not_exists":
        passed = not exists
    elif not exists:
        passed = False
    elif isinstance(raw, bool):
        truth = val.lower() in ("true", "1", "yes", "on")
        passed = raw != truth if op == "not_equals" else raw == truth
        if op not in ("equals", "not_equals"):
            passed = False
    else:
        ctx = str(raw)
        if op == "equals":
            passed = ctx == val
        elif op == "not_equals":
            passed = ctx != val
        elif op == "contains":
            passed = val in ctx
        elif op == "not_contains":
            passed = val not in ctx
        elif op == "starts_with":
            passed = ctx.startswith(val)
        elif op == "ends_with":
            passed = ctx.endswith(val)
        elif op == "regex":
            try:
                passed = re.search(val, ctx) is not None
            except re.error:
                passed = False
        elif op in ("greater", "less", "greater_eq", "less_eq"):
            try:
                a, b = float(ctx), float(val)
                passed = {"greater": a > b, "less": a < b,
                          "greater_eq": a >= b, "less_eq": a <= b}[op]
            except (TypeError, ValueError):
                passed = False
        elif op == "in":
            passed = ctx in [x.strip() for x in val.split(",") if x.strip()]
        elif op == "not_in":
            passed = ctx not in [x.strip() for x in val.split(",") if x.strip()]
        elif op == "cidr":
            passed = False
            try:
                addr = ipaddress.ip_address(ctx)
            except ValueError:
                addr = None
            if addr is not None:
                for net_raw in val.split(","):
                    net_raw = net_raw.strip()
                    if not net_raw:
                        continue
                    try:
                        if addr in ipaddress.ip_network(net_raw, strict=False):
                            passed = True
                            break
                    except ValueError:
                        continue
        else:
            passed = False

    return (not passed) if cond.get("invert") else passed


def match_group_filters(meta: dict, filters) -> bool:
    """Group-format matching: groups of conditions combined per the top-level combinator.

    Accepts {"combinator": "and|or", "groups": [{logic, conditions}]} or a plain
    list of groups (optionally with a leading {"combinator": "..."} marker item).
    Empty filters / empty groups match everything.
    """
    if isinstance(filters, dict):
        groups = filters.get("groups") or []
        combinator = str(filters.get("combinator") or "and").lower()
    else:
        groups = []
        combinator = "and"
        for f in filters or []:
            if not isinstance(f, dict):
                continue
            if set(f.keys()) == {"combinator"}:
                combinator = str(f.get("combinator") or "and").lower()
            else:
                groups.append(f)

    if not groups:
        return True

    results = []
    for g in groups:
        conditions = g.get("conditions") or []
        if not conditions:
            results.append(True)
            continue
        if str(g.get("logic") or "and").lower() == "or":
            results.append(any(_eval_condition(meta, c) for c in conditions))
        else:
            results.append(all(_eval_condition(meta, c) for c in conditions))
    return any(results) if combinator == "or" else all(results)


def match_flow_filters(meta: dict, filters, request: Request) -> bool:
    """Dispatch legacy flat lists to check_filters, group formats to match_group_filters."""
    if not filters:
        return True
    if is_group_filter_format(filters):
        return match_group_filters(meta, filters)
    return check_filters(meta, filters, request)


async def match_flow_filters_async(meta: dict, filters, request: Request) -> bool:
    """Event-loop-safe filter evaluation.

    User-supplied regex conditions can catastrophic-backtrack; the evaluation
    runs in a worker thread with a 2s ceiling and a timeout counts as
    no-match, so one hostile filter can never hang request handling.
    """
    if not filters:
        return True
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(match_flow_filters, meta, filters, request),
            timeout=2.0)
    except asyncio.TimeoutError:
        log_track("⏱ Flow-filter evaluation timed out (2s); treating as no-match")
        return False


def schedule_matches(schedule: dict) -> bool:
    """Dayparting: flow eligible only when local time (in schedule.timezone) fits."""
    if not isinstance(schedule, dict):
        return True
    try:
        tz = ZoneInfo((schedule.get("timezone") or "UTC").strip() or "UTC")
    except Exception:
        from datetime import timezone as _tz
        tz = _tz.utc
    now = datetime.now(tz)

    days = schedule.get("days")
    if days:
        if DAYS_OF_WEEK[now.weekday()] not in [str(d).lower()[:3] for d in days]:
            return False

    hours = schedule.get("hours")
    if hours in (None, [], {}):
        return True
    if isinstance(hours, dict):
        try:
            h_from, h_to = int(hours.get("from")), int(hours.get("to"))
        except (TypeError, ValueError):
            return True
        if h_from <= h_to:
            return h_from <= now.hour <= h_to
        return now.hour >= h_from or now.hour <= h_to  # overnight window
    try:
        return now.hour in [int(h) for h in hours]
    except (TypeError, ValueError):
        return True


async def flow_click_counts(campaign_id: int, flow_index: int) -> dict:
    """Click counts for a flow from ClickHouse (per hour / per day / total).

    On a query error returns {} so a ClickHouse hiccup never blocks traffic.
    """
    ch = acquire_ch()
    failed = False
    try:
        base = (f"FROM clicks_data WHERE campaign_id = {int(campaign_id)} "
                f"AND flow_index = {int(flow_index)}")

        def _run():
            hour_c = ch.query(f"SELECT count() {base} AND received_at >= now() - INTERVAL 1 HOUR").result_rows[0][0]
            day_c = ch.query(f"SELECT count() {base} AND received_at >= today()").result_rows[0][0]
            total_c = ch.query(f"SELECT count() {base}").result_rows[0][0]
            return hour_c, day_c, total_c

        hour_c, day_c, total_c = await asyncio.to_thread(_run)
        return {"hour": hour_c, "day": day_c, "total": total_c}
    except Exception as e:
        failed = True
        log_track(f"❌ Cap count query failed for campaign {campaign_id} flow {flow_index}: {e}")
        return {}
    finally:
        release_ch(ch, failed=failed)


def cap_exceeded(caps: dict, counts: dict) -> bool:
    """Cap of n means the n-th click passes; the (n+1)-th is already over the cap."""
    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    for cap_key, count_key in (("per_hour", "hour"), ("per_day", "day"), ("total", "total")):
        limit = as_int(caps.get(cap_key))
        if limit > 0 and as_int(counts.get(count_key)) >= limit:
            return True
    return False


# ─── Offer daily conversion caps + overflow (G4) ───────────────────
async def offer_state(pg, cache: dict, offer_id) -> tuple:
    """(status, archived) for an offer, cached per request."""
    try:
        offer_id = int(offer_id)
    except (TypeError, ValueError):
        return None, None
    if offer_id not in cache:
        async with pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, archived FROM offers WHERE id = $1", offer_id)
        cache[offer_id] = (row["status"], row["archived"]) if row else (None, None)
    return cache[offer_id]


async def offer_cap_state(pg, cache: dict, offer_id) -> tuple:
    """(daily_conversions_cap, overflow_offer_id) for an offer, cached per request."""
    try:
        offer_id = int(offer_id)
    except (TypeError, ValueError):
        return None, None
    if offer_id not in cache:
        async with pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT daily_conversions_cap, overflow_offer_id FROM offers WHERE id = $1",
                offer_id)
        cache[offer_id] = (row["daily_conversions_cap"], row["overflow_offer_id"]) if row else (None, None)
    return cache[offer_id]


async def offer_conversions_today(pg, cache: dict, offer_id) -> int:
    """Today's confirmed conversions (sale/upsale) for an offer, cached per request.

    Counted straight from conversions_data.offer_id (the column exists there),
    not via a ClickHouse clicks join — a conversions row is the source of truth
    for a paid conversion.
    """
    try:
        offer_id = int(offer_id)
    except (TypeError, ValueError):
        return 0
    if offer_id not in cache:
        async with pg.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT count(*) AS n FROM conversions_data
                WHERE offer_id = $1 AND status IN ('sale', 'upsale')
                  -- Rows are stamped with Python utcnow() — anchor the day window
                  -- to UTC, not the server's local date.
                  AND received_at >= (NOW() AT TIME ZONE 'UTC')::date
                """, offer_id)
        cache[offer_id] = row["n"] if row else 0
    return cache[offer_id]


# ─── Visitor stickiness (A/B binding) ─────────────────────────────
BIND_COOKIE = "aaa_bind"
BIND_TTL_SECONDS = 30 * 24 * 3600
# G55 click-date attribution: first-party visitor id, set on the campaign hit
# and read back on the click-out, so the ClickHouse click row and the Postgres
# conversion row share one join key.
VISITOR_COOKIE = "aaa_vid"
VISITOR_TTL_SECONDS = 180 * 24 * 3600


def _load_or_create_bind_secret() -> bytes:
    """Stable per-installation bind secret persisted in the settings table.

    Generated once, then reused across restarts and workers (a per-process
    random secret would invalidate every binding on deploy). Never derived
    from the DB password.
    """
    try:
        conn = pg_connect()
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE name = 'aaa_bind_secret'")
        row = cur.fetchone()
        if row and row[0]:
            conn.close()
            return row[0].encode()
        value = secrets.token_urlsafe(32)
        cur.execute(
            "INSERT INTO settings (name, value) VALUES ('aaa_bind_secret', %s) "
            "ON CONFLICT (name) DO NOTHING", (value,))
        conn.commit()
        cur.execute("SELECT value FROM settings WHERE name = 'aaa_bind_secret'")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return row[0].encode()
    except Exception as e:
        log_track(f"Bind secret load error: {e}")
    # Fail closed: no constant fallback secret — a known secret would make
    # every stickiness cookie forgeable. Callers skip binding entirely until
    # the settings row is readable.
    return None


def _bind_secret() -> bytes:
    env = os.environ.get("AAA_BIND_SECRET")
    if env:
        return env.encode()
    cached = getattr(app.state, "_bind_secret", None)
    if cached is None:
        cached = _load_or_create_bind_secret()
        # A None result (settings read failed) is NOT cached — the next hit
        # retries instead of staying fail-closed forever.
        app.state._bind_secret = cached
    return cached


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def campaign_routing_hash(config: dict, distribution_mode: str) -> str:
    """Hash of the routing-relevant config — editing flows invalidates bindings.

    `weight` is excluded: weight tweaks (including the AI optimizer's periodic
    reweighting) must not reset already-bound visitors. Every other flow field
    (offer/landing/schema/position/type/filters/enabled) still feeds the hash.
    """
    flows = [{k: v for k, v in flow.items() if k != "weight"}
             for flow in config.get("flows", [])]
    payload_obj = {"flows": flows, "redirect_mode": distribution_mode}
    # G10: funnel mode replaces flows at execution time, so the funnel config
    # is part of the routing hash too — editing steps/names resets every
    # bound visitor to step 0 (the step rides in the same bind cookie).
    funnel_cfg = config.get("funnel")
    if isinstance(funnel_cfg, dict) and funnel_cfg.get("enabled"):
        payload_obj["funnel"] = funnel_cfg
    payload = json.dumps(payload_obj, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def make_bind_cookie(campaign_id: int, flow_index: int, offer, landing, routing_hash: str,
                     step: int = None) -> str:
    # Fail closed: with no usable secret (settings read failed) no binding is
    # issued — never a cookie signed with a constant.
    secret = _bind_secret()
    if not secret:
        log_track("⚠ Bind secret unavailable — binding cookie skipped")
        return ""
    payload = {"cid": int(campaign_id), "fi": int(flow_index), "offer": offer, "landing": landing,
               "exp": int(datetime.utcnow().timestamp()) + BIND_TTL_SECONDS, "h": routing_hash}
    # G10 funnel campaigns: "st" is the visitor's current step index. Absent on
    # non-funnel cookies (backward compatible) and on step 0.
    if step is not None:
        payload["st"] = int(step)
    raw = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def parse_bind_cookie(request: Request, campaign_id: int, routing_hash: str):
    """Return the validated binding payload, or None (bad sig/expired/other campaign/stale config)."""
    # Fail closed: no usable secret → never accept a binding signed with one.
    secret = _bind_secret()
    if not secret:
        return None
    raw_cookie = request.cookies.get(BIND_COOKIE)
    if not raw_cookie or "." not in raw_cookie:
        return None
    raw, sig = raw_cookie.rsplit(".", 1)
    expected = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()
    # bytes, not str: a non-ASCII cookie value would raise TypeError (500).
    if not hmac.compare_digest(sig.encode(), expected.encode()):
        return None
    try:
        payload = json.loads(_b64url_decode(raw))
    except Exception:
        return None
    try:
        if int(payload.get("cid")) != int(campaign_id):
            return None
        if int(payload.get("exp") or 0) < int(datetime.utcnow().timestamp()):
            return None
    except (TypeError, ValueError):
        return None
    if payload.get("h") != routing_hash:
        return None
    return payload


def _bound_flow_still_valid(bound: dict, sorted_flows: list) -> bool:
    fi = bound.get("fi")
    if not isinstance(fi, int) or isinstance(fi, bool) or fi < 0 or fi >= len(sorted_flows):
        return False
    flow = sorted_flows[fi]
    return bool(flow and flow.get("enabled"))


def check_filters(meta: dict, filters: list, request: Request) -> bool:
    result = None

    for idx, f in enumerate(filters):
        key = f.get("key")
        op = f.get("operator")
        val = str(f.get("value", "")).strip()
        condition = f.get("condition", "").lower()

        ctx_val = str(meta.get(key, "")).strip()

        match op:
            case "equals":
                passed = ctx_val == val
            case "not_equals":
                passed = ctx_val != val
            case "contains":
                passed = val in ctx_val
            case "not_contains":
                passed = val not in ctx_val
            case "starts_with":
                passed = ctx_val.startswith(val)
            case "ends_with":
                passed = ctx_val.endswith(val)
            case "greater":
                try:
                    passed = float(ctx_val) > float(val)
                except:
                    passed = False
            case "less":
                try:
                    passed = float(ctx_val) < float(val)
                except:
                    passed = False
            case "in":
                passed = ctx_val in val.split(",")
            case "not_in":
                passed = ctx_val not in val.split(",")
            case _:
                passed = False

        if idx == 0 or condition == "":
            result = passed
        elif condition == "and":
            result = result and passed
        elif condition == "or":
            result = result or passed

    return bool(result)


def _postback_rule_payout(action: dict, current: float) -> float:
    """Resolve a set_payout action value.

    `action.value` is an absolute payout unless `action.mode == "multiplier"`,
    in which case it is a factor applied to the current payout (payout * value).
    A missing/non-numeric value leaves the payout unchanged.
    """
    try:
        if str(action.get("mode") or "").lower() == "multiplier":
            return float(current) * float(action.get("value"))
        return float(action.get("value"))
    except (TypeError, ValueError):
        return current


def apply_postback_rules(status: str, payout_value: float, data: dict,
                         request: Request) -> dict:
    """Evaluate the global postback-processing rules (settings.postback_rules).

    Runs on every inbound /pb postback BEFORE the conversion row is written and
    before fanout. Conditions reuse the flow-filter engine (check_filters) over
    the postback data dict — status, payout, click_id, sub_id_1..10 and any
    passthrough params. First matching rule wins; a terminal `reject` action
    stops the evaluation. Returns {"rejected", "reason", "status", "payout"}.
    """
    result = {"rejected": False, "reason": "", "status": status, "payout": payout_value}
    rules = load_postback_rules()
    if not rules:
        return result

    meta = {str(k): ("" if v is None else str(v)) for k, v in dict(data or {}).items()}
    meta["status"] = status
    meta["payout"] = payout_value

    for rule in rules:
        if rule.get("enabled") is False:
            continue
        filters = [{
            "key": c.get("field") if c.get("field") is not None else c.get("key"),
            "operator": c.get("operator"),
            "value": c.get("value"),
            "condition": c.get("condition") or "and",
        } for c in (rule.get("conditions") or []) if isinstance(c, dict)]
        if filters and not check_filters(meta, filters, request):
            continue

        action = rule.get("action") or {}
        atype = str(action.get("type") or "").lower()
        if atype == "reject":
            result["rejected"] = True
            result["reason"] = str(rule.get("name") or "Rejected by postback rule")
            return result
        if atype == "set_status":
            new_status = normalize_status(action.get("value"))
            # Only remap to a status the engine accepts — an unknown value would
            # otherwise poison the later INSERT/UPDATE against the status enum.
            if new_status and new_status in valid_conversion_statuses():
                result["status"] = new_status
                meta["status"] = new_status
        elif atype == "set_payout":
            result["payout"] = _postback_rule_payout(action, result["payout"])
            meta["payout"] = result["payout"]

    return result


def _sample_allows(click_id, percent) -> bool:
    """G86 deterministic traffic-source sampling.

    Empty/unset percent = forward everything (current behavior). Otherwise the
    same click_id always yields the same decision — md5(click_id) mod 100,
    never RNG — so a retried postback cannot alternate. percent >= 100 always
    forwards, <= 0 never does.
    """
    if percent is None or (isinstance(percent, str) and not percent.strip()):
        return True
    try:
        pct = float(percent)
    except (TypeError, ValueError):
        return True
    if pct >= 100:
        return True
    if pct <= 0:
        return False
    digest = hashlib.md5(str(click_id or "").encode()).hexdigest()
    return (int(digest, 16) % 100) < pct


def get_params_id_mapping_from_campaign(campaign: dict) -> list:
    config_str = campaign.get("config")
    if not config_str:
        return []

    try:
        config = json.loads(config_str)
        return config.get("paramsIdMapping", [])
    except json.JSONDecodeError:
        return []


def parse_campaign_config(campaign) -> dict:
    """Safe parse of campaign['config'] — NULL or garbage JSON behaves as {}.

    A visitor must never see a 500 because an operator saved a broken/empty
    config; the empty config falls through to the normal fallback/404 handling.
    """
    try:
        cfg = json.loads(campaign["config"] or "{}")
    except (TypeError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _meta_refresh_html(url: str) -> Response:
    """The bare meta-refresh document (no secondary-domain hop)."""
    import html as html_module
    safe_url = html_module.escape(url, quote=True)
    html_doc = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Redirecting...</title>
    <meta name="referrer" content="no-referrer">
    <meta http-equiv="refresh" content="0; url={safe_url}">
</head>
<body>
    <p style="font-family: sans-serif; text-align: center; margin-top: 40vh;">Continue...</p>
</body>
</html>"""
    return HTMLResponse(html_doc)


def hide_referrer_hop_url(request: Request, url: str) -> str:
    """Secondary referrer-hiding domain: when `tracking.referrer_hiding_domain`
    is set (and differs from the current host), the hide-referrer hop is served
    from that domain instead of the campaign's own host, so the destination
    never sees the campaign domain. Empty/unset = no hop (current behavior)."""
    hiding = str(load_tracking_settings().get("referrer_hiding_domain") or "").strip()
    if not hiding:
        return ""
    current_host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    if hiding.split(":")[0].strip().lower() == current_host:
        return ""
    return f"https://{hiding}/__hide_referrer?" + urlencode({"u": url})


def meta_refresh_redirect(url: str, request: Request = None) -> Response:
    """Redirect via an HTML meta refresh so the browser sends no referrer.

    With a configured referrer-hiding domain, the browser is first sent (302)
    to that domain's /__hide_referrer hop, which then serves the meta refresh —
    the offer never sees the campaign's own host in the referrer chain.
    """
    if request is not None:
        hop = hide_referrer_hop_url(request, url)
        if hop:
            return RedirectResponse(hop, status_code=302)
    return _meta_refresh_html(url)


@app.get("/__hide_referrer")
async def hide_referrer_hop(request: Request) -> Response:
    """Serves the meta-refresh document for the secondary hiding domain.

    The destination is validated to an http(s) URL so this endpoint can't be
    turned into a javascript:/data: redirect gadget.
    """
    target = (request.query_params.get("u") or "").strip()
    if not target.lower().startswith(("http://", "https://")):
        return render_404_html()
    return _meta_refresh_html(target)


# ─── Per-flow delivery actions ─────────────────────────────────────
# Optional flow.action decides HOW the visitor reaches the destination URL:
#   redirect  — current 302 / meta-refresh behavior (default, backward compatible)
#   iframe    — full-viewport iframe pointing at the URL
#   form_post — auto-submitting POST form to the URL
#   curl      — server-side fetch of the URL, its response served to the visitor
#   show_html — serve flow.html verbatim (no destination needed)
#   none      — 200 empty body (pixel-only campaigns)
# Absent action == redirect. The actions apply at the two destination points:
# the redirect schema's redirect_url (execute_flow_schema) and the offer
# click-out /c/{alias}/{offer_id} (campaign_click, flow recovered from the
# signed aaa_bind cookie's fi). Landing schemas reach the offer URL only at
# click-out time, so their action naturally takes effect there. The direct
# schema has no action-aware destination — action is ignored there.

FLOW_ACTIONS = ("redirect", "iframe", "curl", "form_post", "show_html", "none")
CURL_ACTION_MAX_BYTES = 2 * 1024 * 1024


def iframe_action_response(url: str) -> Response:
    import html as html_module
    safe_url = html_module.escape(url, quote=True)
    html_doc = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Loading...</title></head>
<body style="margin:0">
<iframe src="{safe_url}" style="position:fixed;inset:0;width:100%;height:100%;border:0"></iframe>
</body>
</html>"""
    return HTMLResponse(html_doc)


def form_post_action_response(url: str) -> Response:
    import html as html_module
    safe_url = html_module.escape(url, quote=True)
    # offer.tokens has no token→POST-param mapping on the tracking plane
    # (substitution happens via {token} placeholders inside the offer URL), so
    # the already-substituted URL query string is submitted as the POST body.
    hidden_inputs = "".join(
        f'<input type="hidden" name="{html_module.escape(k, quote=True)}" '
        f'value="{html_module.escape(v, quote=True)}">'
        for k, v in parse_qsl(urlparse(url).query))
    html_doc = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Loading...</title></head>
<body>
<form method="post" action="{safe_url}">
{hidden_inputs}
</form>
<script>document.forms[0].submit()</script>
</body>
</html>"""
    return HTMLResponse(html_doc)


async def curl_action_response(url: str, fallback_redirect) -> Response:
    if not url.startswith(("http://", "https://")):
        return HTMLResponse(
            "<h1>400 Bad Request</h1><p>The curl action requires an http(s) destination URL.</p>",
            status_code=400)
    try:
        # v1 serves the fetched body as-is — no HTML rewriting of links/assets.
        # verify=False: destinations are operator-configured URLs on their own
        # infra, where self-signed certificates are common.
        async with httpx.AsyncClient(follow_redirects=True, timeout=10, verify=False) as client:
            upstream = await client.get(url)
        body = upstream.content[:CURL_ACTION_MAX_BYTES]
        media_type = upstream.headers.get("content-type", "text/html; charset=utf-8")
        return Response(content=body, media_type=media_type)
    except Exception as e:
        log_track(f"❌ curl action fetch failed for '{url[:120]}': {e} — falling back to redirect")
        return fallback_redirect(url)


async def flow_action_response(url: str, flow: dict, fallback_redirect) -> Response:
    """Deliver `url` per the flow's action. Absent/unknown action = redirect.

    `fallback_redirect` builds the legacy redirect response — used for the
    redirect action itself and as the curl fetch-failure fallback.
    """
    action = (flow or {}).get("action") or "redirect"
    if action == "redirect":
        return fallback_redirect(url)
    if action == "iframe":
        return iframe_action_response(url)
    if action == "form_post":
        return form_post_action_response(url)
    if action == "show_html":
        return HTMLResponse((flow or {}).get("html") or "ok")
    if action == "none":
        return Response(status_code=200)
    if action == "curl":
        return await curl_action_response(url, fallback_redirect)
    # Unknown action — keep the legacy redirect rather than break traffic.
    return fallback_redirect(url)


async def referrer_page_title(referrer: str) -> str:
    """Fetch the referring page and use its <title> as the keyword (cached)."""
    if not referrer or not referrer.startswith(("http://", "https://")):
        return None
    if referrer in _title_cache:
        return _title_cache[referrer]

    def _fetch():
        try:
            r = requests.get(referrer, timeout=5)
            m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.IGNORECASE | re.DOTALL)
            if m:
                return re.sub(r"\s+", " ", m.group(1)).strip()[:255] or None
        except Exception:
            pass
        return None

    title = await asyncio.to_thread(_fetch)
    _title_cache[referrer] = title
    if len(_title_cache) > 5000:
        _title_cache.clear()
    return title


def funnel_config_steps(config: dict):
    """G10: the funnel step list when funnel mode is active and valid, else None.

    While enabled, the funnel takes over execution and config.flows is ignored
    (the campaigns UI shows a warning banner in that state). An enabled funnel
    with no steps is treated as inactive so a broken config can't brick the
    campaign into a 404.
    """
    funnel = config.get("funnel")
    if not isinstance(funnel, dict) or not funnel.get("enabled"):
        return None
    steps = funnel.get("steps")
    if not isinstance(steps, list):
        return None
    steps = [s for s in steps if isinstance(s, dict)]
    if not steps:
        return None
    return steps


async def execute_funnel(campaign, request: Request, config: dict, meta_data: dict,
                         steps: list, routing_hash: str, stickiness: bool,
                         depth: int, track: bool) -> Response:
    """G10: serve the visitor's current funnel step and (re)issue the signed
    binding cookie carrying the step index ("st" payload key).

    The step advances only on a successful click-out at /c/{alias}/{offer_id}
    (campaign_click re-issues the cookie with st = min(current + 1, last)).
    Visitors at/past the last step re-see the last step on every repeat visit.
    Step state rides in the same bind cookie as flow stickiness, so a config
    edit (routing_hash change) resets every visitor to step 0 — the same
    stickiness contract as flows.

    The step is served through execute_flow_schema by mapping the step onto a
    synthetic flow: landing_only steps keep their schema; steps with offers run
    as a "multi" flow over the step's landing + offer pool, so landing HTML and
    the {offer} click-out substitution reuse the exact serving path.

    For funnel campaigns the ClickHouse flow_index IS the step index.
    """
    # Step from the binding cookie: absent/invalid "st" = step 0; a step index
    # at/past the end (steps shrank under a still-valid cookie — only possible
    # while the routing_hash hasn't changed) serves the last step again.
    # Unlike flow stickiness, the binding is ALWAYS issued for funnels — the
    # step cannot advance across visits without it.
    step_index = 0
    bound = parse_bind_cookie(request, campaign["id"], routing_hash)
    if bound is not None:
        st = bound.get("st")
        if isinstance(st, int) and not isinstance(st, bool) and st > 0:
            step_index = min(st, len(steps) - 1)

    step = steps[step_index]
    landing = step.get("landing")
    offers = step.get("offers") or []
    step_schema = step.get("schema") if step.get("schema") in ("landing_offer", "landing_only") \
        else ("landing_offer" if offers else "landing_only")
    # multi honors the whole offer pool (random pick + bound stickiness within
    # the step), which is exactly the landing_offer semantics for N offers.
    flow = {"name": step.get("name") or f"Step {step_index + 1}",
            "schema": "multi" if step_schema == "landing_offer" else "landing_only",
            "landing": landing, "offer": offers[0] if offers else None,
            "landings": [landing] if landing else [], "offers": offers}

    flow_indexes = getattr(request.state, "flow_indexes", None)
    if flow_indexes is None:
        flow_indexes = {}
        request.state.flow_indexes = flow_indexes
    flow_indexes[campaign["id"]] = step_index

    if config.get("hide_referrer"):
        def campaign_redirect(url):
            return meta_refresh_redirect(url, request)
    else:
        def campaign_redirect(url):
            return RedirectResponse(url)

    served = {"offer": None, "landing": None}
    # The parsed binding feeds ONLY the step index — it must not leak into the
    # schema execution (multi would re-serve the PREVIOUS step's bound
    # landing/offer). Landing/offer picks always come from the current step's
    # own pool.
    response = await execute_flow_schema(
        campaign, request, config, meta_data, flow, None, served, campaign_redirect,
        depth=depth)

    # Funnels always (re)issue the step binding cookie — independent of the
    # campaign's stickiness switch, which only governs flow A/B stickiness.
    if not getattr(request.state, "optout", False):
        offer_id = served["offer"] if served["offer"] is not None else flow.get("offer")
        landing_id = served["landing"] if served["landing"] is not None else landing
        bind_cookie = make_bind_cookie(campaign["id"], 0, offer_id, landing_id, routing_hash,
                                       step=step_index)
        if bind_cookie:
            response.set_cookie(
                BIND_COOKIE, bind_cookie,
                max_age=BIND_TTL_SECONDS, path="/", httponly=True, samesite="lax")

    if track and not getattr(request.state, "optout", False):
        await track_event(campaign, request)
    return response


async def do_campaign_execution(campaign, request: Request, depth: int = 0,
                                track: bool = True) -> Response:
    log_track(f"🔁 New campaign execution call for '{campaign}'")

    config = parse_campaign_config(campaign)
    flows = config.get("flows", [])

    pg = app.state.pg

    # Distribution mode: 'position' = first matching flow wins,
    # 'weight' = weighted random split across matching flows (% share per flow).
    distribution_mode = (campaign.get("redirect_mode") if hasattr(campaign, "get") else campaign["redirect_mode"]) or "position"

    # Visitor stickiness: bind visitors to their assigned flow/offer/landing
    stickiness = bool(config.get("stickiness"))

    # FORCED FLOWS FIRST
    sorted_flows = sorted(
        flows,
        key=lambda f: (
            0 if f.get("type") == "forced" else 1,  # forced first
            f.get("position", 9999)  # then by position
        )
    )

    paramsIdMapping = get_params_id_mapping_from_campaign(campaign)
    meta_data = await enrich_meta(request, paramsIdMapping)

    # Prior conversion status for THIS visitor+campaign — the field flows can
    # filter on (e.g. only re-show an offer to visitors whose last status was
    # "rejected"). Bounded TTL cache; empty when the visitor is unknown.
    visitor_id = meta_data.get("visitor_id") or request.cookies.get(VISITOR_COOKIE)
    if visitor_id:
        meta_data["conversion_status"] = await conversion_status_for(visitor_id, campaign["id"])
    else:
        meta_data.setdefault("conversion_status", "")

    # use the referring page's <title> as keyword when no keyword param came in
    if config.get("use_title_as_keyword") and not meta_data.get("keyword"):
        title = await referrer_page_title(meta_data.get("referrer"))
        if title:
            meta_data["keyword"] = title

    routing_hash = campaign_routing_hash(config, distribution_mode)

    # G10 multi-step funnels: an enabled funnel takes over execution entirely —
    # config.flows is ignored while funnel mode is on (the campaigns UI shows a
    # warning banner). Step state rides in the same signed aaa_bind cookie, so
    # a funnel config edit resets every bound visitor to step 0.
    funnel_steps = funnel_config_steps(config)
    if funnel_steps is not None:
        return await execute_funnel(campaign, request, config, meta_data, funnel_steps,
                                    routing_hash, stickiness, depth, track)

    # Stickiness: a valid binding for THIS campaign with a current config hash
    # wins — the bound flow is served directly without re-filtering.
    bound = None
    if stickiness:
        bound = parse_bind_cookie(request, campaign["id"], routing_hash)
        if bound is not None and not _bound_flow_still_valid(bound, sorted_flows):
            bound = None

    # Choose the flow to serve
    chosen = None
    chosen_index = -1
    chosen_override = None
    if bound is not None:
        chosen = sorted_flows[bound["fi"]]
        chosen_index = bound["fi"]
        # A bound visitor whose flow's offer is paused/archived loses the
        # binding and falls through to fresh selection — the same offer-state
        # gate the eligibility loop below applies.
        bound_offer = chosen.get("offer")
        if bound_offer:
            bound_offer_cache: dict = {}
            o_status, o_archived = await offer_state(pg, bound_offer_cache, bound_offer)
            if o_status != "active" or o_archived:
                log_track(f"🚫 Bound flow offer {bound_offer} inactive — re-selecting for campaign {campaign['id']}")
                bound = None
                chosen = None
                chosen_index = -1
    if chosen is None:
        # Collect eligible flows: enabled + schedule open + caps open + filters passed
        eligible = []
        cap_state_cache: dict = {}
        conv_count_cache: dict = {}
        offer_state_cache: dict = {}
        for idx, flow in enumerate(sorted_flows):
            if not flow or not flow.get("enabled"):
                continue
            schedule = flow.get("schedule")
            if schedule and not schedule_matches(schedule):
                continue
            caps = flow.get("caps")
            if caps:
                counts = await flow_click_counts(campaign["id"], idx)
                if counts and cap_exceeded(caps, counts):
                    continue
            filters = flow.get("filters", [])
            if filters and not await match_flow_filters_async(meta_data, filters, request):
                continue
            # G4: offer daily conversion cap — count once per offer per request.
            # Over cap → swap to the overflow offer when configured, otherwise
            # the flow is ineligible and routing continues to the next flow.
            offer_override = None
            flow_offer = flow.get("offer")
            if flow_offer:
                # Paused/archived offers receive no traffic — skip the flow
                # (same as capped-without-overflow).
                offer_status, offer_archived = await offer_state(pg, offer_state_cache, flow_offer)
                if offer_status != "active" or offer_archived:
                    continue
                daily_cap, overflow = await offer_cap_state(pg, cap_state_cache, flow_offer)
                if daily_cap and await offer_conversions_today(pg, conv_count_cache, flow_offer) >= int(daily_cap):
                    if overflow:
                        o_cap, _ = await offer_cap_state(pg, cap_state_cache, overflow)
                        o_used = await offer_conversions_today(pg, conv_count_cache, overflow) if o_cap else 0
                        if o_cap and o_used >= int(o_cap):
                            continue
                        offer_override = overflow
                    else:
                        continue
            eligible.append((idx, flow, offer_override))

        if eligible:
            # Forced flows always win first (position order)
            forced = [(i, f, o) for i, f, o in eligible if f.get("type") == "forced"]
            if forced:
                chosen_index, chosen, chosen_override = forced[0]
            elif distribution_mode == "weight":
                weights = []
                for _, f, _ in eligible:
                    try:
                        w = max(float(f.get("weight", 100) or 0), 0)
                    except (TypeError, ValueError):
                        w = 0
                    weights.append(w)
                if sum(weights) > 0:
                    chosen_index, chosen, chosen_override = random.choices(eligible, weights=weights, k=1)[0]
                else:
                    chosen_index, chosen, chosen_override = eligible[0]
            else:
                chosen_index, chosen, chosen_override = eligible[0]

    # Record the chosen flow index so track_event can tag the ClickHouse row
    # (used by click caps). flow_index 0 = no flow chosen / legacy row.
    flow_indexes = getattr(request.state, "flow_indexes", None)
    if flow_indexes is None:
        flow_indexes = {}
        request.state.flow_indexes = flow_indexes
    if chosen is not None:
        flow_indexes[campaign["id"]] = chosen_index

    # Nothing matched → campaign fallback URL if set, else 404
    if chosen is None:
        fallback_url = (config.get("fallback_url") or "").strip()
        if fallback_url:
            if config.get("hide_referrer"):
                response = meta_refresh_redirect(fallback_url, request)
            else:
                response = RedirectResponse(fallback_url)
        else:
            response = render_404_html()
    else:
        flow = chosen
        # G4: offer capped → the overflow offer executes with the same flow schema
        if chosen_override is not None:
            flow = dict(flow)
            flow["offer"] = chosen_override
        # Respect per-campaign "hide referrer" on outbound redirects
        if config.get("hide_referrer"):
            def campaign_redirect(url):
                return meta_refresh_redirect(url, request)
        else:
            def campaign_redirect(url):
                return RedirectResponse(url)

        served = {"offer": None, "landing": None}
        response = await execute_flow_schema(
            campaign, request, config, meta_data, flow, bound, served, campaign_redirect,
            depth=depth)

        # Stickiness: (re)issue the signed binding cookie — a fresh assignment
        # overwrites any previous binding; bound visits just get their expiry renewed.
        # Opted-out visitors (G79) get no cookies at all.
        if stickiness and not getattr(request.state, "optout", False):
            offer_id = served["offer"] if served["offer"] is not None else flow.get("offer")
            landing_id = served["landing"] if served["landing"] is not None else flow.get("landing")
            bind_cookie = make_bind_cookie(campaign["id"], chosen_index, offer_id, landing_id, routing_hash)
            if bind_cookie:
                response.set_cookie(
                    BIND_COOKIE, bind_cookie,
                    max_age=BIND_TTL_SECONDS, path="/", httponly=True, samesite="lax")

    # Track THIS campaign's own execution (flow index was recorded above under
    # this campaign's id). Inner redirect_campaign levels track themselves when
    # they execute; opted-out visitors are never recorded. ClickHouse failures
    # never kill the visitor's response — track_event logs and swallows them.
    if not getattr(request.state, "optout", False):
        # G55: maintain the first-party visitor id across hits so the CH click
        # row and the later conversion row share a join key for click-date
        # attribution in the conversions log.
        vid = request.cookies.get(VISITOR_COOKIE) or str(uuid.uuid4())
        request.state.aaa_vid = vid
        response.set_cookie(
            VISITOR_COOKIE, vid,
            max_age=VISITOR_TTL_SECONDS, path="/", httponly=True, samesite="lax")
    if track and not getattr(request.state, "optout", False):
        await track_event(campaign, request)
    return response


MAX_REDIRECT_DEPTH = 3  # redirect_campaign chains deeper than this get a 404/fallback


async def execute_flow_schema(campaign, request: Request, config: dict, meta_data: dict,
                              flow: dict, bound: dict, served: dict, campaign_redirect,
                              depth: int = 0) -> Response:
    """Execute one chosen flow's schema. Populates `served` with the concrete
    offer/landing ids for stickiness binding (bound ids override random picks)."""
    pg = app.state.pg
    schema = flow.get("schema")

    # SCHEMA: direct
    if schema == "direct":
        served["offer"] = flow.get("offer")
        offer_url = await get_real_offer_url(flow.get("offer"))
        click_id = meta_data.get("click_id") or generate_click_id()
        if "{click_id}" in offer_url:
            offer_url = offer_url.replace("{click_id}", click_id)
        else:
            offer_url = merge_query_params(offer_url, {"click_id": click_id})
        # G87: {_md5} = md5 hex of the click id in offer URLs.
        if "{_md5}" in offer_url:
            offer_url = offer_url.replace("{_md5}", hashlib.md5(str(click_id).encode()).hexdigest())
        if config.get("send_query_params"):
            offer_url = merge_query_params(offer_url, request.query_params)
        if config.get("send_se_referrer") and meta_data.get("referrer"):
            offer_url = merge_query_params(offer_url, {"referrer": meta_data["referrer"]})
        return campaign_redirect(offer_url)

    # SCHEMA: landing → offer
    elif schema == "landing_offer":
        landing = flow.get("landing")
        offer_id = flow.get("offer")
        served["offer"] = offer_id
        served["landing"] = landing
        if landing:
            async with pg.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM landings WHERE id = $1", landing)
                if row:
                    landing_folder = row["folder"]
                    offer_url = await get_offer_click_url(
                        campaign['alias'], offer_id, row['id'],
                        click_id=meta_data.get("click_id"),
                        passthrough=mapped_passthrough_params(config, meta_data))
                    return await show_landing(landing_folder, offer_url)
        return render_404_html()

    # SCHEMA: landing only
    elif schema == "landing_only":
        landing = flow.get("landing")
        served["landing"] = landing
        if landing:
            async with pg.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM landings WHERE id = $1", landing)
                if row:
                    landing_folder = row["folder"]
                    return await show_landing(landing_folder)
        return render_404_html()

    # SCHEMA: multi
    elif schema == "multi":
        # Bound visits keep their originally assigned landing/offer (A/B stability)
        landing_id = bound.get("landing") if bound else None
        offer_id = bound.get("offer") if bound else None
        landings = flow.get("landings") or []
        offers = flow.get("offers") or []
        # Empty pools make the flow ineligible — never crash on random.choice
        if landing_id is None and landings:
            landing_id = random.choice(landings)
        if offer_id is None and offers:
            offer_id = random.choice(offers)
        served["landing"] = landing_id
        served["offer"] = offer_id

        if landing_id:
            async with pg.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM landings WHERE id = $1", landing_id)
                if row:
                    landing_folder = row["folder"]
                    offer_url = await get_offer_click_url(
                        campaign['alias'], offer_id, row['id'],
                        click_id=meta_data.get("click_id"),
                        passthrough=mapped_passthrough_params(config, meta_data))
                    return await show_landing(landing_folder, offer_url)
        return render_404_html()

    # SCHEMA: redirect
    elif schema == "redirect":
        redirect_url = (flow.get("redirect_url") or "").strip()
        if not redirect_url:
            # Empty redirect URL makes the flow ineligible
            return render_404_html()
        if config.get("send_query_params"):
            redirect_url = merge_query_params(redirect_url, request.query_params)
        if config.get("send_se_referrer") and meta_data.get("referrer"):
            redirect_url = merge_query_params(redirect_url, {"referrer": meta_data["referrer"]})
        # Per-flow delivery action (iframe/form_post/curl/show_html/none);
        # absent action = legacy redirect via campaign_redirect.
        return await flow_action_response(redirect_url, flow, campaign_redirect)

    # SCHEMA: redirect_campaign ++++
    elif schema == "redirect_campaign":
        campaign_id = flow.get("redirect_campaign")

        if campaign_id and depth < MAX_REDIRECT_DEPTH:
            async with pg.acquire() as conn:
                target_campaign = await conn.fetchrow("SELECT * FROM campaigns WHERE id = $1", campaign_id)
                if target_campaign:
                    # The inner campaign executes (and tracks) its own level;
                    # the depth guard turns A→B→A loops into a 404/fallback.
                    return await do_campaign_execution(target_campaign, request, depth=depth + 1)
        return render_404_html()

    # SCHEMA: return_404 +++
    elif schema == "return_404":
        return render_404_html()

    # Default fallback
    return render_404_html()


def merge_query_params(url: str, params) -> str:
    """Merge extra params into a URL's query string (existing keys win)."""
    parsed = urlparse(url)
    query_params = dict(parse_qsl(parsed.query))
    for k, v in dict(params).items():
        query_params.setdefault(k, v)
    return urlunparse(parsed._replace(query=urlencode(query_params)))


async def get_offer_click_url(campaign_alias: str, offer_id: str, landing_id: str = None,
                              click_id: str = None, passthrough: dict = None) -> str:
    """Build the /c click-out URL.

    Carries ONLY what /c cannot reconstruct server-side when it re-enriches
    the request: the landing id, the visitor's click id and the
    paramsIdMapping passthrough tokens. The enriched meta (ip, user agent,
    referrer, cookies...) must never leak into URLs.
    """
    base_url = f"/c/{campaign_alias}/{offer_id}"
    query_params = {}

    if landing_id:
        query_params["l_id"] = landing_id

    if click_id:
        query_params["click_id"] = click_id

    if passthrough:
        for k, v in passthrough.items():
            if v is not None and k not in query_params:
                query_params[k] = v

    if query_params:
        return f"{base_url}?" + urlencode(query_params)

    return base_url


def mapped_passthrough_params(config: dict, meta_data: dict) -> dict:
    """paramsIdMapping token values present in the meta, for click-out passthrough."""
    out = {}
    for item in config.get("paramsIdMapping", []) or []:
        token = (item.get("token") or "").strip()
        if token and meta_data.get(token) is not None:
            out[token] = meta_data[token]
    return out


async def get_real_offer_url(offer_id: str, offer_vars: list = None) -> str:
    pg = app.state.pg
    async with pg.acquire() as conn:
        offer_row = await conn.fetchrow("SELECT * FROM offers WHERE id = $1", offer_id)
        # log_track(f"🔁 offer_row - '{offer_row}'")
        try:
            if offer_row:
                offer_url = offer_row["url"]
                if offer_vars:
                    for var in offer_vars:
                        offer_url = offer_url.replace(f"{{{var}}}", str(offer_row.get(var, "")))

                # G87: a {payout} macro in an offer URL resolves to this offer's
                # payout (payout=auto is the postback-URL equivalent).
                if "{payout}" in offer_url:
                    offer_payout = offer_row.get("payout")
                    offer_url = offer_url.replace(
                        "{payout}", str(offer_payout if offer_payout is not None else ""))

                # log_track(f"🔁 offer_url - '{offer_url}'")
                return offer_url
        except TypeError:
            log_track(f"❌ TypeError Offer Url Build: {offer_row}")
    return ""


async def track_event(campaign, request: Request, click: bool = None, extra_meta: dict = None):
    """ track events to ClickHouse

    click: when not None, the row's `click` flag is set explicitly (direct
    tracking records visits with click=False; classic redirects leave it NULL).
    extra_meta: extra validated (VALID_PARAMS) fields merged into the row.
    """

    # Prefetch/prerender hits never count as real visits — no ClickHouse row.
    if request_is_prefetch(request):
        log_track("🙈 Prefetch hit — skipping track_event row")
        return

    try:
        campaign_alias = campaign["alias"]
        log_track(f"🔁 New track data call for '{campaign_alias}'")
    except KeyError:
        msg = "❌ Campaign alias not found in track_event"
        log_track(msg)
        raise HTTPException(status_code=400, detail=msg)

    content_type = request.headers.get('content-type', '')
    cached_body = getattr(request.state, "_parsed_body", None)
    if cached_body is not None:
        query = cached_body
    elif content_type.startswith('application/x-www-form-urlencoded'):
        query = dict(await request.form())
    else:
        try:
            query = await request.json()
        except:
            query = {}
    # Non-dict bodies (JSON scalars/arrays) can't be keyed — normalize so the
    # `if key in query` membership test below never raises TypeError.
    if not isinstance(query, dict):
        query = {}

    config = parse_campaign_config(campaign)

    # Extract the mapping from config.paramsIdMapping
    mapping = {}
    for item in config.get("paramsIdMapping", []):
        param = item.get("parameter")
        if param:
            mapping[param] = param

    # Base record
    result_row = {
        "campaign_id": str(campaign["id"])
    }

    # Chosen flow index (set by do_campaign_execution) — powers click caps
    flow_indexes = getattr(request.state, "flow_indexes", {}) or {}
    try:
        result_row["flow_index"] = int(flow_indexes.get(campaign["id"], 0) or 0)
    except (TypeError, ValueError):
        result_row["flow_index"] = 0

    # Add the enriched fields
    meta_data = await enrich_meta(request, campaign.get("paramsIdMapping"))

    # use the referring page's <title> as keyword when no keyword param came in
    if config.get("use_title_as_keyword") and not meta_data.get("keyword"):
        title = await referrer_page_title(meta_data.get("referrer"))
        if title:
            meta_data["keyword"] = title

    # Request-derived meta may never set conversion/server fields — a crafted
    # ?status=sale&revenue=999 would otherwise fabricate conversions in the
    # ClickHouse row. Only the server-set paths below may touch these.
    _SERVER_ONLY_KEYS = ("status", "revenue", "profit", "is_bot")
    for k, v in meta_data.items():
        if k in VALID_PARAMS and k not in _SERVER_ONLY_KEYS:
            result_row[k] = v

    # G55: stamp the visitor id so click-date attribution can join this row
    # to the conversion recorded at click-out time. Runs after the meta merge
    # so a stale/empty meta value can't clobber it.
    vid = getattr(request.state, "aaa_vid", None) or request.cookies.get(VISITOR_COOKIE)
    if vid and not result_row.get("visitor_id"):
        result_row["visitor_id"] = str(vid)

    # Bot rule with "mark" action — flag the click but keep tracking it
    if getattr(request.state, "bot_marked", None):
        result_row["is_bot"] = True

    # Heuristic fraud score (monitoring-only — nothing is blocked here).
    score, verified_crawler, reasons = compute_fraud_score(
        visitor_key=visitor_key_for(request),
        ip=str(meta_data.get("ip") or ""),
        ua=str(meta_data.get("user_agent") or ""),
        isp=str(meta_data.get("isp") or ""),
        is_bot=bool(meta_data.get("is_bot")) or bool(getattr(request.state, "bot_marked", None)))
    if getattr(request.state, "honeypot_flagged", False):
        score, verified_crawler, reasons = 100, False, reasons + ["honeypot"]
    if getattr(request.state, "shield_watch", False):
        # Shield "allow" action: watch mode — track but force a high score.
        score, reasons = max(score, 50), reasons + ["shield_watch"]
    result_row["fraud_score"] = min(int(score), 100)
    if verified_crawler or score >= 70 or getattr(request.state, "shield_watch", False):
        result_row["is_bot"] = True
    if score >= 30:
        log_track(f"🕵 Fraud score {score} for '{campaign_alias}': {', '.join(reasons)}")

    # Live rows always carry an explicit bot flag — reports treat NULL as
    # human, but the fraud feed and regression checks expect a crisp false.
    result_row.setdefault("is_bot", False)

    # "Do not assign costs for bot clicks" (default off): a bot-flagged row
    # carries cost 0, so cost aggregations (SUM(cost) in clickHouse.py, no
    # is_bot filter) are unaffected without touching that module.
    if result_row.get("is_bot") and tracking_skip_bot_costs() and "cost" in result_row:
        result_row["cost"] = 0.0

    if click is not None:
        result_row["click"] = bool(click)

    if extra_meta:
        for k, v in extra_meta.items():
            if k in VALID_PARAMS and v is not None:
                result_row[k] = v

    # Apply the mapping and add the query parameters — same server-only guard
    # as the meta copy above (query values are request-derived too).
    for key in VALID_PARAMS:
        if key in query and key not in _SERVER_ONLY_KEYS:
            mapped_key = mapping.get(key, key)
            result_row[mapped_key] = query[key]

    # ❗ Drop every field whose value is None
    result_row = {k: v for k, v in result_row.items() if v is not None}

    # Money columns are Float32 — European decimals ("0,5") or garbage would
    # fail the whole insert, so normalize commas and drop the key on failure
    # (keeping the row).
    for money_key in ("cost", "revenue", "profit"):
        if money_key in result_row:
            try:
                result_row[money_key] = round(
                    float(str(result_row[money_key]).strip().replace(",", ".")), 6)
            except (TypeError, ValueError):
                result_row.pop(money_key, None)

    # G79 privacy: anonymize IPs at rest when settings.privacy.anonymize_ip is on
    # (IPv4 → last octet zeroed, e.g. 1.2.3.4 → 1.2.3.0; IPv6 → last 16 bits
    # zeroed). Default off.
    if load_privacy_settings().get("anonymize_ip"):
        ip_val = result_row.get("ip")
        if ip_val:
            try:
                addr = ipaddress.ip_address(str(ip_val))
                if addr.version == 4:
                    result_row["ip"] = ".".join(str(addr).split(".")[:3] + ["0"])
                else:
                    result_row["ip"] = str(ipaddress.IPv6Address((int(addr) >> 16) << 16))
            except ValueError:
                pass

    # Full client address (v4 or v6) for the ip_full String column — captured
    # AFTER the privacy mask above (so it never leaks what `ip` masked) but
    # BEFORE the IPv4 coercion below, which would otherwise erase v6 visitors.
    ip_full_val = result_row.get("ip")
    if ip_full_val:
        try:
            result_row["ip_full"] = str(ipaddress.ip_address(str(ip_full_val)))
        except ValueError:
            result_row["ip_full"] = ""
    else:
        result_row["ip_full"] = ""

    # The ClickHouse column is IPv4 — an IPv6/garbage value would fail the whole
    # insert, so store the type default (0.0.0.0) instead of dropping the row.
    ip_val = result_row.get("ip")
    if ip_val is not None:
        try:
            if ipaddress.ip_address(str(ip_val)).version != 4:
                result_row["ip"] = "0.0.0.0"
        except ValueError:
            result_row["ip"] = "0.0.0.0"

    # A ClickHouse failure must never kill the visitor's redirect — the response
    # was already computed by the caller. Log and move on.
    try:
        columns = list(result_row.keys())
        values = [list(result_row.values())]
        ch = acquire_ch()
        try:
            await asyncio.to_thread(ch.insert, "clicks_data", values, column_names=columns)
        except Exception:
            release_ch(ch, failed=True)
            raise
        release_ch(ch)
        # log_track(f"✅ Inserted into ClickHouse: {campaign_alias}")
    except Exception as e:
        log_track(f"❌ ClickHouse insert failed for campaign '{campaign_alias}': {str(e)}")


def fill_postback_template(url: str, mapping: dict) -> str:
    """Replace {key} placeholders in an admin-configured postback URL.

    Regex substitution, not str.format_map: unknown keys, format specs
    ({click_id:>10}), attribute/index accesses and stray braces all stay
    literal, and malformed template text can never raise (a raised exception
    here would kill the whole background-tasks queue for the conversion).
    """
    def _sub(match):
        token = match.group(1)
        # Only a bare {key} with a clean identifier name substitutes; anything
        # else — format specs ({click_id:>10}), attribute/index access, unknown
        # keys — stays literal so admin-authored text is never reinterpreted.
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token) and token in mapping:
            return str(mapping[token])
        return match.group(0)
    return re.sub(r"\{([^{}]*)\}", _sub, url)


def _to_unix_seconds(value) -> str:
    """Epoch seconds as a string from a datetime / ISO string / number, else ''."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        try:
            return str(int(value))
        except (TypeError, ValueError, OverflowError):
            return ""
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        return str(int((dt - datetime(1970, 1, 1)).total_seconds()))
    except Exception:
        return ""


def _postback_token_map(data: dict) -> dict:
    """Token map for {placeholder} substitution in postback URLs.

    Extends the row fields with G87 tokens: {_md5} (md5 hex of the click id),
    {status2} (secondary status when the row carries one, else ''), 
    {unixconversiontime} and event_1..event_30 (custom conversion event params,
    empty when the row stores none). Missing tokens resolve to empty string
    rather than staying literal; unknown tokens are left untouched by
    fill_postback_template.
    """
    tokens = {k: str(v) for k, v in dict(data or {}).items() if v is not None}
    tokens["_md5"] = hashlib.md5(str(data.get("click_id") or "").encode()).hexdigest()
    tokens.setdefault("status2", "")
    tokens["unixconversiontime"] = _to_unix_seconds(
        data.get("last_postback_at") or data.get("received_at")
        or data.get("conversion_time"))
    for i in range(1, 31):
        key = f"event_{i}"
        tokens.setdefault(key, str(data.get(key) or ""))
    return tokens


async def send_postback(url: str, data: dict, post: bool = False):
    VALID_PARAMS = [
        'payout', 'status', 'click_id', 'browser', 'campaign_id', 'city', 'connection_type', 'currency',
        'cost', 'country', 'utm_creative', 'utm_campaign', 'utm_source', 'device_type', 'external_id', 'ip',
        'is_bot', 'is_using_proxy', 'isp', 'keyword', 'landing_id', 'language', 'offer_id',
        'os', 'profit', 'referrer', 'region', 'revenue', 'status', 'sub_id_1', 'sub_id_2', 'sub_id_3',
        'sub_id_4', 'sub_id_5', 'sub_id_6', 'sub_id_7', 'sub_id_8', 'sub_id_9', 'sub_id_10',
        'traffic_source_name', 'url', 'visitor_id'
    ]

    # 🔒 Filtered parameters — None values dropped (str(None) serializes as
    # "None" in postback URLs/queries; the s2s path already excluded them).
    safe_data = {k: str(v) for k, v in data.items() if k in VALID_PARAMS and v is not None}

    # Templating lives inside try/except: a malformed admin-configured URL must
    # be logged and skipped, never abort the BackgroundTasks queue (Telegram →
    # campaign/source postbacks → ClickHouse sync for this conversion).
    try:
        tokens = _postback_token_map(data)
        filled_url = fill_postback_template(url, tokens)

        # `payout=auto` is a literal an admin may put where a payout number is
        # expected: it is replaced by the payout actually being fired (the offer
        # payout on campaign postbacks, the conversion payout otherwise). Scope:
        # postback URLs only — offer/lander URLs use their own builders.
        if "payout=auto" in filled_url:
            filled_url = filled_url.replace("payout=auto", f"payout={tokens.get('payout', '')}")

        # Redact: only the origin+path goes into the debug log, never the query
        # string (it carries click ids, tokens and payouts).
        redacted = filled_url.split("?")[0]
        log_track(f"<UNK> Sending Postback: {redacted}")

        async with httpx.AsyncClient(timeout=10) as client:
            if post:
                response = await client.post(filled_url, json=safe_data)
            else:
                # httpx REPLACES a URL's query string when params= is passed, which
                # silently dropped admin-authored macros from the URL. Merge the
                # row params into the templated URL instead (admin URL keys win).
                response = await client.get(merge_query_params(filled_url, safe_data))

        if response.status_code != 200:
            log_track(f"❌ Postback failed: {response.status_code} - {response.text}")

    except Exception as e:
        log_track(f"❌ Postback error: {str(e)}")


# ─── Privacy (G79): settings.privacy + opt-out cookie ──────────────
OPT_OUT_COOKIE = "aaa_optout"
OPT_OUT_TTL = 10 * 365 * 24 * 3600


def load_privacy_settings() -> dict:
    """Read the privacy block from the settings row (30s TTL cache).

    Shape: {"anonymize_ip": bool} — default off. The settings UI toggle is a
    separate deliverable; the engine only reads this key.
    """
    return _settings_block("privacy")


# ─── Tracking-plane toggles (prefetch / bot costs / hiding domain) ──
def load_tracking_settings() -> dict:
    """Read the tracking block from the settings row (30s TTL cache).

    Shape (all optional):
      {"prefetch_filter_enabled": bool (default True),
       "skip_bot_costs": bool (default False),
       "referrer_hiding_domain": str (default "")}
    """
    return _settings_block("tracking")


def tracking_prefetch_filter_enabled() -> bool:
    return bool(load_tracking_settings().get("prefetch_filter_enabled", True))


def tracking_skip_bot_costs() -> bool:
    return bool(load_tracking_settings().get("skip_bot_costs", False))


# ─── rDNS (reverse DNS) filter field ────────────────────────────────
# The PTR name of the client IP is resolved in a worker thread with a hard 2s
# ceiling so a hostile/unroutable IP can never block the event loop and stall
# the redirect path; results (including negatives) are memoized in a bounded,
# TTL'd process-local map. On failure/timeout the field is empty and filters
# simply don't match.
_rdns_cache: dict = {}
_RDNS_TTL = 300.0
_RDNS_CACHE_CAP = 10_000


def _rdns_lookup_sync(ip: str) -> str:
    try:
        name, _aliases, _addrs = socket.gethostbyaddr(str(ip))
        return name or ""
    except Exception:
        return ""


async def reverse_dns(ip: str) -> str:
    ip = (ip or "").strip()
    if not ip:
        return ""
    now = time.monotonic()
    hit = _rdns_cache.get(ip)
    if hit is not None and now < hit[0]:
        return hit[1]
    name = ""
    try:
        name = await asyncio.wait_for(
            asyncio.to_thread(_rdns_lookup_sync, ip), timeout=2.0)
    except asyncio.TimeoutError:
        log_track(f"⏱ rDNS lookup timed out (2s) for {ip}; field left empty")
        name = ""
    except Exception:
        name = ""
    if len(_rdns_cache) >= _RDNS_CACHE_CAP:
        _rdns_cache.clear()
    _rdns_cache[ip] = (now + _RDNS_TTL, name)
    return name


# ─── Prior conversion status filter field ───────────────────────────
# The visitor's LAST conversion status for THIS campaign, keyed on the
# first-party visitor cookie. A single bounded, TTL'd lookup keeps a hot
# redirect from hammering Postgres; a miss is cached too so repeated hits on a
# non-converting visitor stay cheap.
_conv_status_cache: dict = {}
_CONV_STATUS_TTL = 60.0
_CONV_STATUS_CACHE_CAP = 20_000


async def conversion_status_for(visitor_id: str, campaign_id) -> str:
    visitor_id = str(visitor_id or "").strip()
    if not visitor_id or campaign_id in (None, ""):
        return ""
    key = f"{visitor_id}|{campaign_id}"
    now = time.monotonic()
    hit = _conv_status_cache.get(key)
    if hit is not None and now < hit[0]:
        return hit[1]
    status = ""
    try:
        async with app.state.pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status FROM conversions_data "
                "WHERE visitor_id = $1 AND campaign_id = $2 "
                "ORDER BY id DESC LIMIT 1",
                visitor_id, int(campaign_id))
            if row and row["status"]:
                status = str(row["status"])
    except Exception as e:
        log_track(f"conversion_status lookup error: {e}")
    if len(_conv_status_cache) >= _CONV_STATUS_CACHE_CAP:
        _conv_status_cache.clear()
    _conv_status_cache[key] = (now + _CONV_STATUS_TTL, status)
    return status


# ─── Prefetch-request filtering ─────────────────────────────────────
def request_is_prefetch(request: Request) -> bool:
    """True when the hit is a browser prefetch/prerender, not a real visit.

    Honors the standard `Purpose: prefetch` / `Sec-Purpose: prefetch` headers
    plus the legacy `X-Purpose` / `X-Moz` variants. Toggle default ON because
    prefetches otherwise inflate visit/click-out stats.
    """
    if not tracking_prefetch_filter_enabled():
        return False
    for header in ("purpose", "sec-purpose", "x-purpose", "x-moz"):
        value = (request.headers.get(header) or "").lower()
        if value and "prefetch" in value:
            return True
    return False


# ─── Source-declared bot flag (opt-in per source) ───────────────────
_source_extra_cache: dict = {}
_SOURCE_EXTRA_TTL = 30.0
_SOURCE_EXTRA_CACHE_CAP = 5_000


async def source_extra_settings(campaign) -> dict:
    """Cached `additional_settings` of the campaign's traffic source.

    The source may declare a request-param name via `is_bot_param`; only that
    configured mapping is honored (a free-form ?is_bot=1 is ignored unless a
    source opts in), so a normal visitor cannot spoof bot exclusion.
    """
    try:
        source_id = (campaign.get("traffic_source_id") if hasattr(campaign, "get")
                     else campaign["traffic_source_id"])
    except Exception:
        source_id = None
    if not source_id:
        return {}
    now = time.monotonic()
    hit = _source_extra_cache.get(source_id)
    if hit is not None and now < hit[0]:
        return hit[1]
    extra: dict = {}
    try:
        async with app.state.pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT additional_settings FROM sources WHERE id = $1", int(source_id))
        raw = row["additional_settings"] if row else None
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, dict):
            extra = raw
    except Exception as e:
        log_track(f"source additional_settings load error: {e}")
    if len(_source_extra_cache) >= _SOURCE_EXTRA_CACHE_CAP:
        _source_extra_cache.clear()
    _source_extra_cache[source_id] = (now + _SOURCE_EXTRA_TTL, extra)
    return extra


def source_declares_bot(request: Request, extra: dict) -> bool:
    """True when the request carries a truthy value under the source's
    configured `is_bot_param` (opt-in; empty/unset = ignore)."""
    param = str((extra or {}).get("is_bot_param") or "").strip()
    if not param:
        return False
    value = request.query_params.get(param)
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on", "bot")


@app.get("/optout")
async def opt_out_page() -> HTMLResponse:
    """G79: GDPR opt-out — set the permanent aaa_optout cookie and confirm.

    Once set, campaign routes still redirect (the funnel keeps working) but
    skip the ClickHouse insert and never set tracking cookies. Impressions
    and direct-tracking collects are dropped entirely.
    """
    html = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Opt-out confirmed</title></head>
<body style="font-family: sans-serif; max-width: 32em; margin: 4em auto; line-height: 1.5;">
  <h1>Opt-out confirmed</h1>
  <p>You have opted out of tracking on this site. Your visits are not recorded
     and no tracking cookies are set. You will still be redirected normally
     when following campaign links.</p>
  <p>To opt back in, delete the <code>aaa_optout</code> cookie for this domain
     in your browser settings.</p>
</body>
</html>"""
    resp = HTMLResponse(content=html)
    resp.set_cookie(OPT_OUT_COOKIE, "1", max_age=OPT_OUT_TTL, path="/", samesite="lax")
    return resp


# ─── Server-side synthetic requests (G27 / G74) ────────────────────
def _synthetic_request(ip: str, ua: str, referrer: str = "", language: str = "",
                       path: str = "/", method: str = "GET",
                       query_string: bytes = b"") -> Request:
    """Build a Starlette request from server-provided visitor data.

    No X-Forwarded-For / X-Real-IP headers are set, so enrich_meta resolves
    the IP from the synthetic peer — the caller's IP can never be overridden
    by headers on the outer request. Used by /click-api and /simulate.
    """
    headers = []
    if ua:
        headers.append((b"user-agent", ua.encode("utf-8", "ignore")))
    if referrer:
        headers.append((b"referer", referrer.encode("utf-8", "ignore")))
    if language:
        headers.append((b"accept-language", language.encode("utf-8", "ignore")))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string,
        "headers": headers,
        "client": (ip or "127.0.0.1", 0),
    }
    return Request(scope)


async def decide_campaign_flow(campaign, request: Request, click_id: str = None,
                               depth: int = 0) -> dict:
    """Decision-only counterpart of do_campaign_execution (G27).

    Mirrors the flow-selection logic 1:1 (forced flows, stickiness binding,
    schedules, ClickHouse click caps, filters, offer caps, weight/position
    distribution) but resolves the destination into a JSON-friendly decision
    object instead of executing a redirect or serving a landing.

    SAFER-PATH JUSTIFICATION: do_campaign_execution mutates request.state,
    recurses (redirect_campaign), issues stickiness cookies and returns
    Response objects consumed byte-for-byte by four live call sites. Splitting
    its "decision phase" out would touch every redirect path on a live
    tracker; a parallel decision path that calls the same lower-level helpers
    (match_flow_filters, schedule_matches, flow_click_counts, cap_exceeded,
    offer_cap_state, get_real_offer_url, merge_query_params) leaves the
    redirect pipeline byte-identical. The trade-off is duplicated selection
    logic — keep the two in sync when editing either (including the per-flow
    delivery `action`, mirrored into the decision below so /click-api
    consumers can wrap the destination URL the same way, and the G10 funnel
    branch, mirrored by execute_funnel).

    Shield note: the campaign shield is evaluated by the caller BEFORE this
    function runs (click_api → evaluate_campaign_shield, mirroring
    apply_tracking_gate on the redirect path) and its "allow" watch mode is
    applied by track_event — no shield logic lives here.
    """
    config = parse_campaign_config(campaign)
    flows = config.get("flows", [])
    pg = app.state.pg

    distribution_mode = (campaign.get("redirect_mode") if hasattr(campaign, "get") else campaign["redirect_mode"]) or "position"
    stickiness = bool(config.get("stickiness"))

    sorted_flows = sorted(
        flows,
        key=lambda f: (
            0 if f.get("type") == "forced" else 1,
            f.get("position", 9999)
        )
    )

    paramsIdMapping = get_params_id_mapping_from_campaign(campaign)
    meta_data = await enrich_meta(request, paramsIdMapping)

    if config.get("use_title_as_keyword") and not meta_data.get("keyword"):
        title = await referrer_page_title(meta_data.get("referrer"))
        if title:
            meta_data["keyword"] = title

    routing_hash = campaign_routing_hash(config, distribution_mode)

    # G10 funnels — decision-only mirror of execute_funnel (KEEP IN SYNC with
    # do_campaign_execution/execute_funnel): funnel campaigns ignore
    # config.flows, serve the bound step ("st" in the bind cookie, else step 0,
    # past-the-end clamped to the last step) and stamp flow_index = step.
    funnel_steps_ = funnel_config_steps(config)
    if funnel_steps_ is not None:
        step_index = 0
        bound_f = parse_bind_cookie(request, campaign["id"], routing_hash) if stickiness else None
        if bound_f is not None:
            st = bound_f.get("st")
            if isinstance(st, int) and not isinstance(st, bool) and st > 0:
                step_index = min(st, len(funnel_steps_) - 1)
        flow_indexes = getattr(request.state, "flow_indexes", None)
        if flow_indexes is None:
            flow_indexes = {}
            request.state.flow_indexes = flow_indexes
        flow_indexes[campaign["id"]] = step_index

        step = funnel_steps_[step_index]
        offers = step.get("offers") or []
        decision = {"campaign_id": campaign["id"], "bound": bound_f is not None,
                    "funnel": True, "step": step_index, "name": step.get("name"),
                    "schema": step.get("schema") or ("landing_offer" if offers else "landing_only"),
                    "landing_id": step.get("landing"), "offers": offers,
                    "landing_url": None, "url": None, "flow_index": step_index,
                    "action": "redirect"}
        if step.get("landing"):
            async with pg.acquire() as conn:
                row = await conn.fetchrow("SELECT folder FROM landings WHERE id = $1", step["landing"])
            if row:
                decision["landing_url"] = f"/l/{row['folder']}"
                if decision["schema"] != "landing_only" and offers:
                    decision["url"] = await get_offer_click_url(
                        campaign['alias'], offers[0], step["landing"],
                        click_id=click_id or meta_data.get("click_id"),
                        passthrough=mapped_passthrough_params(config, meta_data))
        return decision

    bound = None
    if stickiness:
        bound = parse_bind_cookie(request, campaign["id"], routing_hash)
        if bound is not None and not _bound_flow_still_valid(bound, sorted_flows):
            bound = None

    chosen = None
    chosen_index = -1
    chosen_override = None
    if bound is not None:
        chosen = sorted_flows[bound["fi"]]
        chosen_index = bound["fi"]
        # A bound visitor whose flow's offer is paused/archived loses the
        # binding and falls through to fresh selection (mirror of
        # do_campaign_execution — KEEP IN SYNC).
        bound_offer = chosen.get("offer")
        if bound_offer:
            bound_offer_cache: dict = {}
            o_status, o_archived = await offer_state(pg, bound_offer_cache, bound_offer)
            if o_status != "active" or o_archived:
                bound = None
                chosen = None
                chosen_index = -1
    if chosen is None:
        eligible = []
        cap_state_cache: dict = {}
        conv_count_cache: dict = {}
        offer_state_cache: dict = {}
        for idx, flow in enumerate(sorted_flows):
            if not flow or not flow.get("enabled"):
                continue
            schedule = flow.get("schedule")
            if schedule and not schedule_matches(schedule):
                continue
            caps = flow.get("caps")
            if caps:
                counts = await flow_click_counts(campaign["id"], idx)
                if counts and cap_exceeded(caps, counts):
                    continue
            filters = flow.get("filters", [])
            if filters and not await match_flow_filters_async(meta_data, filters, request):
                continue
            offer_override = None
            flow_offer = flow.get("offer")
            if flow_offer:
                # Paused/archived offers receive no traffic — skip the flow
                # (mirror of the fresh-selection loop in do_campaign_execution).
                o_status, o_archived = await offer_state(pg, offer_state_cache, flow_offer)
                if o_status != "active" or o_archived:
                    continue
                daily_cap, overflow = await offer_cap_state(pg, cap_state_cache, flow_offer)
                if daily_cap and await offer_conversions_today(pg, conv_count_cache, flow_offer) >= int(daily_cap):
                    if overflow:
                        o_cap, _ = await offer_cap_state(pg, cap_state_cache, overflow)
                        o_used = await offer_conversions_today(pg, conv_count_cache, overflow) if o_cap else 0
                        if o_cap and o_used >= int(o_cap):
                            continue
                        offer_override = overflow
                    else:
                        continue
            eligible.append((idx, flow, offer_override))

        if eligible:
            forced = [(i, f, o) for i, f, o in eligible if f.get("type") == "forced"]
            if forced:
                chosen_index, chosen, chosen_override = forced[0]
            elif distribution_mode == "weight":
                weights = []
                for _, f, _ in eligible:
                    try:
                        w = max(float(f.get("weight", 100) or 0), 0)
                    except (TypeError, ValueError):
                        w = 0
                    weights.append(w)
                if sum(weights) > 0:
                    chosen_index, chosen, chosen_override = random.choices(eligible, weights=weights, k=1)[0]
                else:
                    chosen_index, chosen, chosen_override = eligible[0]
            else:
                chosen_index, chosen, chosen_override = eligible[0]

    if chosen is not None and chosen_index >= 0:
        flow_indexes = getattr(request.state, "flow_indexes", None)
        if flow_indexes is None:
            flow_indexes = {}
            request.state.flow_indexes = flow_indexes
        flow_indexes[campaign["id"]] = chosen_index

    base = {"campaign_id": campaign["id"], "bound": bound is not None}

    # Nothing matched → campaign fallback
    if chosen is None:
        fallback_url = (config.get("fallback_url") or "").strip()
        return {**base, "schema": "fallback", "flow_index": 0, "fallback": True,
                "offer_id": None, "landing_id": None,
                "url": fallback_url or None, "action": "redirect"}

    flow = chosen
    if chosen_override is not None:
        flow = dict(flow)
        flow["offer"] = chosen_override

    click_id = click_id or meta_data.get("click_id") or generate_click_id()
    meta_data["click_id"] = click_id

    schema = flow.get("schema")
    offer_id = flow.get("offer")
    landing_id = flow.get("landing")
    decision = {**base, "schema": schema, "flow_index": chosen_index,
                "offer_id": offer_id, "landing_id": landing_id, "url": None,
                "action": flow.get("action") or "redirect"}

    # SCHEMA: direct — resolve the offer URL with click_id substituted
    if schema == "direct":
        offer_url = await get_real_offer_url(flow.get("offer"))
        if "{click_id}" in offer_url:
            offer_url = offer_url.replace("{click_id}", click_id)
        else:
            offer_url = merge_query_params(offer_url, {"click_id": click_id})
        # G87: {_md5} = md5 hex of the click id in offer URLs.
        if "{_md5}" in offer_url:
            offer_url = offer_url.replace("{_md5}", hashlib.md5(str(click_id).encode()).hexdigest())
        if config.get("send_query_params"):
            offer_url = merge_query_params(offer_url, request.query_params)
        if config.get("send_se_referrer") and meta_data.get("referrer"):
            offer_url = merge_query_params(offer_url, {"referrer": meta_data["referrer"]})
        decision["url"] = offer_url

    # SCHEMA: landing (offer|only|multi) — report the landing + click-out URL
    elif schema in ("landing_offer", "landing_only", "multi"):
        if schema == "multi":
            m_landing = bound.get("landing") if bound else None
            m_offer = bound.get("offer") if bound else None
            m_landings = flow.get("landings") or []
            m_offers = flow.get("offers") or []
            if m_landing is None and m_landings:
                m_landing = random.choice(m_landings)
            if m_offer is None and m_offers:
                m_offer = random.choice(m_offers)
            decision["landing_id"] = m_landing
            decision["offer_id"] = m_offer
            landing_id, offer_id = m_landing, m_offer
        if landing_id:
            async with pg.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM landings WHERE id = $1", landing_id)
            if row:
                decision["landing_url"] = f"/l/{row['folder']}"
                if schema != "landing_only" and offer_id:
                    decision["url"] = await get_offer_click_url(
                        campaign['alias'], offer_id, row['id'],
                        click_id=click_id,
                        passthrough=mapped_passthrough_params(config, meta_data))

    # SCHEMA: redirect
    elif schema == "redirect":
        redirect_url = (flow.get("redirect_url") or "").strip()
        if not redirect_url:
            decision["schema"] = "return_404"
            return decision
        if config.get("send_query_params"):
            redirect_url = merge_query_params(redirect_url, request.query_params)
        if config.get("send_se_referrer") and meta_data.get("referrer"):
            redirect_url = merge_query_params(redirect_url, {"referrer": meta_data["referrer"]})
        decision["url"] = redirect_url

    # SCHEMA: redirect_campaign — recurse into the target campaign's decision
    elif schema == "redirect_campaign":
        target = None
        if flow.get("redirect_campaign") and depth < MAX_REDIRECT_DEPTH:
            async with pg.acquire() as conn:
                target = await conn.fetchrow("SELECT * FROM campaigns WHERE id = $1", flow.get("redirect_campaign"))
        if target:
            nested = await decide_campaign_flow(target, request, click_id, depth=depth + 1)
            nested["redirected_from"] = campaign["id"]
            return nested
        decision["schema"] = "return_404"

    # SCHEMA: return_404 (and unknown schemas) — url stays None
    return decision


# ─── G17: impression tracking ──────────────────────────────────────
@app.get("/i/{campaign_alias}")
async def impression_tracker(campaign_alias: str, request: Request) -> Response:
    """Record an impression (click=false, impression=1) and return a 1x1 GIF.

    Enables view-through reporting (impressions vs clicks per campaign).
    Accepts the same whitelist params as track_event (incl. utm_medium) and
    the JS-collected click_id, keeping the visitor's aaa_cid cookie so a
    later conversion can attribute. Bot rules apply exactly like /t/collect.
    Opted-out visitors get the GIF with no insert and no cookies.
    """
    campaign = await find_campaign_for_tracking(campaign_alias)
    if not campaign:
        log_track(f"❌ Impression for unknown campaign '{campaign_alias}'")
        return Response(content="Not Found", status_code=404, media_type="text/html")

    rule, blocked = await apply_bot_rules(request)
    if rule:
        request.state.bot_marked = rule.get("type")
    if blocked:
        log_track(f"🤖 Blocked impression to campaign '{campaign_alias}' by rule '{rule.get('type')}'")
        return Response(content="Not Found", status_code=404, media_type="text/html")

    if request.cookies.get(OPT_OUT_COOKIE):
        return Response(content=PIXEL_GIF, media_type="image/gif", headers=open_cors_headers())

    click_id = (request.query_params.get("click_id")
                or request.cookies.get(DIRECT_COOKIE) or "").strip() or generate_click_id()

    await track_event(campaign, request, click=False,
                      extra_meta={"impression": 1, "visitor_id": click_id})

    resp = Response(content=PIXEL_GIF, media_type="image/gif", headers=open_cors_headers())
    resp.set_cookie(DIRECT_COOKIE, click_id, max_age=DIRECT_COOKIE_TTL, path="/", samesite="lax")
    return resp


# ─── G27: click API (server-side click processing) ─────────────────
@app.post("/click-api/{campaign_alias}")
async def click_api(campaign_alias: str, request: Request) -> Response:
    """Server-to-server click processing: a mobile app/webview POSTs the
    visitor's real IP/UA/sub_ids and gets the routing decision as JSON
    instead of a redirect. The full normal pipeline runs (enrich, bot rules,
    filters, stickiness, schedule, ClickHouse caps) and the click is tracked;
    no cookies are ever set on this path."""
    campaign = await find_campaign_for_tracking(campaign_alias)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object body required")

    # The visitor IP comes from the JSON body — never from headers.
    ip = str(body.get("ip") or "").strip()
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid or missing 'ip'")

    ua = str(body.get("user_agent") or "")
    # Blacklists match on hit meta (sub_id_1..10, geo, device, os, browser) —
    # expose the body's blacklist-relevant fields as the synthetic query so the
    # click-api path sees exactly what the redirect path would (KEEP IN SYNC
    # with apply_tracking_gate).
    bl_query = urlencode({k: str(v) for k, v in body.items()
                          if k in BLACKLIST_FIELDS and v is not None})
    synth = _synthetic_request(
        ip=ip, ua=ua,
        referrer=str(body.get("referrer") or ""),
        language=str(body.get("language") or ""),
        path=f"/click-api/{campaign_alias}", method="POST",
        query_string=bl_query.encode("utf-8"))

    rule, blocked = await apply_bot_rules(synth)
    if rule:
        synth.state.bot_marked = rule.get("type")

    # G44 blacklists mirror of apply_tracking_gate.
    bl_action = await apply_blacklists(synth, campaign)
    if bl_action == "block":
        blocked = True
    elif bl_action == "mark" and not getattr(synth.state, "bot_marked", None):
        synth.state.bot_marked = "blacklist"

    # Shield + honeypot mirror of apply_tracking_gate (KEEP IN SYNC): both
    # paths call the same helpers, so decisions are identical per entry point.
    if await honeypot_flagged(visitor_key_for(synth)):
        synth.state.honeypot_flagged = True
        if not getattr(synth.state, "bot_marked", None):
            synth.state.bot_marked = "honeypot"
    shield_action = await evaluate_campaign_shield(synth, campaign)

    click_id = str(body.get("click_id") or "").strip() or generate_click_id()
    decision = await decide_campaign_flow(campaign, synth, click_id)
    if rule:
        decision["bot_rule"] = rule.get("type")
    if bl_action:
        decision["blacklist"] = bl_action
    if shield_action:
        decision["shield"] = shield_action
    shield_blocked = shield_action in ("blank", "404")
    decision["shield_blocked"] = shield_blocked
    blocked = blocked or shield_blocked
    decision["bot_blocked"] = bool(blocked)

    if not blocked:
        extra = {k: v for k, v in body.items() if k in VALID_PARAMS}
        await track_event(campaign, synth, extra_meta={**extra, "visitor_id": click_id})

    return JSONResponse({"click_id": click_id, "decision": decision},
                        headers=open_cors_headers())


# ─── G74: traffic simulation (dry-run the routing pipeline) ────────
SIM_GEO_POOL = ["US", "GB", "DE", "IN", "BR", "FR", "NL", "ES"]
SIM_OS_POOL = ["Windows", "Android", "iOS", "macOS", "Linux"]
SIM_BROWSER_POOL = ["Chrome", "Safari", "Firefox", "Edge"]
SIM_IP_PREFIXES = ["11.22.33.", "44.55.66.", "77.88.99.", "90.12.34."]


# ─── Admin auth for operational endpoints ──────────────────────────
# /simulate and /_aaa_tracker_debug live on the public tracking host, so they
# gate on the backend's session cookie: auth_sessions joined to users, checked
# for is_admin. Positive lookups are cached 60s.
_admin_auth_cache: dict = {}


def _check_admin_token(token: str) -> bool:
    try:
        conn = pg_connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT u.is_admin, s.expires_at, s.revoked FROM auth_sessions s "
            "JOIN users u ON u.username = s.username WHERE s.token = %s", (token,))
        row = cur.fetchone()
        conn.close()
        if not row or row[2]:
            return False
        if row[1] and row[1] < datetime.utcnow():
            return False
        return bool(row[0])
    except Exception:
        return False


async def is_admin_request(request: Request) -> bool:
    token = request.cookies.get("session_token")
    if not token:
        return False
    hit = _admin_auth_cache.get(token)
    if hit is not None and time.monotonic() - hit < 60.0:
        return True
    ok = await asyncio.to_thread(_check_admin_token, token)
    if ok:
        _admin_auth_cache[token] = time.monotonic()
    return ok


async def require_admin(request: Request):
    if not await is_admin_request(request):
        raise HTTPException(status_code=401, detail="Admin session required")


@app.post("/simulate/{campaign_alias}")
async def simulate_traffic(campaign_alias: str, request: Request) -> Response:
    """Run N synthetic visitors through the routing pipeline IN MEMORY.

    Flows/filters/stickiness-independent selection/schedules are evaluated for
    real; click caps are simulated by counting within the run (no ClickHouse
    queries). Guaranteed side-effect free: no ClickHouse or Postgres writes,
    no redirects served, no postbacks, no Telegram. Use it to test flow logic
    before sending real traffic. Admin-only."""
    await require_admin(request)
    campaign = await find_campaign_for_tracking(campaign_alias)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON object body required")

    try:
        count = int(body.get("count"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="'count' must be an integer 1-1000")
    if not 1 <= count <= 1000:
        raise HTTPException(status_code=400, detail="'count' must be between 1 and 1000")

    seed = body.get("seed")
    # seed omitted/None -> system entropy; a fixed seed reproduces a run exactly
    rng = random.Random(seed)
    profile = body.get("profile")
    profile = profile if isinstance(profile, dict) else {}

    config = parse_campaign_config(campaign)
    flows = config.get("flows", [])
    distribution_mode = (campaign.get("redirect_mode") if hasattr(campaign, "get") else campaign["redirect_mode"]) or "position"
    sorted_flows = sorted(
        flows,
        key=lambda f: (
            0 if f.get("type") == "forced" else 1,
            f.get("position", 9999)
        )
    )

    bot_cfg = load_bot_rules()
    bot_rules = (bot_cfg.get("rules") or []) if bot_cfg.get("enabled") else []

    stats = {
        "flow_distribution": {}, "schema_distribution": {}, "offer_distribution": {},
        "rejected_by_filters": 0, "capped": 0, "scheduled_out": 0,
        "fallback": 0, "bot_blocked": 0,
    }
    inrun_counts: dict = {}

    for _ in range(count):
        country = str(profile.get("country") or rng.choice(SIM_GEO_POOL))
        os_name = str(profile.get("os") or rng.choice(SIM_OS_POOL))
        browser = str(profile.get("browser") or rng.choice(SIM_BROWSER_POOL))
        if profile.get("device"):
            device = str(profile.get("device"))
        elif os_name in ("Android", "iOS"):
            device = "mobile"
        else:
            device = rng.choice(["desktop", "mobile"])
        ua = f"Mozilla/5.0 (SmokeSim; {os_name}; {browser})"
        prefix = str(profile.get("ip_prefix") or rng.choice(SIM_IP_PREFIXES))
        ip = prefix + str(rng.randint(1, 254))

        meta = {"country": country, "device_type": device, "os": os_name,
                "browser": browser, "ip": ip, "user_agent": ua,
                "language": "en", "is_bot": False}

        synth = _synthetic_request(ip=ip, ua=ua, path=f"/simulate/{campaign_alias}")
        rule = None
        if bot_rules:
            try:
                rule = await asyncio.wait_for(
                    asyncio.to_thread(match_bot_rule, synth, bot_rules, ""), timeout=2.0)
            except asyncio.TimeoutError:
                rule = None
        if rule:
            stats["bot_blocked"] += 1
            if (rule.get("action") or "block") == "block":
                continue
            meta["is_bot"] = True

        eligible = []
        for idx, flow in enumerate(sorted_flows):
            if not flow or not flow.get("enabled"):
                continue
            schedule = flow.get("schedule")
            if schedule and not schedule_matches(schedule):
                stats["scheduled_out"] += 1
                continue
            caps = flow.get("caps")
            if caps:
                # caps simulated within the run: hour/day/total all map to the
                # number of simulated clicks this flow has already taken
                used = inrun_counts.get(idx, 0)
                if cap_exceeded(caps, {"hour": used, "day": used, "total": used}):
                    stats["capped"] += 1
                    continue
            filters = flow.get("filters", [])
            if filters and not await match_flow_filters_async(meta, filters, None):
                stats["rejected_by_filters"] += 1
                continue
            eligible.append((idx, flow))

        if not eligible:
            stats["fallback"] += 1
            continue

        forced = [(i, f) for i, f in eligible if f.get("type") == "forced"]
        if forced:
            idx, flow = forced[0]
        elif distribution_mode == "weight":
            weights = []
            for _, f in eligible:
                try:
                    w = max(float(f.get("weight", 100) or 0), 0)
                except (TypeError, ValueError):
                    w = 0
                weights.append(w)
            if sum(weights) > 0:
                idx, flow = rng.choices(eligible, weights=weights, k=1)[0]
            else:
                idx, flow = eligible[0]
        else:
            idx, flow = eligible[0]

        inrun_counts[idx] = inrun_counts.get(idx, 0) + 1
        stats["flow_distribution"][str(idx)] = stats["flow_distribution"].get(str(idx), 0) + 1
        schema = flow.get("schema") or "unknown"
        stats["schema_distribution"][schema] = stats["schema_distribution"].get(schema, 0) + 1
        offer = flow.get("offer")
        if offer is not None:
            key = str(offer)
            stats["offer_distribution"][key] = stats["offer_distribution"].get(key, 0) + 1

    return JSONResponse({
        "campaign": campaign_alias, "count": count, "seed": seed, "stats": stats,
    })


@app.get("/_aaa_tracker_debug")
async def show_logs(request: Request):
    await require_admin(request)
    return JSONResponse(content=jsonable_encoder(TRACK_LOG[-50:]))


# track and do campaign rules
@app.get("/{campaign_alias}")
async def get_with_campaign_alias(campaign_alias: str, request: Request):
    log_track(f"🔁 New get track or compaign request for '{campaign_alias}'")

    pg = request.app.state.pg

    # get campaign from db
    async with pg.acquire() as conn:
        campaign = await conn.fetchrow("""
                                       SELECT *
                                       FROM campaigns
                                       WHERE alias = $1
                                       """, campaign_alias)

    if not campaign or campaign["status"] != "active":
        msg = f"❌ Campaign '{campaign_alias}' not found"
        log_track(msg)
        raise HTTPException(status_code=404, detail=msg)

    # Bot & filter rules + shield + GDPR opt-out (shared gate). Blocked → 404;
    # opted-out → funnel still redirects but stores nothing.
    blocked = await apply_tracking_gate(request, campaign_alias, campaign)
    if blocked is not None:
        return blocked

    # execution records the chosen flow index and tracks the ClickHouse row
    # itself before returning the response
    return await do_campaign_execution(campaign, request)


@app.head("/{campaign_alias}")
async def head_with_campaign_alias(campaign_alias: str, request: Request):
    # HEAD mirrors GET's status/Location but tracks no click and sends no body
    pg = request.app.state.pg

    async with pg.acquire() as conn:
        campaign = await conn.fetchrow("""
                                       SELECT *
                                       FROM campaigns
                                       WHERE alias = $1
                                       """, campaign_alias)

    if not campaign or campaign["status"] != "active":
        return Response(status_code=404)

    resp = await do_campaign_execution(campaign, request, track=False)
    headers = {}
    if resp.headers.get("location"):
        headers["location"] = resp.headers["location"]
    return Response(status_code=resp.status_code, headers=headers)



@app.post("/{campaign_alias}")
async def post_with_campaign_alias(campaign_alias: str, request: Request):
    log_track(f"🔁 New post track request for '{campaign_alias}'")

    pg = request.app.state.pg

    # get campaign from db
    async with pg.acquire() as conn:
        campaign = await conn.fetchrow("""
                                       SELECT *
                                       FROM campaigns
                                       WHERE alias = $1
                                       """, campaign_alias)

    if not campaign or campaign["status"] != "active":
        msg = f"❌ Campaign '{campaign_alias}' not found"
        log_track(msg)
        raise HTTPException(status_code=404, detail=msg)

    # Bot & filter rules + shield + GDPR opt-out (shared gate)
    blocked = await apply_tracking_gate(request, campaign_alias, campaign)
    if blocked is not None:
        return blocked

    # execution records the chosen flow index and tracks the ClickHouse row
    # itself before returning the response (opted-out visitors are skipped)
    response = await do_campaign_execution(campaign, request)

    out = {"status": "ok", "campaign": campaign_alias}
    if response.headers.get("set-cookie"):
        return JSONResponse(content=out, headers={"set-cookie": response.headers["set-cookie"]})
    return out
