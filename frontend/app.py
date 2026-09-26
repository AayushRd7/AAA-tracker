import random
import base64
import hashlib
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
from fastapi.middleware.cors import CORSMiddleware
from user_agents import parse as parse_ua
import re
import os
import requests
import httpx
import psycopg2

import asyncio
import queue

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

    # Daily data-retention prune loop
    app.state.retention_task = asyncio.create_task(retention_loop())


async def ensure_schema():
    try:
        async with app.state.pg.acquire() as conn:
            await conn.execute("""
                ALTER TABLE conversions_data
                ADD COLUMN IF NOT EXISTS postback_count INTEGER DEFAULT 0,
                ADD COLUMN IF NOT EXISTS last_postback_at TIMESTAMP,
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
    except Exception as e:
        log_track(f"Schema migration error: {e}")


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
    'ad_campaign_id', 'browser', 'campaign_id', 'city', 'connection_type', 'currency',
    'cost', 'country', 'utm_creative', 'utm_campaign', 'utm_medium', 'utm_source', 'device_type', 'external_id', 'ip',
    'impression', 'is_bot', 'is_using_proxy', 'isp', 'keyword', 'landing_id', 'language', 'offer_id',
    'os', 'profit', 'referrer', 'region', 'revenue', 'status', 'sub_id_1', 'sub_id_2', 'sub_id_3',
    'sub_id_4', 'sub_id_5', 'sub_id_6', 'sub_id_7', 'sub_id_8', 'sub_id_9', 'sub_id_10',
    'traffic_source_name', 'url', 'visitor_id'
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
        "user_agent": ua_string,
    }

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
    meta_data["click_id"] = meta_data.get("click_id") or generate_click_id()

    # 5. Save the click asynchronously
    background_tasks.add_task(save_click_to_db, meta_data)

    # 6. Build the final URL
    offer_url = offer["url"]
    for key, value in meta_data.items():
        placeholder = f"{{{key}}}"
        if placeholder in offer_url:
            offer_url = offer_url.replace(placeholder, str(value))

    # 2. Always append click_id as ?click_id=...
    parsed = urlparse(offer_url)
    query_params = dict(parse_qsl(parsed.query))
    query_params["click_id"] = meta_data["click_id"]  # required

    # Assemble the final URL
    offer_url = urlunparse(parsed._replace(query=urlencode(query_params)))

    return RedirectResponse(offer_url)


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
        "os", "isp", "is_using_proxy", "is_bot", "device_type"
    }

    # keep only the allowed fields
    insert_data = {k: v for k, v in meta.items() if k in allowed_fields}

    insert_data["received_at"] = datetime.utcnow()

    insert_data["status"] = 'lead'

    columns = ", ".join(insert_data.keys())
    values_placeholders = ", ".join(
        ["NOW()" if v == "now()" else f"${i + 1}" for i, v in enumerate(insert_data.values())]
    )
    values = [v for v in insert_data.values() if v != "now()"]

    query = f"INSERT INTO conversions_data ({columns}) VALUES ({values_placeholders})"

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
        request.state._parsed_body = post_data
    return post_data if isinstance(post_data, dict) else {}


async def find_campaign_for_tracking(c_ref: str):
    """Active campaign by numeric id, falling back to alias.

    Paused/archived campaigns must not track (status gate): every direct
    endpoint (/t/collect, /p, /i, /click-api, /simulate) resolves through here.
    """
    pg = app.state.pg
    async with pg.acquire() as conn:
        if str(c_ref).isdigit():
            row = await conn.fetchrow(
                "SELECT * FROM campaigns WHERE id = $1 AND status = 'active'", int(c_ref))
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


def _read_settings_block(key: str):
    """Fresh (sync, own connection) read of one block from the settings row."""
    try:
        conn = pg_connect()
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE name = 'settings'")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return json.loads(row[0]).get(key)
    except Exception as e:
        log_track(f"Settings '{key}' load error: {e}")
    return None


def _settings_block(key: str, default=None):
    """Cached read of a settings-row block (30s TTL)."""
    if default is None:
        default = {}
    now = time.monotonic()
    hit = _settings_cache.get(key)
    if hit is not None and now - hit[0] < _SETTINGS_CACHE_TTL:
        return hit[1]
    value = _read_settings_block(key)
    if not isinstance(value, type(default)):
        value = default
    _settings_cache[key] = (now, value)
    return value


def load_postback_security() -> dict:
    """Read the postback_security block from the settings row (30s TTL cache)."""
    return _settings_block("postback_security")


def check_postback_access(request: Request, sec: dict) -> tuple[bool, str]:
    """Enforce the optional secret key and IP allowlist on inbound postbacks."""
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
        if not secrets.compare_digest(str(provided), str(secret)):
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


async def apply_tracking_gate(request: Request, label: str) -> Response | None:
    """Bot-rule + GDPR opt-out gate shared by every campaign-serving route.

    Returns a Response to short-circuit with (bot-blocked 404), or None to
    proceed. A non-blocking bot rule marks request.state.bot_marked; an opted-out
    visitor sets request.state.optout — the caller still serves the funnel but
    stores nothing (do_campaign_execution skips tracking when optout is set).
    """
    rule, blocked = await apply_bot_rules(request)
    if rule:
        request.state.bot_marked = rule.get("type")
        if blocked:
            log_track(f"🤖 Blocked visit to '{label}' by rule '{rule.get('type')}'")
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
                data = {"click_id": cid, "status": status, "payout": we_pay}
                data.update(dict(row))
                background_tasks.add_task(
                    send_postback, url, data, post=postback.get("method") == "POST")

        if traffic_source_id:
            source = await conn.fetchrow("SELECT * FROM sources WHERE id = $1", traffic_source_id)
            if source and source["s2s_postback"]:
                raw_statuses = source["s2s_postback_statuses"]
                src_statuses = json.loads(raw_statuses) if isinstance(raw_statuses, str) else (raw_statuses or {})
                status_map = {"sale": "sale", "lead": "lead",
                              "reject": "rejected", "upsell": "upsale"}
                fire = any(tracker_status == status and src_statuses.get(src_key)
                           for src_key, tracker_status in status_map.items())
                if not src_statuses:
                    fire = True
                if fire:
                    src_data = {"click_id": cid, "status": status, "payout": payout_value}
                    src_data["clickid"] = cid
                    src_data.update({k: v for k, v in dict(row).items() if v is not None})
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
            return {"status": "ok", "click_id": click_id,
                    "updated_status": status, "duplicate": False}

        is_duplicate = await apply_to_row(conn, row)

        # Telegram conversion notification (respects settings toggle/statuses);
        # skipped for duplicate postbacks so chat stays spam-free
        if not is_duplicate:
            background_tasks.add_task(
                notify_telegram_conversion_async, click_id, status, payout_value, dict(row))
            await fanout(conn, row, click_id)

    return {"status": "ok", "click_id": click_id, "updated_status": status,
            "duplicate": is_duplicate}


@app.get("/pb/{click_id}/{status}/{payout}")
async def postback_receive(click_id: str, status: str, payout: str, request: Request,
                           background_tasks: BackgroundTasks):
    status = normalize_status(status)
    # Built-ins (lead/sale/upsale/rejected/hold/trash) + configured custom statuses
    if status not in valid_conversion_statuses():
        raise HTTPException(status_code=400, detail="Invalid status")

    try:
        payout_value = float(payout)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payout format")

    # Postback protection (optional secret key + IP allowlist, from Settings)
    sec = load_postback_security()
    allowed, deny_reason = check_postback_access(request, sec)
    if not allowed:
        log_track(f"🚫 Postback denied for {click_id}: {deny_reason}")
        raise HTTPException(status_code=403, detail=deny_reason)

    extra_fields = {k: v for k, v in request.query_params.items() if k in PIXEL_EXTRA_FIELDS}
    result = await record_conversion(click_id, status, payout_value, request, background_tasks,
                                     extra_fields or None, source="postback")
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
        payout_value = float(request.query_params.get("payout") or 0)
    except ValueError:
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
                  AND received_at >= CURRENT_DATE
                """, offer_id)
        cache[offer_id] = row["n"] if row else 0
    return cache[offer_id]


# ─── Visitor stickiness (A/B binding) ─────────────────────────────
BIND_COOKIE = "aaa_bind"
BIND_TTL_SECONDS = 30 * 24 * 3600


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
    return b"aaa-bind-secret"


def _bind_secret() -> bytes:
    env = os.environ.get("AAA_BIND_SECRET")
    if env:
        return env.encode()
    cached = getattr(app.state, "_bind_secret", None)
    if cached is None:
        cached = _load_or_create_bind_secret()
        app.state._bind_secret = cached
    return cached


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def campaign_routing_hash(config: dict, distribution_mode: str) -> str:
    """Hash of the routing-relevant config — editing flows/weights invalidates bindings."""
    payload = json.dumps(
        {"flows": config.get("flows", []), "redirect_mode": distribution_mode},
        sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def make_bind_cookie(campaign_id: int, flow_index: int, offer, landing, routing_hash: str) -> str:
    payload = {"cid": int(campaign_id), "fi": int(flow_index), "offer": offer, "landing": landing,
               "exp": int(datetime.utcnow().timestamp()) + BIND_TTL_SECONDS, "h": routing_hash}
    raw = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_bind_secret(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def parse_bind_cookie(request: Request, campaign_id: int, routing_hash: str):
    """Return the validated binding payload, or None (bad sig/expired/other campaign/stale config)."""
    raw_cookie = request.cookies.get(BIND_COOKIE)
    if not raw_cookie or "." not in raw_cookie:
        return None
    raw, sig = raw_cookie.rsplit(".", 1)
    expected = hmac.new(_bind_secret(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
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


def get_params_id_mapping_from_campaign(campaign: dict) -> list:
    config_str = campaign.get("config")
    if not config_str:
        return []

    try:
        config = json.loads(config_str)
        return config.get("paramsIdMapping", [])
    except json.JSONDecodeError:
        return []


def meta_refresh_redirect(url: str) -> Response:
    """Redirect via an HTML meta refresh so the browser sends no referrer."""
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


async def do_campaign_execution(campaign, request: Request, depth: int = 0,
                                track: bool = True) -> Response:
    log_track(f"🔁 New campaign execution call for '{campaign}'")

    config = json.loads(campaign["config"])
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

    # use the referring page's <title> as keyword when no keyword param came in
    if config.get("use_title_as_keyword") and not meta_data.get("keyword"):
        title = await referrer_page_title(meta_data.get("referrer"))
        if title:
            meta_data["keyword"] = title

    routing_hash = campaign_routing_hash(config, distribution_mode)

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
    else:
        # Collect eligible flows: enabled + schedule open + caps open + filters passed
        eligible = []
        cap_state_cache: dict = {}
        conv_count_cache: dict = {}
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
                response = meta_refresh_redirect(fallback_url)
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
                return meta_refresh_redirect(url)
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
            response.set_cookie(
                BIND_COOKIE,
                make_bind_cookie(campaign["id"], chosen_index, offer_id, landing_id, routing_hash),
                max_age=BIND_TTL_SECONDS, path="/", httponly=True, samesite="lax")

    # Track THIS campaign's own execution (flow index was recorded above under
    # this campaign's id). Inner redirect_campaign levels track themselves when
    # they execute; opted-out visitors are never recorded. ClickHouse failures
    # never kill the visitor's response — track_event logs and swallows them.
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
        return campaign_redirect(redirect_url)

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

    config = json.loads(campaign["config"])

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

    for k, v in meta_data.items():
        if k in VALID_PARAMS:
            result_row[k] = v

    # Bot rule with "mark" action — flag the click but keep tracking it
    if getattr(request.state, "bot_marked", None):
        result_row["is_bot"] = True

    if click is not None:
        result_row["click"] = bool(click)

    if extra_meta:
        for k, v in extra_meta.items():
            if k in VALID_PARAMS and v is not None:
                result_row[k] = v

    # Apply the mapping and add the query parameters
    for key in VALID_PARAMS:
        if key in query:
            mapped_key = mapping.get(key, key)
            result_row[mapped_key] = query[key]

    # ❗ Drop every field whose value is None
    result_row = {k: v for k, v in result_row.items() if v is not None}

    # G79 privacy: anonymize IPs at rest when settings.privacy.anonymize_ip is on
    # (IPv4 → last octet zeroed, e.g. 1.2.3.4 → 1.2.3.0). Default off.
    if load_privacy_settings().get("anonymize_ip"):
        ip_val = result_row.get("ip")
        if ip_val:
            try:
                addr = ipaddress.ip_address(str(ip_val))
                if addr.version == 4:
                    result_row["ip"] = ".".join(str(addr).split(".")[:3] + ["0"])
            except ValueError:
                pass

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


async def send_postback(url: str, data: dict, post: bool = False):
    VALID_PARAMS = [
        'payout', 'status', 'click_id', 'browser', 'campaign_id', 'city', 'connection_type', 'currency',
        'cost', 'country', 'utm_creative', 'utm_campaign', 'utm_source', 'device_type', 'external_id', 'ip',
        'is_bot', 'is_using_proxy', 'isp', 'keyword', 'landing_id', 'language', 'offer_id',
        'os', 'profit', 'referrer', 'region', 'revenue', 'status', 'sub_id_1', 'sub_id_2', 'sub_id_3',
        'sub_id_4', 'sub_id_5', 'sub_id_6', 'sub_id_7', 'sub_id_8', 'sub_id_9', 'sub_id_10',
        'traffic_source_name', 'url', 'visitor_id'
    ]

    # 🔒 Filtered parameters
    safe_data = {k: str(v) for k, v in data.items() if k in VALID_PARAMS}

    class SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    filled_url = url.format_map(SafeDict({k: str(v) for k, v in data.items()}))

    # Redact: only the origin+path goes into the debug log, never the query
    # string (it carries click ids, tokens and payouts).
    redacted = filled_url.split("?")[0]
    log_track(f"<UNK> Sending Postback: {redacted}")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if post:
                response = await client.post(filled_url, json=safe_data)
            else:
                response = await client.get(filled_url, params=safe_data)

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
    logic — keep the two in sync when editing either.
    """
    config = json.loads(campaign["config"])
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
    else:
        eligible = []
        cap_state_cache: dict = {}
        conv_count_cache: dict = {}
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
                "url": fallback_url or None}

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
                "offer_id": offer_id, "landing_id": landing_id, "url": None}

    # SCHEMA: direct — resolve the offer URL with click_id substituted
    if schema == "direct":
        offer_url = await get_real_offer_url(flow.get("offer"))
        if "{click_id}" in offer_url:
            offer_url = offer_url.replace("{click_id}", click_id)
        else:
            offer_url = merge_query_params(offer_url, {"click_id": click_id})
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
    synth = _synthetic_request(
        ip=ip, ua=ua,
        referrer=str(body.get("referrer") or ""),
        language=str(body.get("language") or ""),
        path=f"/click-api/{campaign_alias}", method="POST")

    rule, blocked = await apply_bot_rules(synth)
    if rule:
        synth.state.bot_marked = rule.get("type")

    click_id = str(body.get("click_id") or "").strip() or generate_click_id()
    decision = await decide_campaign_flow(campaign, synth, click_id)
    if rule:
        decision["bot_rule"] = rule.get("type")
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
    rng = random.Random(seed if seed is not None else 0)
    profile = body.get("profile")
    profile = profile if isinstance(profile, dict) else {}

    config = json.loads(campaign["config"])
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

    # Bot & filter rules + GDPR opt-out (shared gate). Blocked → 404;
    # opted-out → funnel still redirects but stores nothing.
    blocked = await apply_tracking_gate(request, campaign_alias)
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

    # Bot & filter rules + GDPR opt-out (shared gate)
    blocked = await apply_tracking_gate(request, campaign_alias)
    if blocked is not None:
        return blocked

    # execution records the chosen flow index and tracks the ClickHouse row
    # itself before returning the response (opted-out visitors are skipped)
    response = await do_campaign_execution(campaign, request)

    out = {"status": "ok", "campaign": campaign_alias}
    if response.headers.get("set-cookie"):
        return JSONResponse(content=out, headers={"set-cookie": response.headers["set-cookie"]})
    return out
