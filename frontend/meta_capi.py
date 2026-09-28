"""Meta Conversions API (CAPI) sender.

Pure payload builder + HTTP poster for the tracking plane. Deliberately free of
FastAPI/DB imports so it stays unit-testable; frontend/app.py owns config,
dedupe, logging and scheduling. A send failure must never surface to the
visitor's redirect or to the postback response — every error is swallowed and
reported through the return value.
"""
import hashlib
import os
import re
import time

import requests

DEFAULT_API_VERSION = "v21.0"
DEFAULT_GRAPH_BASE = "https://graph.facebook.com"
DEFAULT_TIMEOUT = 10
DEFAULT_MAX_ATTEMPTS = 3


def normalize_email(value) -> str:
    """Meta expects trimmed + lowercased email before hashing."""
    return str(value or "").strip().lower()


def normalize_phone(value) -> str:
    """Digits only, international format without '+' or a leading '00'.

    Country-code assumption: the postback must supply the full international
    number (e.g. +15551234567 or 0015551234567). A national-only number that
    still starts with '0' cannot be resolved to a country here, so the leading
    zero is kept and Meta simply fails to match (never a wrong match)."""
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hashed(value, normalizer):
    normalized = normalizer(value)
    return sha256_hex(normalized) if normalized else None


def build_user_data(conv: dict, include_customer_match: bool = True) -> dict:
    """user_data from a conversion dict — empty keys are dropped."""
    user_data = {}
    if conv.get("fbc"):
        user_data["fbc"] = conv["fbc"]
    if conv.get("fbp"):
        user_data["fbp"] = conv["fbp"]
    if conv.get("client_ip"):
        user_data["client_ip_address"] = conv["client_ip"]
    if conv.get("user_agent"):
        user_data["client_user_agent"] = conv["user_agent"]
    if include_customer_match:
        em = hashed(conv.get("email"), normalize_email)
        if em:
            user_data["em"] = [em]
        ph = hashed(conv.get("phone"), normalize_phone)
        if ph:
            user_data["ph"] = [ph]
    return user_data


def build_payload(cfg: dict, conv: dict) -> "dict | None":
    """Build the Graph API body. Returns None when the status is unmapped."""
    status = str(conv.get("status") or "")
    mapping = cfg.get("status_events") or {}
    event_name = mapping.get(status)
    if not event_name:
        return None

    try:
        value = float(conv.get("payout") or 0)
    except (TypeError, ValueError):
        value = 0.0

    event = {
        "event_name": str(event_name),
        "event_time": int(conv.get("event_time") or time.time()),
        "event_id": str(conv.get("click_id") or conv.get("event_id") or ""),
        "action_source": str(cfg.get("action_source") or "website"),
        "user_data": build_user_data(conv, bool(cfg.get("include_customer_match", True))),
        "custom_data": {
            "value": value,
            "currency": str(conv.get("currency") or cfg.get("default_currency") or "USD"),
        },
    }
    payload = {"data": [event]}
    if cfg.get("test_event_code"):
        payload["test_event_code"] = str(cfg["test_event_code"])
    return payload


def resolve_event_name(pixel: dict, status: str) -> "str | None":
    """Per-pixel event name: custom mapping for the status, else the default."""
    if pixel.get("custom_matching"):
        for row in pixel.get("conversion_matching") or []:
            if str(row.get("conversion_type") or "") == str(status):
                name = str(row.get("event_name") or "").strip()
                if name:
                    return name
    name = str(pixel.get("default_event_name") or "").strip()
    return name or None


def resolve_payout(pixel: dict, conv: dict) -> tuple:
    """(value, currency) override for this conversion type, else (None, None).

    A customisation with an empty value only overrides the currency; with an
    empty currency only the value. No matching rule -> both None (defaults)."""
    status = str(conv.get("status") or "")
    for row in pixel.get("payout_customisations") or []:
        if str(row.get("conversion_type") or "") != status:
            continue
        value = row.get("value")
        try:
            value = float(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            value = None
        currency = str(row.get("currency") or "").strip() or None
        return value, currency
    return None, None


def build_pixel_payload(pixel: dict, conv: dict, defaults: dict) -> "dict | None":
    """Graph API body for one pixel record. None when the pixel has no event name."""
    event_name = resolve_event_name(pixel, str(conv.get("status") or ""))
    if not event_name:
        return None

    try:
        value = float(conv.get("payout") or 0)
    except (TypeError, ValueError):
        value = 0.0
    currency = str(conv.get("currency") or defaults.get("default_currency") or "USD")
    override_value, override_currency = resolve_payout(pixel, conv)
    if override_value is not None:
        value = override_value
    if override_currency:
        currency = override_currency

    event = {
        "event_name": event_name,
        "event_time": int(conv.get("event_time") or time.time()),
        "event_id": str(conv.get("click_id") or conv.get("event_id") or ""),
        "action_source": str(pixel.get("action_source")
                             or defaults.get("action_source") or "website"),
        "user_data": build_user_data(conv, bool(defaults.get("include_customer_match", True))),
        "custom_data": {"value": value, "currency": currency},
    }
    event_url = str(pixel.get("event_url") or "").strip()
    if event_url:
        event["event_source_url"] = event_url
    payload = {"data": [event]}
    if defaults.get("test_event_code"):
        payload["test_event_code"] = str(defaults["test_event_code"])
    return payload


def pixel_endpoint(cfg: dict, pixel: dict) -> tuple:
    """(url, dataset_id, params) for one pixel, falling back to the global config."""
    dataset_id = str(pixel.get("pixel_id") or cfg.get("dataset_id") or "").strip()
    token = str(pixel.get("access_token") or cfg.get("access_token") or "")
    params = {"access_token": token}
    dq = str(pixel.get("data_quality_token") or "").strip()
    if dq:
        params["data_quality_token"] = dq
    return endpoint_url(cfg, dataset_id), dataset_id, params


def graph_base(cfg: dict) -> str:
    """Graph base URL. `graph_base_url` (settings) or META_CAPI_GRAPH_BASE (env)
    override the real host — test-only, so the suite can point at a local mock."""
    return (str(cfg.get("graph_base_url") or "").strip()
            or os.environ.get("META_CAPI_GRAPH_BASE", "").strip()
            or DEFAULT_GRAPH_BASE).rstrip("/")


def endpoint_url(cfg: dict, dataset_id: str) -> str:
    version = str(cfg.get("api_version") or "").strip() or DEFAULT_API_VERSION
    return f"{graph_base(cfg)}/{version}/{dataset_id}/events"


def post_event(cfg: dict, payload: dict, dataset_id: str) -> dict:
    """POST a built payload to Meta with bounded retries on transient failures.

    Retries network errors, 429 and 5xx up to `max_attempts` (default 3) with
    exponential backoff; a 4xx (bad token/payload) is terminal. Never raises —
    the outcome is returned so the caller can log it."""
    url = endpoint_url(cfg, dataset_id)
    params = dict(cfg.get("params") or {})
    if "access_token" not in params:
        params["access_token"] = str(cfg.get("access_token") or "")
    try:
        max_attempts = max(1, int(cfg.get("max_attempts") or DEFAULT_MAX_ATTEMPTS))
    except (TypeError, ValueError):
        max_attempts = DEFAULT_MAX_ATTEMPTS
    try:
        timeout = float(cfg.get("timeout_seconds") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    attempts = []
    for attempt in range(1, max_attempts + 1):
        status_code = None
        response_text = ""
        error = ""
        try:
            resp = requests.post(url, params=params, json=payload, timeout=timeout)
            status_code = resp.status_code
            response_text = resp.text[:1000]
            if 200 <= status_code < 300:
                attempts.append({"attempt": attempt, "status_code": status_code,
                                 "response": response_text, "transient": False})
                return {"ok": True, "attempts": attempts,
                        "status_code": status_code, "response": response_text}
            transient = status_code == 429 or status_code >= 500
        except requests.RequestException as e:
            transient = True
            error = str(e)[:300]
        attempts.append({"attempt": attempt, "status_code": status_code,
                         "response": response_text, "error": error,
                         "transient": transient})
        if not transient or attempt == max_attempts:
            break
        time.sleep(min(0.5 * (2 ** (attempt - 1)), 5.0))

    last = attempts[-1]
    return {"ok": False, "attempts": attempts,
            "status_code": last.get("status_code"),
            "response": last.get("response", ""), "error": last.get("error", "")}