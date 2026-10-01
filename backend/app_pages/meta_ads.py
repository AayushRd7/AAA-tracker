"""Meta Ads cost auto-sync.

Pulls daily spend/impressions/clicks from the Meta Marketing API (Graph
`insights`, level=campaign, time_increment=1) for the configured ad accounts,
stores the raw audit trail in Postgres `ad_cost_daily`, and allocates each
matched campaign-day's spend into the existing per-click ``cost`` on
ClickHouse ``clicks_data``.

COST PRECEDENCE (important — read before touching the allocation code)
---------------------------------------------------------------------
Reports and ROAS read ``cost`` from ``clicks_data`` (see
``clickHouse.get_report_breakdown``: ``sumOrNull(toFloat64(cost))``). Keeping a
single cost field avoids a metric-layer rewrite and any double counting:
the platform's spend for a matched campaign+day is *authoritative for that day*
and is written into ``clicks_data.cost`` for that campaign's clicks on that
day. Per-click cost = platform spend / that day's tracker clicks; re-running a
day REPLACES its values (an absolute UPDATE, never an add) so re-syncs are
idempotent. The retroactive cost tool (``app_pages/costs.py``) stays for manual
corrections and for campaigns with no platform mapping — the last writer wins
per campaign+day. Days with zero tracker clicks store the daily audit row but
allocate nothing — unless the campaign's traffic source enabled impression cost
sync, in which case one synthetic ``system-`` click is recorded for that
campaign+day and the whole day's spend is attached to it (re-run replaces it).

Matching order (see ``match_insight``): the explicit
``campaigns.ad_platform_campaign_id`` column first, then the tracker campaign
``name`` (exact, then case-insensitive/trimmed), then the campaign's configured
tracking identifiers (``utm_campaign`` / ``sub_id_*``). The settings
``match_preference`` narrows which of those strategies may run.

Everything here runs off the request hot path: the background loop and the
manual ``POST /api/meta-ads/sync`` both call ``run_sync`` in a worker thread.
HTTP calls have a timeout and bounded retries; the ClickHouse mutation uses the
same ``ALTER TABLE ... UPDATE ... SETTINGS mutations_sync = 1`` mechanism as the
retroactive cost tool. No error is ever raised into a request path or a loop tick.
"""
import asyncio
import json
import threading
from datetime import datetime, date, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db, SessionLocal
from tenant_context import current_tenant
from tenant_settings import for_each_tenant
from graph_version import DEFAULT_GRAPH_VERSION
from env_config import meta_graph_base_url, meta_graph_version
from models.settings import SettingsORM
from clickHouse import get_clickhouse_client

router = APIRouter()

PLATFORM = "meta"
DEFAULT_GRAPH_BASE = "https://graph.facebook.com"
HOURLY_SECONDS = 60 * 60
DAILY_SECONDS = 24 * 60 * 60
HTTP_TIMEOUT_SECONDS = 15
MAX_HTTP_ATTEMPTS = 3          # 1 try + 2 retries on network / 5xx / 429
MAX_PAGING_PAGES = 100         # safety cap when following paging.next verbatim
MATCH_PREFERENCES = ("auto", "ad_platform_campaign_id", "name", "tracking_id")
MAX_AD_ACCOUNT_IDS = 100      # safety cap on the chosen account list

# Settings block defaults — the shape saved under the 'meta_ads' settings key.
DEFAULTS = {
    "enabled": False,
    "ad_account_ids": [],
    "access_token": "",
    "api_version": DEFAULT_GRAPH_VERSION,
    "dry_run": True,           # default ON — a fresh install never writes costs
    "cadence": "hourly",       # hourly | daily
    "backfill_days": 7,
    "graph_base_url": "",      # empty -> the real Graph host
    "match_preference": "auto",
}

# Heartbeats / last-run state surfaced on /api/meta-ads/status and the system
# status page. In-memory only — a restart clears them. Keyed by tenant: the
# loop sweeps every workspace, so "the last run" is per tenant.
_loop_last_run = None
_last_sync_ats: dict = {}
_last_results: dict = {}
_last_errors: dict = {}
_running = False
_state_lock = threading.Lock()


def _record_run(result=None, error=object(), sync_at=None):
    """Store this tenant's last sync state (error=object() means 'leave as is')."""
    tid = current_tenant()
    if result is not None:
        _last_results[tid] = result
    if error is not object():
        _last_errors[tid] = error
    if sync_at is not None:
        _last_sync_ats[tid] = sync_at


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    """Current meta_ads block merged over DEFAULTS (never raises).

    When the block carries no token, the token stored by the OAuth Connect flow
    for this workspace is used instead: that flow requests ``ads_management``,
    so a connected Meta account can drive the cost sync and the campaign controls
    without an operator pasting a second (System User) token. An explicit token in
    the block always wins. Only the internal callers see this — the settings
    document, the status endpoint and the masked request views never carry it.
    """
    cfg = dict(DEFAULTS)
    db = SessionLocal()
    try:
        row = db.query(SettingsORM).filter_by(name="settings").first()
        if row and row.value:
            block = (json.loads(row.value) or {}).get("meta_ads")
            if isinstance(block, dict):
                for k, v in block.items():
                    if k in cfg and v is None:
                        continue
                    cfg[k] = v
    except Exception:
        pass
    finally:
        db.close()
    cfg = _normalise(cfg)
    # The Graph host and API version belong to the deployment: an environment
    # override wins over anything stored, so an operator updates every
    # workspace at once and tenants are never asked about Graph internals.
    cfg["api_version"] = meta_graph_version(cfg["api_version"]) or DEFAULT_GRAPH_VERSION
    cfg["graph_base_url"] = meta_graph_base_url(cfg["graph_base_url"])
    if not cfg.get("access_token"):
        cfg["access_token"] = connected_meta_token()
    return cfg


def connected_meta_token() -> str:
    """The Meta token the OAuth Connect flow stored for this workspace, or "".

    Decryption failures (no key, wrong key) degrade to "" so callers report
    "not configured" rather than sending a broken token to Graph.
    """
    try:
        from app_pages.integrations import _connection_row, decrypt_token
        db = SessionLocal()
        try:
            row = _connection_row(db, "meta")
            if row is None:
                return ""
            # Rows from _connection_row are unpacked positionally elsewhere in the
            # codebase (integrations._connection_view); access_token is the second
            # column of that SELECT.
            _platform, stored = row[0], row[1]
        finally:
            db.close()
        return decrypt_token(stored) or ""
    except Exception:
        return ""


def _normalise(cfg: dict) -> dict:
    """Coerce a stored block into usable types without dropping unknown keys."""
    out = dict(cfg)
    out["enabled"] = bool(out.get("enabled", False))
    out["dry_run"] = bool(out.get("dry_run", True))
    ids = out.get("ad_account_ids")
    if isinstance(ids, str):
        ids = [ids]
    out["ad_account_ids"] = [str(i).strip() for i in (ids or []) if str(i).strip()]
    out["access_token"] = str(out.get("access_token") or "").strip()
    out["api_version"] = str(out.get("api_version") or DEFAULT_GRAPH_VERSION).strip()
    out["cadence"] = "daily" if str(out.get("cadence")).lower() == "daily" else "hourly"
    try:
        out["backfill_days"] = max(0, int(out.get("backfill_days", 7)))
    except (TypeError, ValueError):
        out["backfill_days"] = 7
    out["graph_base_url"] = str(out.get("graph_base_url") or "").strip()
    if out.get("match_preference") not in MATCH_PREFERENCES:
        out["match_preference"] = "auto"
    return out


def _credentials_ready(cfg: dict) -> bool:
    return bool(cfg.get("access_token")) and bool(cfg.get("ad_account_ids"))


def _normalise_ad_account_ids(values):
    """(ids, bad_entry): numeric-only, deduped, order-preserving, capped at 100.

    Accepts ``act_123`` and ``123`` alike — the stored value is always the
    numeric part. Returns ``([], bad_entry)`` on the first entry that is not
    numeric (so the caller can reject it by name), including an empty entry.
    """
    if not isinstance(values, list):
        return [], values
    out, seen = [], set()
    for raw in values:
        text = str(raw).strip() if raw is not None else ""
        number = text[4:].strip() if text[:4].lower() == "act_" else text
        if not number or not (number.isascii() and number.isdigit()):
            return [], text
        if number in seen:
            continue
        seen.add(number)
        out.append(number)
        if len(out) >= MAX_AD_ACCOUNT_IDS:
            break
    return out, None


def _write_ad_account_ids(db: Session, ids: list) -> None:
    """Set ``meta_ads.ad_account_ids`` in the settings document.

    Reuses the one settings row (no second store): the existing ``meta_ads``
    block is preserved except for the list itself, so the cost sync's other
    settings (token, cadence, matching, …) are never disturbed.
    """
    row = db.execute(text(
        "SELECT id, value FROM settings "
        "WHERE name = 'settings' AND tenant_id = :tid FOR UPDATE"),
        {"tid": current_tenant()}).fetchone()
    doc = {}
    if row and row[1]:
        try:
            parsed = json.loads(row[1])
            if isinstance(parsed, dict):
                doc = parsed
        except Exception:
            doc = {}
    block = doc.get("meta_ads")
    block = dict(block) if isinstance(block, dict) else {}
    block["ad_account_ids"] = list(ids)
    doc["meta_ads"] = block
    val_str = json.dumps(doc)
    if row:
        db.execute(text("UPDATE settings SET value = :v WHERE id = :i"),
                   {"v": val_str, "i": row[0]})
    else:
        db.add(SettingsORM(name="settings", value=val_str))
    db.commit()


# ---------------------------------------------------------------------------
# Graph client
# ---------------------------------------------------------------------------

def _insights_url(cfg: dict, account_id: str) -> str:
    base = (cfg.get("graph_base_url") or DEFAULT_GRAPH_BASE).rstrip("/")
    return f"{base}/{cfg['api_version']}/act_{account_id}/insights"


def build_insights_request(cfg: dict, account_id: str) -> dict:
    """The exact query the sync will issue for one ad account.

    Uses an explicit JSON ``time_range`` (backfill window) whenever
    ``backfill_days`` > 0, otherwise Meta's ``date_preset=yesterday``. The
    fields list is the contract: campaign_id,campaign_name,spend,impressions,clicks.
    """
    params = {
        "level": "campaign",
        "time_increment": 1,
        "fields": "campaign_id,campaign_name,spend,impressions,clicks",
        "access_token": cfg.get("access_token") or "",
    }
    backfill = int(cfg.get("backfill_days") or 0)
    if backfill > 0:
        until = date.today()
        since = until - timedelta(days=backfill)
        params["time_range"] = json.dumps({"since": since.isoformat(),
                                           "until": until.isoformat()})
    else:
        params["date_preset"] = "yesterday"
    return {"url": _insights_url(cfg, account_id), "params": params}


def _masked_request(req: dict) -> dict:
    """Same-shaped request with the token redacted — safe to log / return."""
    out = {"url": req["url"], "params": dict(req["params"])}
    if "access_token" in out["params"]:
        out["params"]["access_token"] = "\u2022" * 8
    return out


def _get_with_retries(client: httpx.Client, url: str, params: dict):
    """GET with bounded retries + backoff on network errors / 5xx / 429.

    Returns (response_or_None, attempts, error_str). Never raises.
    """
    delay = 0.25
    last_err = ""
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        try:
            resp = client.get(url, params=params)
            if resp.status_code >= 500 or resp.status_code == 429:
                last_err = f"HTTP {resp.status_code}"
                if attempt < MAX_HTTP_ATTEMPTS:
                    import time as _t
                    _t.sleep(delay)
                    delay *= 2
                    continue
                return None, attempt, last_err
            return resp, attempt, ""
        except Exception as e:  # network / timeout
            last_err = repr(e)[:200]
            if attempt < MAX_HTTP_ATTEMPTS:
                import time as _t
                _t.sleep(delay)
                delay *= 2
                continue
            return None, attempt, last_err
    return None, MAX_HTTP_ATTEMPTS, last_err


def fetch_insights(cfg: dict, account_id: str):
    """Fetch every insight row for one account, following paging.next verbatim.

    Returns (rows, attempts, error). ``rows`` is [] on error; the caller decides
    whether that is fatal (it is recorded, never raised).
    """
    req = build_insights_request(cfg, account_id)
    rows = []
    attempts = 0
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
    with httpx.Client(timeout=timeout) as client:
        resp, attempts, err = _get_with_retries(client, req["url"], req["params"])
        if resp is None:
            return [], attempts, err
        if resp.status_code != 200:
            return [], attempts, f"HTTP {resp.status_code}: {resp.text[:200]}"
        pages = 0
        nxt = None
        data = _safe_json(resp)
        while pages < MAX_PAGING_PAGES:
            pages += 1
            for row in (data.get("data") or []):
                if isinstance(row, dict):
                    rows.append(row)
            nxt = ((data.get("paging") or {}).get("next") or "").strip()
            if not nxt:
                break
            # paging.next is an absolute URL carrying its own query string —
            # follow it verbatim (no extra params, no token injection).
            nresp, nattempts, nerr = _get_with_retries(client, nxt, None)
            attempts += nattempts
            if nresp is None or nresp.status_code != 200:
                return rows, attempts, nerr or f"HTTP {nresp.status_code if nresp else '?'}"
            data = _safe_json(nresp)
        return rows, attempts, ""


def _safe_json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Campaign matching
# ---------------------------------------------------------------------------

def _tracking_identifiers(config: dict) -> set:
    """The campaign's configured utm_campaign / sub_id-style identifiers.

    Sources: explicit top-level config keys (``utm_campaign``, ``sub_id_1`` …)
    plus ``config.paramsIdMapping`` entries whose ``parameter`` is a tracking id
    and whose ``token``/``value`` is a literal (macros like {{…}}/{…} are
    template placeholders, not matchable values, so they are skipped).
    """
    out = set()
    if isinstance(config, dict):
        for key in list(config.keys()):
            if key == "utm_campaign" or key.startswith("sub_id") or key == "subid":
                val = config.get(key)
                if isinstance(val, str) and val.strip() and not _is_macro(val):
                    out.add(val.strip())
        for item in (config.get("paramsIdMapping") or []):
            if not isinstance(item, dict):
                continue
            param = str(item.get("parameter") or "").strip().lower()
            if not (param == "utm_campaign" or param.startswith("sub_id") or param == "subid"):
                continue
            for key in ("token", "value"):
                val = item.get(key)
                if isinstance(val, str) and val.strip() and not _is_macro(val):
                    out.add(val.strip())
    return out


def _is_macro(value: str) -> bool:
    v = value.strip()
    return ("{" in v or "}" in v)


def _build_index(db: Session) -> dict:
    """Index every non-archived campaign for the three match strategies."""
    rows = db.execute(text(
        "SELECT id, name, ad_platform_campaign_id, config FROM campaigns "
        "WHERE archived = false AND tenant_id = :tid"),
        {"tid": current_tenant()}).fetchall()
    by_id, by_name, by_name_ci, by_tracking = {}, {}, {}, {}
    for cid, name, platform_id, config in rows:
        if platform_id:
            by_id.setdefault(str(platform_id).strip(), cid)
        if name:
            by_name.setdefault(name, cid)
            by_name_ci.setdefault(name.strip().lower(), cid)
        for ident in _tracking_identifiers(config):
            by_tracking.setdefault(ident, cid)
            by_tracking.setdefault(ident.lower(), cid)
    return {"id": by_id, "name": by_name, "name_ci": by_name_ci,
            "tracking": by_tracking}


def match_insight(index: dict, platform_campaign_id: str, campaign_name: str,
                  preference: str = "auto"):
    """Resolve one Graph insight row to a tracker campaign id.

    Returns ``(campaign_id, strategy)``; strategy is one of
    ``ad_platform_campaign_id`` / ``name`` / ``name_ci`` / ``tracking_id`` /
    ``None`` (unmatched). ``preference`` gates which strategies may run:
    ``auto`` runs all in order; the other values restrict to exactly one.
    """
    pref = preference if preference in MATCH_PREFERENCES else "auto"
    pid = (str(platform_campaign_id) or "").strip()
    name = (str(campaign_name) or "").strip()

    if pref in ("auto", "ad_platform_campaign_id") and pid:
        cid = index["id"].get(pid)
        if cid is not None:
            return cid, "ad_platform_campaign_id"
    if pref in ("auto", "name") and name:
        cid = index["name"].get(name)
        if cid is not None:
            return cid, "name"
        cid = index["name_ci"].get(name.lower())
        if cid is not None:
            return cid, "name_ci"
    if pref in ("auto", "tracking_id"):
        candidates = []
        if pid:
            candidates.append(pid)
        if name:
            candidates.append(name)
            candidates.append(name.lower())
        for cand in candidates:
            cid = index["tracking"].get(cand)
            if cid is not None:
                return cid, "tracking_id"
    return None, None


# ---------------------------------------------------------------------------
# Persistence + allocation
# ---------------------------------------------------------------------------

def _upsert_daily_row(db: Session, account_id: str, row: dict, matched_campaign_id):
    db.execute(text("""
        INSERT INTO ad_cost_daily
            (platform, ad_account_id, platform_campaign_id, campaign_name, date,
             spend, impressions, clicks, matched_campaign_id, synced_at, tenant_id)
        VALUES (:platform, :acct, :pcid, :cname, :day,
                :spend, :impressions, :clicks, :matched, now(), :tid)
        ON CONFLICT (tenant_id, platform, ad_account_id, platform_campaign_id, date)
        DO UPDATE SET campaign_name = EXCLUDED.campaign_name,
                      spend = EXCLUDED.spend,
                      impressions = EXCLUDED.impressions,
                      clicks = EXCLUDED.clicks,
                      matched_campaign_id = EXCLUDED.matched_campaign_id,
                      synced_at = now()
    """), {"platform": PLATFORM, "acct": account_id, "pcid": row["platform_campaign_id"],
           "cname": row["campaign_name"], "day": row["date"], "spend": row["spend"],
           "impressions": row["impressions"], "clicks": row["clicks"],
           "matched": matched_campaign_id, "tid": current_tenant()})


def _click_counts(ch, campaign_dates: dict) -> dict:
    """{(campaign_id, day_iso): click_count} for the matched campaign+days.

    ``clicks`` throughout this module means the tracker's click rows
    (``click = true``) — the same rows the allocation UPDATE targets.
    """
    if not campaign_dates:
        return {}
    cids = tuple(campaign_dates.keys())
    days = sorted({d for dates in campaign_dates.values() for d in dates})
    out = {}
    try:
        rows = ch.query(
            "SELECT campaign_id, toString(toDate(received_at)) AS day, "
            "countIf(click = true) AS clicks FROM clicks_data "
            "WHERE tenant_id = %(tenant_id)s "
            "AND campaign_id IN %(cids)s AND toString(toDate(received_at)) IN %(days)s "
            "GROUP BY campaign_id, day",
            parameters={"cids": cids, "days": tuple(days),
                        "tenant_id": current_tenant()}).result_rows
        for cid, day, clicks in rows:
            out[(int(cid), str(day))] = int(clicks or 0)
    except Exception as e:
        print("meta-ads: click-count query failed:", repr(e))
    return out


def _allocate(ch, campaign_id: int, day: str, spend: float, clicks: int) -> int:
    """Write per-click cost for one campaign+day. Returns rows touched.

    Absolute UPDATE (replace, never add) with mutations_sync=1 so a re-run
    overwrites the day rather than stacking. ``clicks`` is that day's tracker
    click-row count, so total allocated cost == the platform's spend.
    """
    if clicks <= 0:
        return 0
    per_click = float(spend) / clicks
    ch.command(
        "ALTER TABLE clicks_data UPDATE cost = %(cost)s "
        "WHERE campaign_id = %(cid)s AND tenant_id = %(tenant_id)s "
        "AND toDate(received_at) = toDate(%(day)s) AND click = true",
        parameters={"cost": per_click, "cid": int(campaign_id), "day": str(day),
                    "tenant_id": current_tenant()},
        settings={"mutations_sync": 1})
    return clicks


def _impression_cost_campaigns(db: Session) -> set:
    """Tracker campaign ids whose traffic source has impression cost sync on.

    A campaign whose ``traffic_source_id`` points at a channel with the flag
    set keeps a day's platform spend even when it has zero tracker clicks —
    the spend is recorded on one synthetic system click instead of vanishing.
    """
    try:
        rows = db.execute(text(
            "SELECT c.id FROM campaigns c "
            "JOIN capi_channel_settings s ON s.source_id = c.traffic_source_id "
            "WHERE c.tenant_id = :tid AND s.impression_cost_sync = true"),
            {"tid": current_tenant()}).fetchall()
        return {int(r[0]) for r in rows}
    except Exception as e:
        print("meta-ads: impression cost-sync lookup failed:", repr(e))
        return set()


def _record_impression_cost(ch, campaign_id: int, day: str, spend: float) -> int:
    """Record ONE synthetic system click carrying a day's whole spend.

    Impression-only campaigns have platform spend but no tracker click rows, so
    the day's spend would otherwise disappear. The row is obviously
    system-generated (visitor/click id ``system-meta-cost-…``) and re-running
    replaces it for the same campaign+day (delete + insert), never stacking.
    """
    cid = int(campaign_id)
    tenant = int(current_tenant())
    marker = f"system-meta-cost-{cid}-{day}"
    ch.command(
        "ALTER TABLE clicks_data DELETE WHERE tenant_id = %(tenant_id)s "
        "AND campaign_id = %(cid)s AND toDate(received_at) = toDate(%(day)s) "
        "AND click_id = %(marker)s",
        parameters={"tenant_id": tenant, "cid": cid, "day": str(day),
                    "marker": marker},
        settings={"mutations_sync": 1})
    try:
        received = datetime.strptime(str(day), "%Y-%m-%d")
    except (TypeError, ValueError):
        received = datetime.utcnow()
    ch.insert(
        "clicks_data",
        [[received, cid, tenant, True, "system", marker, marker, float(spend)]],
        column_names=["received_at", "campaign_id", "tenant_id", "click",
                      "status", "visitor_id", "click_id", "cost"])
    return 1


# ---------------------------------------------------------------------------
# The sync itself
# ---------------------------------------------------------------------------

def run_sync(trigger: str = "manual", dry_run=None) -> dict:
    """One full sync pass. Safe to call from the loop or the API — never raises.

    With ``dry_run`` true (the default unless explicitly overridden) it still
    issues the read-only Graph request and computes the matching, logs the
    request it would send and what it would allocate, and writes NOTHING: no
    Postgres rows and no ClickHouse mutation.
    """
    global _running
    if not _state_lock.acquire(blocking=False):
        return {"status": "skipped", "reason": "already_running"}
    try:
        _running = True
        cfg = load_settings()
        if not cfg["enabled"]:
            res = {"status": "skipped", "reason": "disabled"}
            _record_run(result=res)
            return res
        if not _credentials_ready(cfg):
            res = {"status": "skipped", "reason": "missing_credentials"}
            _record_run(result=res)
            return res

        is_dry = cfg["dry_run"] if dry_run is None else bool(dry_run)
        db = SessionLocal()
        ch = None
        try:
            index = _build_index(db)
            summary = {
                "status": "dry_run" if is_dry else "ok",
                "dry_run": is_dry,
                "trigger": trigger,
                "accounts": 0, "rows": 0, "matched": 0, "unmatched": 0,
                "allocated_campaigns": 0, "zero_click_days": 0,
                "impression_only_days": 0,
                "updated_rows": 0, "errors": [],
                "requests": [], "matches": [], "unmatched_rows": [],
            }
            alloc_targets = []  # (campaign_id, day, spend)
            for account_id in cfg["ad_account_ids"]:
                summary["accounts"] += 1
                req = build_insights_request(cfg, account_id)
                # Always record the request (token masked) so dry-run is auditable.
                summary["requests"].append(_masked_request(req))
                print(f"meta-ads[{trigger}]: GET {req['url']} "
                      f"params={_masked_request(req)['params']}")
                rows, attempts, err = fetch_insights(cfg, account_id)
                summary.setdefault("attempts", 0)
                summary["attempts"] += attempts
                if err:
                    summary["errors"].append({"account_id": account_id, "error": err})
                    print(f"meta-ads[{trigger}]: account {account_id} error: {err}")
                    continue
                for raw in rows:
                    parsed = _parse_insight_row(raw, account_id)
                    if parsed is None:
                        continue
                    summary["rows"] += 1
                    cid, strategy = match_insight(
                        index, parsed["platform_campaign_id"],
                        parsed["campaign_name"], cfg.get("match_preference", "auto"))
                    decision = {"platform_campaign_id": parsed["platform_campaign_id"],
                                "campaign_name": parsed["campaign_name"],
                                "date": parsed["date"], "spend": parsed["spend"],
                                "matched_campaign_id": cid, "strategy": strategy,
                                "ad_account_id": account_id}
                    if cid is None:
                        summary["unmatched"] += 1
                        summary["unmatched_rows"].append(decision)
                    else:
                        summary["matched"] += 1
                        if len(summary["matches"]) < 50:
                            summary["matches"].append(decision)
                        alloc_targets.append((cid, parsed["date"], parsed["spend"]))

                    if is_dry:
                        continue  # writes nothing at all
                    parsed["matched_campaign_id"] = cid
                    _upsert_daily_row(db, account_id, parsed, cid)
            if is_dry:
                # Report the allocation the run WOULD have done, without touching CH.
                _describe_allocation(alloc_targets, summary)
                _record_run(result=summary, error=None, sync_at=datetime.utcnow())
                return summary

            db.commit()
            # ClickHouse allocation (off the request path, mutations_sync=1).
            ch = get_clickhouse_client()
            try:
                impression_sync_cids = _impression_cost_campaigns(db)
                _describe_allocation(alloc_targets, summary, execute=True, ch=ch,
                                     impression_sync_cids=impression_sync_cids)
            finally:
                ch.close()
            _record_run(result=summary, error=None, sync_at=datetime.utcnow())
            return summary
        except Exception as e:
            db.rollback()
            print("meta-ads: sync error:", repr(e))
            res = {"status": "error", "dry_run": is_dry, "trigger": trigger,
                   "error": repr(e)[:300]}
            _record_run(result=res, error=repr(e)[:300])
            return res
        finally:
            db.close()
    finally:
        _running = False
        _state_lock.release()


def _parse_insight_row(raw: dict, account_id: str):
    """Normalise one Graph insight row; returns None when it carries no date."""
    day = str(raw.get("date_start") or "").strip()
    if not day:
        return None
    try:
        spend = float(raw.get("spend") or 0)
    except (TypeError, ValueError):
        spend = 0.0
    try:
        impressions = int(float(raw.get("impressions") or 0))
    except (TypeError, ValueError):
        impressions = 0
    try:
        clicks = int(float(raw.get("clicks") or 0))
    except (TypeError, ValueError):
        clicks = 0
    return {
        "platform_campaign_id": str(raw.get("campaign_id") or "").strip(),
        "campaign_name": str(raw.get("campaign_name") or "").strip(),
        "date": day, "spend": spend, "impressions": impressions, "clicks": clicks,
        "ad_account_id": account_id,
    }


def _describe_allocation(targets, summary: dict, execute: bool = False, ch=None,
                         impression_sync_cids=None) -> None:
    """Either report or perform (execute=True) the per campaign+day allocation.

    ``impression_sync_cids`` are the campaigns whose channel enabled impression
    cost sync: a day they spent on with zero tracker clicks gets one synthetic
    system click carrying the spend instead of being dropped."""
    impression_sync_cids = impression_sync_cids or set()
    grouped = {}
    for cid, day, spend in targets:
        grouped.setdefault((cid, day), spend)
    if execute and ch is not None:
        counts = _click_counts(ch, {cid: {day} for (cid, day) in grouped})
        for (cid, day), spend in grouped.items():
            clicks = counts.get((int(cid), str(day)), 0)
            if clicks <= 0:
                if int(cid) in impression_sync_cids:
                    summary["updated_rows"] += _record_impression_cost(
                        ch, cid, day, spend)
                    summary["impression_only_days"] += 1
                else:
                    summary["zero_click_days"] += 1
                continue
            rows = _allocate(ch, cid, day, spend, clicks)
            summary["allocated_campaigns"] += 1
            summary["updated_rows"] += rows
    else:
        # Dry-run: describe without any ClickHouse read/mutation.
        summary["would_allocate"] = [
            {"campaign_id": cid, "date": day, "spend": round(spend, 4)}
            for (cid, day), spend in list(grouped.items())[:50]
        ]


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def _tenant_meta_ads_sync(tenant_id: int, intervals: list) -> None:
    """Run one tenant's cost sync (inside its context, disabled/missing creds skipped)."""
    global _loop_last_run
    cfg = load_settings()
    intervals.append(DAILY_SECONDS if cfg["cadence"] == "daily" else HOURLY_SECONDS)
    if not (cfg["enabled"] and _credentials_ready(cfg)):
        return
    result = await asyncio.to_thread(run_sync, "loop")
    _loop_last_run = datetime.utcnow()
    print(f"Meta Ads sync (tenant {tenant_id}, {cfg['cadence']}): "
          f"{result.get('status')} matched={result.get('matched')} "
          f"unmatched={result.get('unmatched')}")


async def meta_ads_loop():
    """Scheduled cost sync honouring each tenant's configured cadence.

    Sweeps every active tenant sequentially; per tenant it is skipped entirely
    (no HTTP, no write) when disabled or when the token / account ids are
    missing. A run never overlaps: ``run_sync`` takes a non-blocking lock and
    returns ``already_running`` if one is in flight. The next sleep is the
    shortest cadence any tenant asked for, so a tenant configured daily is not
    re-synced hourly.
    """
    await asyncio.sleep(120)  # stagger behind monitor (60) / rules (90) / optimizer (150)
    while True:
        intervals = []
        try:
            await for_each_tenant(lambda tid: _tenant_meta_ads_sync(tid, intervals))
        except Exception as e:
            print("Meta Ads loop error:", repr(e))
        await asyncio.sleep(min(intervals) if intervals else HOURLY_SECONDS)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.post("/sync")
async def sync_now(request: Request):
    """Run one cost sync immediately (admin/settings plane). Never raises."""
    from audit_logger import audit_event
    from auth import get_caller
    result = await asyncio.to_thread(run_sync, "manual")
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "meta_ads_sync", "meta_ads", "",
                {"status": result.get("status"), "dry_run": result.get("dry_run"),
                 "matched": result.get("matched"), "unmatched": result.get("unmatched")},
                request.client.host if request.client else "")
    return result


@router.get("/status")
def status(db: Session = Depends(get_db)):
    """Enabled/dry-run/cadence + last run state, matched/unmatched.

    The Graph endpoint and token are deployment-owned and are not reported."""
    cfg = load_settings()
    tid = current_tenant()
    last_result = _last_results.get(tid)
    last_error = _last_errors.get(tid)
    last_sync_at = _last_sync_ats.get(tid)
    return {
        "enabled": cfg["enabled"],
        "dry_run": cfg["dry_run"],
        "cadence": cfg["cadence"],
        "api_version": cfg["api_version"],
        "ad_account_ids": cfg["ad_account_ids"],
        "match_preference": cfg["match_preference"],
        "backfill_days": cfg["backfill_days"],
        "running": _running,
        "last_sync_at": last_sync_at.isoformat() if last_sync_at else None,
        "last_result": last_result,
        "last_error": last_error,
        "matched": (last_result or {}).get("matched"),
        "unmatched": (last_result or {}).get("unmatched"),
        "loop_last_run": _loop_last_run.isoformat() if _loop_last_run else None,
        "last_control": _last_control_snapshot(),
    }


class AdAccountsIn(BaseModel):
    ad_account_ids: list = []


@router.post("/accounts")
def set_ad_accounts(payload: AdAccountsIn, request: Request,
                    db: Session = Depends(get_db)):
    """Replace this workspace's Meta ad account list (chosen under Integrations).

    Body ``{"ad_account_ids": ["act_123", "456", …]}``. Each entry is trimmed, an
    ``act_`` prefix is dropped and only the numeric part is stored; the list is
    deduped (first occurrence wins), order-preserving and capped at 100. A
    non-numeric entry is rejected 400 naming it. The result is written into the
    settings document's ``meta_ads`` block as ``ad_account_ids`` — the block's
    other keys are left untouched. The cost sync reads this same list, so the
    Integrations card is the single place it is chosen (owner/admin only, via
    the router's ``require_section_write("settings")`` dependency).
    """
    from audit_logger import audit_event
    from auth import get_caller
    ids, bad = _normalise_ad_account_ids(payload.ad_account_ids)
    if bad is not None:
        raise HTTPException(status_code=400, detail=(
            f"'{bad}' is not a valid ad account id — use the numeric part of "
            "act_… or digits only."))
    _write_ad_account_ids(db, ids)
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "meta_ads_accounts", "meta_ads", "",
                {"ad_account_ids": ids, "count": len(ids)},
                request.client.host if request.client else "")
    return {"ad_account_ids": ids, "count": len(ids)}


# ---------------------------------------------------------------------------
# Ad-platform control surface (pause / resume a campaign and its ad sets)
# ---------------------------------------------------------------------------
#
# Reuses the Graph host/version/token/retry/masking helpers from the cost sync
# above — there is deliberately no second Graph client. WRITES (the status
# POSTs) are dry-run by default (``meta_ads.dry_run``): they log the exact
# method/URL/body they *would* send and return the intended change without
# issuing an HTTP write. READS (listing a campaign's ad sets) still run in
# dry-run so the UI can show what a bulk action would change.
#
# Graph calls used (nothing else):
#   GET  {base}/{ver}/{campaign_id}/adsets?fields=id,name,status&limit=100
#   POST {base}/{ver}/{campaign_id}  body status=ACTIVE|PAUSED
#   POST {base}/{ver}/{adset_id}     body status=ACTIVE|PAUSED

_last_control = None


class StatusIn(BaseModel):
    status: str
    include_adsets: bool = False   # the preferred bulk form (campaign + its ad sets)


def _status_url(cfg: dict, object_id: str) -> str:
    base = (cfg.get("graph_base_url") or DEFAULT_GRAPH_BASE).rstrip("/")
    return f"{base}/{cfg['api_version']}/{object_id}"


def build_adsets_request(cfg: dict, platform_id: str) -> dict:
    """The exact ad-set listing query (fields=id,name,status, limit=100)."""
    return {
        "method": "GET",
        "url": f"{_status_url(cfg, platform_id)}/adsets",
        "params": {"fields": "id,name,status", "limit": "100",
                   "access_token": cfg.get("access_token") or ""},
    }


def build_status_request(cfg: dict, object_id: str, status: str) -> dict:
    """The exact status POST for a campaign or an ad set."""
    return {
        "method": "POST",
        "url": _status_url(cfg, object_id),
        "body": {"status": status, "access_token": cfg.get("access_token") or ""},
    }


def _masked_call(req: dict) -> dict:
    """Same-shaped request with the token redacted — safe to log / return."""
    out = {"method": req.get("method", "GET"), "url": req["url"]}
    for key in ("params", "body"):
        if key in req:
            out[key] = {k: ("\u2022" * 8 if k == "access_token" else v)
                        for k, v in req[key].items()}
    return out


def _post_with_retries(client: httpx.Client, url: str, data: dict):
    """POST with bounded retries + backoff on network errors / 5xx / 429.

    Returns (response_or_None, attempts, error_str). Never raises.
    """
    import time as _t
    delay = 0.25
    last_err = ""
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        try:
            resp = client.post(url, data=data)
            if resp.status_code >= 500 or resp.status_code == 429:
                last_err = f"HTTP {resp.status_code}"
                if attempt < MAX_HTTP_ATTEMPTS:
                    _t.sleep(delay)
                    delay *= 2
                    continue
                return None, attempt, last_err
            return resp, attempt, ""
        except Exception as e:  # network / timeout
            last_err = repr(e)[:200]
            if attempt < MAX_HTTP_ATTEMPTS:
                _t.sleep(delay)
                delay *= 2
                continue
            return None, attempt, last_err
    return None, MAX_HTTP_ATTEMPTS, last_err


def _parse_adset_row(raw: dict) -> dict:
    status = str(raw.get("status") or "").strip().upper()
    return {
        "id": str(raw.get("id") or "").strip(),
        "name": str(raw.get("name") or "").strip(),
        "status": status,
        "paused": status == "PAUSED",
    }


def _extract_campaign_status(data: dict):
    """The parent campaign's status *when it is returned* by the ads edge.

    Meta's /adsets edge does not normally include the parent status, so this is
    best-effort: it only reports a status the response actually carries rather
    than issuing an extra Graph call outside the agreed call set.
    """
    for key in ("status", "campaign_status", "parent_status"):
        value = str(data.get(key) or "").strip().upper()
        if value in ("ACTIVE", "PAUSED"):
            return value
    return None


def fetch_adsets(cfg: dict, platform_id: str):
    """List a platform campaign's ad sets, following paging.next verbatim.

    Returns (adsets, campaign_status_or_None, attempts, error). Never raises.
    """
    req = build_adsets_request(cfg, platform_id)
    rows = []
    attempts = 0
    campaign_status = None
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
    with httpx.Client(timeout=timeout) as client:
        resp, attempts, err = _get_with_retries(client, req["url"], req["params"])
        if resp is None:
            return [], None, attempts, err
        if resp.status_code != 200:
            return [], None, attempts, f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = _safe_json(resp)
        campaign_status = _extract_campaign_status(data)
        pages = 0
        while pages < MAX_PAGING_PAGES:
            pages += 1
            for row in (data.get("data") or []):
                if isinstance(row, dict):
                    rows.append(_parse_adset_row(row))
            nxt = ((data.get("paging") or {}).get("next") or "").strip()
            if not nxt:
                break
            nresp, nattempts, nerr = _get_with_retries(client, nxt, None)
            attempts += nattempts
            if nresp is None or nresp.status_code != 200:
                return rows, campaign_status, attempts, \
                    nerr or f"HTTP {nresp.status_code if nresp else '?'}"
            data = _safe_json(nresp)
        return rows, campaign_status, attempts, ""


def set_object_status(cfg: dict, object_id: str, status: str, dry_run: bool) -> dict:
    """Pause/resume one Graph object (campaign or ad set). Never raises."""
    req = build_status_request(cfg, object_id, status)
    print(f"meta-ads control: POST {req['url']} "
          f"body={_masked_call(req)['body']}"
          f"{' (dry-run, not sent)' if dry_run else ''}")
    if dry_run:
        return {"dry_run": True, "ok": True, "object_id": object_id,
                "status": status, "would_send": _masked_call(req), "attempts": 0}
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
    with httpx.Client(timeout=timeout) as client:
        resp, attempts, err = _post_with_retries(client, req["url"], req["body"])
    if resp is None:
        return {"dry_run": False, "ok": False, "object_id": object_id,
                "status": status, "error": err, "attempts": attempts}
    if resp.status_code != 200:
        return {"dry_run": False, "ok": False, "object_id": object_id,
                "status": status, "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                "attempts": attempts}
    return {"dry_run": False, "ok": True, "object_id": object_id, "status": status,
            "attempts": attempts, "response": _safe_json(resp)}


def resolve_platform_campaign_id(db: Session, campaign_id):
    """Map a path value to a platform campaign id.

    Returns ``(platform_id, tracker_campaign_id, error, http_status)``.

    A numeric value is treated as a tracker campaign id: it must carry
    ``ad_platform_campaign_id`` or the call is rejected with a clear 400 (never
    a guessed campaign). A non-numeric value is treated as a platform campaign
    id and must resolve through the same fallbacks the cost sync uses
    (``match_insight`` over the ``_build_index`` name / tracking strategies).
    """
    raw = str(campaign_id or "").strip()
    if not raw:
        return None, None, "campaign_id is required", 400

    if raw.isdigit():
        row = db.execute(text(
            "SELECT id, name, ad_platform_campaign_id FROM campaigns "
            "WHERE id = :id AND tenant_id = :tid"),
            {"id": int(raw), "tid": current_tenant()}).fetchone()
        if row is not None:
            cid, name, pid = row
            if pid and str(pid).strip():
                return str(pid).strip(), int(cid), None, None
            return None, int(cid), (
                f"Campaign '{name or cid}' has no ad-platform campaign id mapping. "
                "Set the Meta campaign ID in the campaign editor first."), 400

    index = _build_index(db)
    matched_cid, _strategy = match_insight(index, raw, raw,
                                           load_settings().get("match_preference", "auto"))
    if matched_cid is not None:
        return raw, int(matched_cid), None, None
    if raw.isdigit():
        return None, None, "Campaign not found.", 404
    return None, None, (
        f"No tracker campaign is mapped to platform campaign '{raw}'."), 404


def _normalise_status(value) -> str:
    status = str(value or "").strip().upper()
    if status not in ("ACTIVE", "PAUSED"):
        raise HTTPException(status_code=400, detail="status must be ACTIVE or PAUSED")
    return status


def run_campaign_control(cfg: dict, platform_id: str, status: str,
                         include_adsets: bool = False, dry_run=None) -> dict:
    """Pause/resume a platform campaign (optionally with its ad sets).

    Never raises. In dry-run every write is simulated (no HTTP POST at all);
    the ad-set listing read still runs so the UI can show what would change.
    A failed campaign write aborts the group before any ad set is touched; a
    failed ad-set write stops the rest — the result reports exactly what was
    applied so nothing is silently half-applied.
    """
    is_dry = cfg["dry_run"] if dry_run is None else bool(dry_run)
    result = {"dry_run": is_dry, "platform_campaign_id": platform_id,
              "status": status, "include_adsets": bool(include_adsets),
              "ok": True, "partial": False, "error": None,
              "campaign": None, "adsets": [], "requests": [], "attempts": 0}

    targets = []
    if include_adsets:
        adsets, campaign_status, attempts, error = fetch_adsets(cfg, platform_id)
        result["requests"].append(_masked_call(build_adsets_request(cfg, platform_id)))
        result["attempts"] += attempts
        result["campaign_status"] = campaign_status
        result["adsets_seen"] = len(adsets)
        if error:
            result["ok"] = False
            result["error"] = f"Could not list ad sets: {error}"
            return result
        targets = [a for a in adsets if a["status"] != status]

    campaign_req = build_status_request(cfg, platform_id, status)
    result["requests"].append(_masked_call(campaign_req))
    campaign_res = set_object_status(cfg, platform_id, status, is_dry)
    result["campaign"] = campaign_res
    result["attempts"] += campaign_res.get("attempts", 0)
    if not campaign_res.get("ok"):
        result["ok"] = False
        result["error"] = campaign_res.get("error")
        if include_adsets:
            result["adsets"] = [
                {"id": a["id"], "name": a["name"], "previous_status": a["status"],
                 "skipped": True, "reason": "campaign write failed"}
                for a in targets]
        return result

    for adset in targets:
        adset_req = build_status_request(cfg, adset["id"], status)
        result["requests"].append(_masked_call(adset_req))
        adset_res = set_object_status(cfg, adset["id"], status, is_dry)
        result["attempts"] += adset_res.get("attempts", 0)
        result["adsets"].append({"id": adset["id"], "name": adset["name"],
                                 "previous_status": adset["status"],
                                 "result": adset_res})
        if not adset_res.get("ok"):
            result["ok"] = False
            result["partial"] = True
            result["error"] = f"Ad set {adset['id']}: {adset_res.get('error')}"
            break
    return result


def _last_control_snapshot() -> dict:
    if not _last_control:
        return {}
    return dict(_last_control)


def _record_control(action: str, platform_id, status: str, result: dict,
                    tracker_campaign_id=None) -> None:
    global _last_control
    _last_control = {
        "at": datetime.utcnow().isoformat(),
        "action": action,
        "platform_campaign_id": platform_id,
        "tracker_campaign_id": tracker_campaign_id,
        "status": status,
        "dry_run": result.get("dry_run"),
        "ok": result.get("ok"),
        "partial": result.get("partial"),
        "error": result.get("error"),
    }


def _require_control_credentials() -> dict:
    cfg = load_settings()
    if not cfg.get("access_token"):
        raise HTTPException(status_code=400, detail=(
            "Meta Ads is not configured — add an access token in Settings "
            "before controlling campaigns."))
    return cfg


@router.get("/campaigns/{campaign_id}/adsets")
async def list_campaign_adsets(campaign_id: str, request: Request,
                               db: Session = Depends(get_db)):
    """List the platform campaign's ad sets + whether each is paused.

    A read: it runs even in dry-run so the UI can show the intended change.
    Returns a clear error when the tracker campaign has no platform mapping.
    """
    cfg = _require_control_credentials()
    platform_id, tracker_cid, error, code = resolve_platform_campaign_id(db, campaign_id)
    if error:
        raise HTTPException(status_code=code, detail=error)
    adsets, campaign_status, attempts, fetch_error = await asyncio.to_thread(
        fetch_adsets, cfg, platform_id)
    return {
        "dry_run": cfg["dry_run"],
        "campaign_id": str(campaign_id),
        "tracker_campaign_id": tracker_cid,
        "platform_campaign_id": platform_id,
        "campaign_status": campaign_status,
        "adsets": adsets,
        "attempts": attempts,
        "error": fetch_error or None,
        "request": _masked_call(build_adsets_request(cfg, platform_id)),
    }


@router.post("/campaigns/{campaign_id}/status")
async def set_campaign_status(campaign_id: str, data: StatusIn, request: Request,
                              db: Session = Depends(get_db)):
    """Pause/resume the platform campaign.

    Body ``{"status": "ACTIVE"|"PAUSED", "include_adsets": false}``. With
    ``include_adsets: true`` this is the bulk form — the campaign and every ad
    set that is not already at the target status are changed together (the
    real-world "pause the campaign" action).
    """
    from audit_logger import audit_event
    from auth import get_caller
    status = _normalise_status(data.status)
    cfg = _require_control_credentials()
    platform_id, tracker_cid, error, code = resolve_platform_campaign_id(db, campaign_id)
    if error:
        raise HTTPException(status_code=code, detail=error)
    result = await asyncio.to_thread(run_campaign_control, cfg, platform_id, status,
                                     data.include_adsets)
    result["campaign_id"] = str(campaign_id)
    result["tracker_campaign_id"] = tracker_cid
    _record_control("set_campaign_status", platform_id, status, result, tracker_cid)
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "meta_ads_campaign_status", "campaigns",
                str(tracker_cid or campaign_id),
                {"status": status, "include_adsets": data.include_adsets,
                 "dry_run": result.get("dry_run"), "ok": result.get("ok"),
                 "platform_campaign_id": platform_id},
                request.client.host if request.client else "")
    return result


@router.post("/adsets/{adset_id}/status")
async def set_adset_status(adset_id: str, data: StatusIn, request: Request,
                           db: Session = Depends(get_db)):
    """Pause/resume one platform ad set."""
    from audit_logger import audit_event
    from auth import get_caller
    status = _normalise_status(data.status)
    cfg = _require_control_credentials()
    result = await asyncio.to_thread(set_object_status, cfg, str(adset_id), status,
                                     cfg["dry_run"])
    result["adset_id"] = str(adset_id)
    _record_control("set_adset_status", str(adset_id), status, result)
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "meta_ads_adset_status", "campaigns",
                str(adset_id),
                {"status": status, "dry_run": result.get("dry_run"),
                 "ok": result.get("ok")},
                request.client.host if request.client else "")
    return result
