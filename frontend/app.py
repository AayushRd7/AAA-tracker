import random

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

app = FastAPI()
app.state = SimpleNamespace()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ClickHouse client pool — the tracking plane runs inserts via asyncio.to_thread,
# so one shared client would hit clickhouse-connect's concurrent-queries-per-session
# guard under load. A small pool hands each thread its own client/session.
CH_POOL_SIZE = 8


def new_ch_client():
    return get_client(
        host='tracker_clickhouse',
        port=8123,
        username='user',
        password='password_password_password',
        database='default'
    )


def acquire_ch(timeout: float = 2.0):
    try:
        return app.state.ch_pool.get(timeout=timeout)
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
    app.state.ch_pool.put(ch)


# 🚀 Startup
@app.on_event("startup")
async def startup():
    app.state.pg = await asyncpg.create_pool(
        user="user",
        password="password_password_password",
        database="db",
        host="tracker_postgres",
        port=5432
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
                ADD COLUMN IF NOT EXISTS last_postback_at TIMESTAMP
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
    conn = psycopg2.connect(
        host="tracker_postgres", dbname="db",
        user="user", password="password_password_password")
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
    'cost', 'country', 'utm_creative', 'utm_campaign', 'utm_source', 'device_type', 'external_id', 'ip',
    'is_bot', 'is_using_proxy', 'isp', 'keyword', 'landing_id', 'language', 'offer_id',
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


def log_track(message: str):
    TRACK_LOG.append(message)
    if len(TRACK_LOG) > 50:
        TRACK_LOG.pop(0)


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

    # POST parameters
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

    # Cookies
    cookies = request.cookies

    # Merge everything into one flat dictionary
    meta = {
        "received_at": datetime.utcnow().isoformat(),
        "ip": request.client.host,
        "referrer": request.headers.get("referer"),
        "current_domain": request.headers.get("host"),
        "language": language,
        "country": country_code,
        "browser": parsed.get("browser", {}).get("name"),
        "os": parsed.get("os", {}).get("name"),
        "device_type": "mobile" if "Mobile" in ua_string else "desktop",
        "is_bot": ua.is_bot or "bot" in ua_string.lower(),
        "user_agent": ua_string,
    }

    combined = {**query_params, **post_data, **cookies}

    # Add query, post and cookie parameters directly
    for k, v in combined.items():
        if k not in meta:  # don't overwrite the base keys
            meta[k] = v

    log_track('params_id_mapping')
    log_track(params_id_mapping)

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

    return meta


async def show_landing(folder: str, offer_url: str = None) -> Response:
    index_php = os.path.join("landings", folder, "index.php")
    index_html = os.path.join("landings", folder, "index.html")

    print(index_php, os.path.exists(index_php))
    if os.path.exists(index_php) or os.path.exists(index_html):
        url = f"https://tracker_nginx/l/{folder}"
        r = requests.get(url, verify=False)  # , data={"name": "Anton"})
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

        # 2. Offer
        offer = await conn.fetchrow("SELECT * FROM offers WHERE id = $1", int(offer_id))
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
        meta_data["landing_id"] = offer["id"]
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


# ─── Telegram conversion notifications ─────────────────────────────
TELEGRAM_STATUS_EMOJI = {
    "lead": "💵", "sale": "💰", "upsale": "💎",
    "rejected": "❌", "hold": "⏳", "trash": "🗑️",
}
ALL_STATUSES = ["lead", "sale", "upsale", "rejected", "hold", "trash"]


def load_telegram_config() -> dict:
    """Read the telegram block from the settings row (sync, own connection)."""
    try:
        conn = psycopg2.connect(
            host="tracker_postgres", dbname="db",
            user="user", password="password_password_password")
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


def load_postback_security() -> dict:
    """Read the postback_security block from the settings row (sync, own connection)."""
    try:
        conn = psycopg2.connect(
            host="tracker_postgres", dbname="db",
            user="user", password="password_password_password")
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE name = 'settings'")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            cfg = json.loads(row[0])
            return cfg.get("postback_security") or {}
    except Exception as e:
        log_track(f"Postback security config load error: {e}")
    return {}


def check_postback_access(request: Request, sec: dict) -> tuple[bool, str]:
    """Enforce the optional secret key and IP allowlist on inbound postbacks."""
    client_ip = (request.client.host if request.client else "") or ""

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
        if provided != secret:
            return False, "Invalid or missing postback key"

    return True, ""


# ====== Bot & filter rules ======
_seen_visitors: set = set()
_SEEN_VISITORS_CAP = 200_000


def load_bot_rules() -> dict:
    """Read the bot_rules block from the settings row (sync, own connection)."""
    try:
        conn = psycopg2.connect(
            host="tracker_postgres", dbname="db",
            user="user", password="password_password_password")
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE name = 'settings'")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            cfg = json.loads(row[0])
            return cfg.get("bot_rules") or {}
    except Exception as e:
        log_track(f"Bot rules config load error: {e}")
    return {}


def visitor_key_for(request: Request) -> str:
    """Stable per-visitor key (IP + User-Agent) used by the duplicate-visitor rule."""
    ua = request.headers.get("user-agent", "") or ""
    ip = (request.client.host if request.client else "") or ""
    import hashlib
    return hashlib.md5(f"{ip}|{ua}".encode()).hexdigest()


def match_bot_rule(request: Request, rules: list, visitor_key: str):
    """Return the first matching rule, or None."""
    client_ip = (request.client.host if request.client else "") or ""
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


def apply_bot_rules(request: Request):
    """Evaluate bot rules for an inbound visit.

    Returns (rule, blocked) — blocked=True means serve 404 without tracking;
    otherwise the rule (if any) marks the click as a bot but tracking continues.
    """
    cfg = load_bot_rules()
    if not cfg.get("enabled"):
        return None, False

    rules = cfg.get("rules") or []
    has_duplicate_rule = any(
        (r.get("type") == "duplicate_visitor" and r.get("enabled") is not False)
        for r in rules)
    visitor_key = visitor_key_for(request) if has_duplicate_rule else ""

    rule = match_bot_rule(request, rules, visitor_key)

    if has_duplicate_rule and visitor_key:
        if len(_seen_visitors) >= _SEEN_VISITORS_CAP:
            _seen_visitors.clear()
        _seen_visitors.add(visitor_key)

    if rule:
        if (rule.get("action") or "block") == "block":
            return rule, True
        return rule, False
    return None, False


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


@app.get("/pb/{click_id}/{status}/{payout}")
async def postback_receive(click_id: str, status: str, payout: str, request: Request,
                           background_tasks: BackgroundTasks):
    VALID_STATUSES = {"lead", "sale", "upsale", "rejected", "hold", "trash"}

    # update status in db but only 'lead', 'sale', 'upsale', 'rejected', 'hold', 'trash'
    if status not in VALID_STATUSES:
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

    pg = request.app.state.pg

    async with pg.acquire() as conn:
        # Fetch the click first so we can detect duplicate postbacks
        row = await conn.fetchrow("""
                                  SELECT c.*, ca.config AS campaign_config, ca.name AS campaign_name
                                  FROM conversions_data c
                                      LEFT JOIN campaigns ca on c.campaign_id = ca.id
                                  WHERE c.click_id = $1
                                  """, click_id)

        if not row:
            raise HTTPException(status_code=404, detail="Click ID not found")

        # Dedupe: identical (status, payout) repeated for the same click is a no-op
        prev_status = row["status"]
        prev_payout = float(row["payout"] or 0)
        is_duplicate = (prev_status == status and abs(prev_payout - payout_value) < 0.005)
        new_count = (row["postback_count"] or 0) + 1

        result = await conn.execute("""
                                    UPDATE conversions_data
                                    SET status = $1, payout = $2, postback_count = $3,
                                        last_postback_at = NOW()
                                    WHERE click_id = $4
                                    """, status, payout_value, new_count, click_id)

        # Telegram conversion notification (respects settings toggle/statuses);
        # skipped for duplicate postbacks so chat stays spam-free
        if not is_duplicate:
            background_tasks.add_task(
                notify_telegram_conversion, click_id, status, payout_value,
                dict(row))

            if row["campaign_config"]:
                config = json.loads(row["campaign_config"])
                # cycle of postbacks
                postbacks = config.get("postbacks", [])

                offer_id = row["offer_id"]
                # get offer from db
                offer = await conn.fetchrow("SELECT * FROM offers WHERE id = $1", offer_id)
                if offer:
                    we_pay__payout_value = offer["payout"]

                for postback in postbacks:
                    url = postback.get("url")
                    if url:

                        # send postback
                        data = {
                            "click_id": click_id,
                            "status": status,
                            "payout": we_pay__payout_value
                        }
                        # if row is an asyncpg.Record → convert it to a dict and merge
                        if row:
                            data.update(dict(row))
                        post_type = postback.get("method") == "POST"
                        log_track(f"<UNK> Postback Type: {post_type}")
                        # await send_postback(url, data, post_type)
                        background_tasks.add_task(send_postback, url, data, post=True)

    return JSONResponse(content={
        "status": "ok",
        "click_id": click_id,
        "updated_status": status,
        "duplicate": is_duplicate,
    })


@app.get("/")
async def domain_page_default_campaign(request: Request) -> Response:
    log_track('🔁 Domain request')
    host = request.headers.get("host")
    campaign = await get_default_campaign_from_db(host)

    if campaign is None:
        return Response(content="404 Not Found", media_type="text/html")

    # print('Requested campaign:', campaign)
    # print('Requested domain:', host)
    await track_event(campaign, request)
    return await do_campaign_execution(campaign, request)
    # return None


@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code in (404, 405):
        host = request.headers.get("host", "").lower().strip()
        if host:
            pg = app.state.pg
            async with pg.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM domains WHERE domain = $1", host)
                log_track(f"🔁 domain '{row}'")
                if row and row['handle_404'] == 'handle':
                    log_track('HANDLE 404')
                    return await domain_page_default_campaign(request)

        return render_404_html()
    # other errors by default
    return Response(content=str(exc.detail), status_code=exc.status_code)


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


async def do_campaign_execution(campaign, request: Request) -> Response:
    log_track(f"🔁 New campaign execution call for '{campaign}'")

    config = json.loads(campaign["config"])
    flows = config.get("flows", [])

    pg = app.state.pg

    # Distribution mode: 'position' = first matching flow wins,
    # 'weight' = weighted random split across matching flows (Binom-style %).
    distribution_mode = (campaign.get("redirect_mode") if hasattr(campaign, "get") else campaign["redirect_mode"]) or "position"

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

    # Collect eligible flows: enabled + filters passed
    eligible = []
    for flow in sorted_flows:
        if not flow or not flow.get("enabled"):
            continue
        filters = flow.get("filters", [])
        if filters and not check_filters(meta_data, filters, request):
            continue
        eligible.append(flow)

    # Choose the flow to serve
    chosen = None
    if eligible:
        # Forced flows always win first (position order)
        forced = [f for f in eligible if f.get("type") == "forced"]
        if forced:
            chosen = forced[0]
        elif distribution_mode == "weight":
            weights = []
            for f in eligible:
                try:
                    w = max(float(f.get("weight", 100) or 0), 0)
                except (TypeError, ValueError):
                    w = 0
                weights.append(w)
            if sum(weights) > 0:
                chosen = random.choices(eligible, weights=weights, k=1)[0]
            else:
                chosen = eligible[0]
        else:
            chosen = eligible[0]

    # Nothing matched → campaign fallback URL if set, else 404
    if chosen is None:
        fallback_url = (config.get("fallback_url") or "").strip()
        if fallback_url:
            if config.get("hide_referrer"):
                return meta_refresh_redirect(fallback_url)
            return RedirectResponse(fallback_url)
        return render_404_html()

    flow = chosen
    schema = flow.get("schema")
    # Respect per-campaign "hide referrer" on outbound redirects
    if config.get("hide_referrer"):
        def campaign_redirect(url):
            return meta_refresh_redirect(url)
    else:
        def campaign_redirect(url):
            return RedirectResponse(url)

    # SCHEMA: direct
    if schema == "direct":
        offer_url = get_real_offer_url(flow.get("offer"))
        return campaign_redirect(offer_url)

    # SCHEMA: landing → offer
    elif schema == "landing_offer":
        landing = flow.get("landing")
        offer_id = flow.get("offer")
        if landing:
            async with pg.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM landings WHERE id = $1", landing)
                if row:
                    landing_folder = row["folder"]
                    offer_url = await get_offer_click_url(campaign['alias'], offer_id, row['id'], meta_data)
                    return await show_landing(landing_folder, offer_url)
        return render_404_html()

    # SCHEMA: landing only
    elif schema == "landing_only":
        landing = flow.get("landing")
        if landing:
            async with pg.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM landings WHERE id = $1", landing)
                if row:
                    landing_folder = row["folder"]
                    return await show_landing(landing_folder)
        return render_404_html()

    # SCHEMA: multi
    elif schema == "multi":
        # Pick random landing and offer
        landing_id = random.choice(flow.get("landings", []))
        offer_id = random.choice(flow.get("offers", []))

        if landing_id:
            async with pg.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM landings WHERE id = $1", landing_id)
                if row:
                    landing_folder = row["folder"]
                    offer_url = await get_offer_click_url(campaign['alias'], offer_id, row['id'], meta_data)
                    return await show_landing(landing_folder, offer_url)
        return render_404_html()

    # SCHEMA: redirect
    elif schema == "redirect":
        return campaign_redirect(flow.get("redirect_url"))

    # SCHEMA: redirect_campaign ++++
    elif schema == "redirect_campaign":
        campaign_id = flow.get("redirect_campaign")

        if campaign_id:
            async with pg.acquire() as conn:
                target_campaign = await conn.fetchrow("SELECT * FROM campaigns WHERE id = $1", campaign_id)
                if target_campaign:
                    return await do_campaign_execution(target_campaign, request)
        return render_404_html()

    # SCHEMA: return_404 +++
    elif schema == "return_404":
        return render_404_html()

    # Default fallback
    return render_404_html()


async def get_offer_click_url(campaign_alias: str, offer_id: str, landing_id: str = None,
                              offer_vars: dict = None) -> str:
    base_url = f"/c/{campaign_alias}/{offer_id}"
    query_params = {}

    if landing_id:
        query_params["l_id"] = landing_id

    if offer_vars:
        query_params.update(offer_vars)

    if query_params:
        return f"{base_url}?" + urlencode(query_params)

    return base_url


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


async def track_event(campaign, request: Request):
    """ track events to ClickHouse """

    try:
        campaign_alias = campaign["alias"]
        log_track(f"🔁 New track data call for '{campaign_alias}'")
    except KeyError:
        msg = "❌ Campaign alias not found in track_event"
        log_track(msg)
        raise HTTPException(status_code=400, detail=msg)

    content_type = request.headers.get('content-type', '')
    if content_type.startswith('application/x-www-form-urlencoded'):
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

    # Add the enriched fields
    meta_data = await enrich_meta(request, campaign.get("paramsIdMapping"))
    for k, v in meta_data.items():
        if k in VALID_PARAMS:
            result_row[k] = v

    # Bot rule with "mark" action — flag the click but keep tracking it
    if getattr(request.state, "bot_marked", None):
        result_row["is_bot"] = True

    # Apply the mapping and add the query parameters
    for key in VALID_PARAMS:
        if key in query:
            mapped_key = mapping.get(key, key)
            result_row[mapped_key] = query[key]

    # ❗ Drop every field whose value is None
    result_row = {k: v for k, v in result_row.items() if v is not None}

    # TODO: POSTBACK SENDING ASYNC WITHOUT AWAIT
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
        log_track(f"❌ ClickHouse insert failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"ClickHouse error: {e}")


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

    filled_url = url.format(**{k: str(v) for k, v in data.items()})

    log_track(f"<UNK> Sending Postback: {filled_url}")

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


@app.get("/_aaa_tracker_debug")
def show_logs():
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

    if not campaign:
        msg = f"❌ Campaign '{campaign_alias}' not found"
        log_track(msg)
        raise HTTPException(status_code=404, detail=msg)

    # Bot & filter rules
    rule, blocked = apply_bot_rules(request)
    if rule:
        request.state.bot_marked = rule.get("type")
        if blocked:
            log_track(f"🤖 Blocked visit to '{campaign_alias}' by rule '{rule.get('type')}'")
            return render_404_html()

    # tracking
    await track_event(campaign, request)
    return await do_campaign_execution(campaign, request)



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

    if not campaign:
        msg = f"❌ Campaign '{campaign_alias}' not found"
        log_track(msg)
        raise HTTPException(status_code=404, detail=msg)

    # Bot & filter rules
    rule, blocked = apply_bot_rules(request)
    if rule:
        request.state.bot_marked = rule.get("type")
        if blocked:
            log_track(f"🤖 Blocked post visit to '{campaign_alias}' by rule '{rule.get('type')}'")
            return render_404_html()

    # tracking
    await track_event(campaign, request)

    await do_campaign_execution(campaign, request)

    return {"status": "ok", "campaign": campaign_alias}
