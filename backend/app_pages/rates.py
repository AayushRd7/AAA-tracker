"""Multi-currency reporting: the workspace's cached FX rate store.

Reporting sums money from two stores, and only some rows name a currency:

* ``conversions_data`` (Postgres) carries a per-row ``currency`` — the currency
  the offer/network paid in, attribute from the click or the postback.
* ``clicks_data`` (ClickHouse) carries a ``currency`` column too, but it is
  frequently empty (the tracking macro is optional); a row without one is
  treated as already being in the workspace's base currency.

A report must never make a live network call, so rates are fetched on a
schedule (and on demand from Settings) and persisted. We keep them in a
per-tenant settings row (``settings.fx_rates``) rather than a new table:

* ``settings`` is already tenant-scoped and unique on ``(tenant_id, name)``, so
  the tenant boundary holds with no extra filtering — a workspace picks its own
  base currency and its own conversions can differ.
* The blob is tiny (one JSON object) and read-only on the request path; a full
  table plus a boot migration (``app.py``) would be more machinery than the
  data warrants.
* The manual override and the fetched rate live in the same row, so a report
  reads one value to be honest about both the number and its provenance.

Store shape (all keys optional)::

    {
      "base": "USD",                     # reporting currency
      "currencies": ["EUR", "GBP"],      # codes the operator expects to convert
      "overrides": {"EUR": 0.95},        # pinned rates, win over fetched ones
      "stale_after_hours": 36,
      "fetched_at": "2026-10-02T12:00:00+00:00",
      "source": "frankfurter",           # which provider the rates came from
      "rates": {"EUR": 0.92, ...},       # units of CUR per 1 base
      "last_error": null                 # why the last refresh found no rates
    }

Convention: every stored rate is "units of that currency per 1 base". An amount
in ``CUR`` is converted to base by ``amount / rate[CUR]``; a currency equal to
the base (or absent from the store) is left untouched, so a workspace with no
``fx_rates`` row reports exactly as it did before this feature existed.

Providers, in order: Frankfurter (official central-bank data, no key), the ECB
raw daily XML, then open.er-api. ``api.exchangerate.host`` is deliberately not
used — it now requires an access key. If all providers fail the last good rates
are kept, ``last_error`` is recorded and the rates' age keeps growing so the
report can mark them stale rather than silently trusting them.
"""
import asyncio
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx
from sqlalchemy.orm import Session

from db import SessionLocal
from models.settings import SettingsORM
from tenant_settings import for_each_tenant

# Settings row name (per tenant). See the module docstring for why a row and
# not a table.
ROWS_NAME = "fx_rates"

# Daily cadence: a cached "daily rate" is enough for reporting, and it keeps the
# free providers' request count trivial.
REFRESH_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_STALE_AFTER_HOURS = 36
# Stagger behind the other background loops (monitor 60s, rules 90s, …).
STARTUP_DELAY_SECONDS = 240

FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ER_API_URL = "https://open.er-api.com/v6/latest/{base}"

HTTP_TIMEOUT = 15
# A currency code from an operator/provider is only ever interpolated into SQL
# after this check, so a hostile value can never become an expression.
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
MAX_CURRENCIES = 60
MAX_STALE_AFTER_HOURS = 24 * 30

_loop_last_run = None
_loop_started = False


# ---------------------------------------------------------------------------
# Store read / write
# ---------------------------------------------------------------------------

def load_store(db: Optional[Session] = None) -> dict:
    """This workspace's ``fx_rates`` store, or ``{}`` when never configured.

    A missing/None row, a malformed body or any read error yields ``{}`` — the
    caller then applies no conversion, which is exactly today's behaviour.
    """
    own = db is None
    if own:
        db = SessionLocal()
    try:
        row = db.query(SettingsORM).filter_by(name=ROWS_NAME).first()
        if row and row.value:
            data = json.loads(row.value)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    finally:
        if own:
            db.close()
    return {}


def save_store(store: dict, db: Optional[Session] = None) -> None:
    """Persist the store for the current workspace (upsert the settings row)."""
    own = db is None
    if own:
        db = SessionLocal()
    try:
        value = json.dumps(store or {})
        row = db.query(SettingsORM).filter_by(name=ROWS_NAME).first()
        if row:
            row.value = value
        else:
            db.add(SettingsORM(name=ROWS_NAME, value=value))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        if own:
            db.close()


# ---------------------------------------------------------------------------
# Validation / normalisation
# ---------------------------------------------------------------------------

def validate_config(payload: dict) -> dict:
    """Clean the operator-editable subset of the store.

    Only keys present in ``payload`` are returned, so a partial save never
    blanks a key it did not mention. Raises ``ValueError`` with a message the
    Settings endpoint turns into a 400."""
    if not isinstance(payload, dict):
        raise ValueError("FX configuration must be an object")
    out: Dict = {}
    if "base" in payload:
        base = str(payload.get("base") or "").strip().upper()
        if base and not _CURRENCY_RE.match(base):
            raise ValueError("base currency must be a 3-letter code (e.g. USD)")
        out["base"] = base
    if "currencies" in payload:
        raw = payload.get("currencies")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise ValueError("currencies must be a list of 3-letter codes")
        cleaned = []
        for item in raw:
            code = str(item or "").strip().upper()
            if not code:
                continue
            if not _CURRENCY_RE.match(code):
                raise ValueError(f"'{item}' is not a 3-letter currency code")
            if code not in cleaned:
                cleaned.append(code)
        if len(cleaned) > MAX_CURRENCIES:
            raise ValueError(f"at most {MAX_CURRENCIES} currencies can be converted")
        out["currencies"] = cleaned
    if "overrides" in payload:
        raw = payload.get("overrides")
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("overrides must be an object of currency -> rate")
        overrides = {}
        for key, value in raw.items():
            code = str(key or "").strip().upper()
            if not code:
                continue
            if not _CURRENCY_RE.match(code):
                raise ValueError(f"'{key}' is not a 3-letter currency code")
            if value in (None, ""):
                continue
            try:
                rate = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"override for {code} must be a number")
            if not rate > 0:
                raise ValueError(f"override for {code} must be greater than zero")
            overrides[code] = rate
        out["overrides"] = overrides
    if "stale_after_hours" in payload:
        try:
            hours = int(payload.get("stale_after_hours"))
        except (TypeError, ValueError):
            raise ValueError("stale_after_hours must be an integer")
        if not 1 <= hours <= MAX_STALE_AFTER_HOURS:
            raise ValueError(f"stale_after_hours must be between 1 and {MAX_STALE_AFTER_HOURS}")
        out["stale_after_hours"] = hours
    return out


# ---------------------------------------------------------------------------
# Rate lookup / conversion
# ---------------------------------------------------------------------------

def _base(store: dict) -> str:
    return str(store.get("base") or "").strip().upper()


def _stale_after_hours(store: dict) -> int:
    try:
        hours = int(store.get("stale_after_hours") or DEFAULT_STALE_AFTER_HOURS)
        if 1 <= hours <= MAX_STALE_AFTER_HOURS:
            return hours
    except (TypeError, ValueError):
        pass
    return DEFAULT_STALE_AFTER_HOURS


def effective_rates(store: dict) -> Dict[str, float]:
    """Fetched rates merged with overrides (an override always wins)."""
    out: Dict[str, float] = {}
    for source in (store.get("rates") or {}, store.get("overrides") or {}):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            code = str(key or "").strip().upper()
            try:
                rate = float(value)
            except (TypeError, ValueError):
                continue
            if code and rate > 0:
                out[code] = rate
    return out


def store_active(store: dict) -> bool:
    """True when a base and at least one rate are configured."""
    return bool(_base(store) and effective_rates(store))


def rate_for(store: dict, currency) -> Optional[float]:
    """Units of ``currency`` per 1 base, or None when it cannot be converted."""
    base = _base(store)
    code = str(currency or "").strip().upper()
    if not base or not code or not _CURRENCY_RE.match(code):
        return None
    if code == base:
        return 1.0
    return effective_rates(store).get(code)


def convert(amount, currency, store: dict):
    """Express ``amount`` (in ``currency``) in the store's base currency.

    Anything that cannot be converted — no store, a blank/base currency, an
    unknown rate — is returned unchanged, so a partially-configured workspace
    never zeroes out or garbles a number it has no rate for."""
    rate = rate_for(store, currency)
    if rate is None or rate <= 0 or amount in (None, ""):
        return amount
    try:
        return float(amount) / rate
    except (TypeError, ValueError):
        return amount


def convert_money(row: dict, currency, store: dict,
                  keys=("payout", "revenue", "profit")) -> dict:
    """Convert the money fields of a serialized row in place (and return it)."""
    if not store_active(store):
        return row
    for key in keys:
        if key in row and row[key] is not None:
            row[key] = convert(row[key], currency, store)
    return row


def sql_factor(store: dict, column: str = "currency") -> Optional[str]:
    """A SQL ``CASE`` multiplier that converts a row's money to base.

    Works in both Postgres and ClickHouse (this project uses it for both). The
    expression keys off the row's own ``currency`` column and defaults to ``1``
    for the base currency, a blank currency or an unknown code — i.e. a row that
    cannot be converted is summed as-is. Returns ``None`` when nothing is
    configured, so callers can keep their query byte-identical to before.
    """
    if not store_active(store):
        return None
    base = _base(store)
    rates = effective_rates(store)
    parts = []
    for code in sorted(rates):
        if code == base or not _CURRENCY_RE.match(code):
            continue
        factor = 1.0 / rates[code]
        parts.append(f"WHEN {column} = '{code}' THEN {factor!r}")
        if len(parts) >= MAX_CURRENCIES:
            break
    if not parts:
        return None
    return "CASE " + " ".join(parts) + " ELSE 1 END"


# ---------------------------------------------------------------------------
# Provenance / status
# ---------------------------------------------------------------------------

def _parse_iso(value) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def fx_meta(store: dict) -> Optional[dict]:
    """Report-facing provenance block, or None when no conversion is active."""
    if not store_active(store):
        return None
    fetched = _parse_iso(store.get("fetched_at"))
    now = datetime.now(timezone.utc)
    age = round((now - fetched).total_seconds() / 3600, 2) if fetched else None
    stale_hours = _stale_after_hours(store)
    return {
        "base": _base(store),
        "source": store.get("source") or "",
        "fetched_at": store.get("fetched_at"),
        "age_hours": age,
        "stale": age is None or age > stale_hours,
        "stale_after_hours": stale_hours,
        "last_error": store.get("last_error"),
        "override_count": len(store.get("overrides") or {}),
        "rate_count": len(effective_rates(store)),
    }


def status(db: Optional[Session] = None) -> dict:
    """Everything the Settings card (and an honest report) needs to show."""
    store = load_store(db)
    base = _base(store)
    if not base:
        # No row yet: show the workspace's General-settings currency as the
        # starting base, without activating conversion.
        base = _workspace_currency(db) if db is not None else "USD"
    meta = fx_meta(store)
    return {
        "active": bool(meta),
        "base": base,
        "currencies": list(store.get("currencies") or []),
        "overrides": dict(store.get("overrides") or {}),
        "stale_after_hours": _stale_after_hours(store),
        "source": store.get("source") or "",
        "fetched_at": store.get("fetched_at"),
        "age_hours": meta["age_hours"] if meta else None,
        "stale": meta["stale"] if meta else False,
        "last_error": store.get("last_error"),
        # The fetched rates and the effective (override-merged) view, so the
        # card can show "provider rate" next to an operator's pinned rate.
        "fetched_rates": dict(store.get("rates") or {}),
        "rates": effective_rates(store),
        "loop_last_run": _loop_last_run.isoformat() if _loop_last_run else None,
    }


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _normalize_rates(base: str, raw, currencies) -> Dict[str, float]:
    """Uppercase/validate a provider's ``{CUR: rate}`` map (units per base)."""
    wanted = {str(c).strip().upper() for c in (currencies or []) if str(c).strip()}
    out: Dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        code = str(key or "").strip().upper()
        if not _CURRENCY_RE.match(code) or code == base:
            continue
        if wanted and code not in wanted:
            continue
        try:
            rate = float(value)
        except (TypeError, ValueError):
            continue
        if rate > 0:
            out[code] = rate
    return out


def _fetch_frankfurter(base: str, currencies) -> Dict[str, float]:
    params = {"base": base}
    symbols = [c for c in (currencies or []) if str(c).strip().upper() != base]
    if symbols:
        params["symbols"] = ",".join(str(c).strip().upper() for c in symbols)
    resp = httpx.get(FRANKFURTER_URL, params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("unexpected response")
    return _normalize_rates(base, data.get("rates"), currencies)


def _fetch_ecb(base: str, currencies) -> Dict[str, float]:
    resp = httpx.get(ECB_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    eur_rates: Dict[str, float] = {"EUR": 1.0}
    for el in root.iter():
        code = el.get("currency")
        rate = el.get("rate")
        if not code or rate is None:
            continue
        try:
            eur_rates[str(code).upper()] = float(rate)
        except (TypeError, ValueError):
            continue
    if base not in eur_rates or not eur_rates[base]:
        raise RuntimeError(f"ECB feed has no {base} rate")
    # ECB is EUR-based: units-of-base-per-EUR is the cross divisor.
    per_base = {}
    for code, rate in _normalize_rates(base, eur_rates, currencies).items():
        per_base[code] = rate / eur_rates[base]
    return per_base


def _fetch_er_api(base: str, currencies) -> Dict[str, float]:
    resp = httpx.get(ER_API_URL.format(base=base), timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or data.get("result") not in (None, "success"):
        raise RuntimeError(str(data.get("error-type") or "request failed"))
    return _normalize_rates(base, data.get("rates"), currencies)


def fetch_rates(base: str, currencies) -> tuple:
    """First provider that answers wins -> ({CUR: rate}, source_name)."""
    errors = []
    for name, fetcher in (("frankfurter", _fetch_frankfurter),
                          ("ecb", _fetch_ecb),
                          ("open.er-api", _fetch_er_api)):
        try:
            rates = fetcher(base, currencies)
            if rates:
                return rates, name
            errors.append(f"{name}: empty response")
        except Exception as e:  # noqa: BLE001 — try the next provider
            errors.append(f"{name}: {e}")
    raise RuntimeError("; ".join(errors)[:300])


def _workspace_currency(db: Session) -> str:
    """The workspace's General-settings currency, used as the initial base."""
    try:
        row = db.query(SettingsORM).filter_by(name="settings").first()
        if row and row.value:
            code = str(json.loads(row.value).get("currency") or "").strip().upper()
            if _CURRENCY_RE.match(code):
                return code
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

def refresh(db: Optional[Session] = None) -> dict:
    """Fetch and persist rates for the current workspace. Never raises.

    On total provider failure the last good rates and their ``fetched_at`` are
    kept (so their age — and the report's ``stale`` flag — keeps growing) and
    the reason lands in ``last_error``."""
    own = db is None
    if own:
        db = SessionLocal()
    try:
        store = load_store(db)
        base = _base(store) or _workspace_currency(db) or "USD"
        currencies = list(store.get("currencies") or [])
        store["base"] = base
        try:
            fetched, source = fetch_rates(base, currencies)
        except Exception as e:  # noqa: BLE001 — FX down must not fail a report
            store["last_error"] = f"{type(e).__name__}: {e}"[:300]
            save_store(store, db)
            return {"status": "error", "base": base,
                    "source": store.get("source") or "",
                    "kept_last_good": bool(store.get("rates")),
                    "error": store["last_error"]}
        store["rates"] = fetched
        store["source"] = source
        store["fetched_at"] = datetime.now(timezone.utc).replace(
            microsecond=0).isoformat()
        store["last_error"] = None
        save_store(store, db)
        return {"status": "ok", "base": base, "source": source,
                "count": len(fetched), "fetched_at": store["fetched_at"]}
    finally:
        if own:
            db.close()


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def _refresh_tenant(tenant_id: int) -> None:
    result = await asyncio.to_thread(refresh)
    print(f"FX rates (tenant {tenant_id}): {result.get('status')} "
          f"source={result.get('source')} base={result.get('base')}")


async def rates_loop():
    """Daily refresh for every active workspace.

    Sweeps sequentially through ``for_each_tenant`` (which scopes each pass to
    its workspace) and sleeps only after a sweep returns, so it never overlaps
    itself. Network calls run in a thread — the event loop is not blocked.
    """
    global _loop_last_run
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            await for_each_tenant(_refresh_tenant)
            _loop_last_run = datetime.utcnow()
        except Exception as e:  # noqa: BLE001 — a sweep must survive one tenant
            print("FX rates loop error:", repr(e))
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)


async def rates_startup() -> None:
    """Register the refresh loop on app startup (idempotent)."""
    global _loop_started
    if _loop_started:
        return
    _loop_started = True
    asyncio.create_task(rates_loop())
