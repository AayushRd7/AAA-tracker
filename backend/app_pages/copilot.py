"""Copilot — an AI read on the account's own aggregated metrics.

Two surfaces, both grounded in a compact snapshot assembled from the same
ClickHouse/report helpers the Reports and Acquisition pages already use (no new
metrics engine):

* ``POST /summary`` — "Summarise my account": totals + the top campaigns for a
  window, sent to a hosted chat model, which returns a short plain-language read.
* ``POST /ask``    — a one-off operator question with the same snapshot attached.

Stateless by design: no conversation is persisted, no new tables. History lives
in the page's own session state. Nothing on the visitor/click/postback path is
touched.

Provider
--------
An OpenAI-compatible chat-completions endpoint. Its base URL is fixed by the
deployment's environment (``OPENROUTER_BASE_URL``, falling back to a built-in
default) and is *not* a settings field: an admin must not be able to repoint the
provider — and with it the environment API key — at an arbitrary host. The API
key is read from the ``OPENROUTER_API_KEY`` environment variable at call time;
it may also be supplied through the ``copilot.api_key`` settings field for
deployments that cannot set environment variables, but that value is masked from
*every* reader (admin included) and is never returned, echoed or logged, so no
endpoint can leak it back. The environment key always wins.

Settings block (``settings.copilot``), all defaults shown below::

    enabled    bool   True                     # switch the whole surface off
    model      str    <see DEFAULTS>           # COPILOT_MODEL wins when set
    max_tokens int    400                      # hard cap, clamped to 60..800
    api_key    str    ""                       # optional override, always masked

Neither the upstream vendor nor the model is advertised in the product UI: the
endpoint is environment-only and the model is chosen for the deployment
(``COPILOT_MODEL`` when set), so the Settings card exposes only the on/off
switch and the token cap.

Token discipline: a compact system prompt, at most 8 campaign rows in the
snapshot, money rounded to two decimals and a hard ``max_tokens`` ceiling.

If no key is configured the endpoints degrade to a clear message, never a
traceback; provider timeouts/errors come back as readable text, never a 500.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from models.settings import SettingsORM
from tenant_context import current_tenant

router = APIRouter()

OPENROUTER_URL = "https://openrouter.ai/api/v1"
ENV_KEY_VAR = "OPENROUTER_API_KEY"
ENV_BASE_URL_VAR = "OPENROUTER_BASE_URL"
ENV_MODEL_VAR = "COPILOT_MODEL"

# The documented defaults. The model is chosen for the deployment (COPILOT_MODEL
# when set, else this) rather than exposed in the UI, so the upstream provider is
# not advertised to tenants. This is a fast, low-cost flash tier.
DEFAULTS = {
    "enabled": True,
    "model": "z-ai/glm-5.3-flash",
    "max_tokens": 400,
    "api_key": "",
}

MAX_SNAPSHOT_ROWS = 8
MAX_QUESTION_CHARS = 500
MIN_MAX_TOKENS = 60
MAX_MAX_TOKENS = 800
PROVIDER_TIMEOUT_SECONDS = 20.0
DEFAULT_WINDOW_DAYS = 7

SYSTEM_PROMPT = (
    "You are the account analyst for AAA Tracker, a click-tracking dashboard. "
    "You are given a compact JSON snapshot of the account's own aggregated "
    "metrics. Use ONLY the numbers in that snapshot: never invent, estimate or "
    "extrapolate figures, campaign names or dates, and never mention anything "
    "outside it. If the snapshot is empty or too thin for a conclusion, say so "
    "plainly instead of guessing. Write for a busy operator in plain language: "
    "what stands out, what moved, and what to look at next. Answer in at most "
    "three short bullets or two short paragraphs. No preamble and no restating "
    "of the question."
)


class WindowIn(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None


class AskIn(WindowIn):
    question: str = ""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _clamp_tokens(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = DEFAULTS["max_tokens"]
    return max(MIN_MAX_TOKENS, min(MAX_MAX_TOKENS, n))


def load_copilot_settings(db: Session) -> dict:
    """The copilot block merged over DEFAULTS (never raises). The key is taken
    from the environment first, then the settings override."""
    cfg = dict(DEFAULTS)
    try:
        row = db.query(SettingsORM).filter_by(name="settings").first()
        if row and row.value:
            block = (json.loads(row.value) or {}).get("copilot")
            if isinstance(block, dict):
                for k in DEFAULTS:
                    if block.get(k) not in (None, ""):
                        cfg[k] = block[k]
    except Exception:
        pass
    cfg["max_tokens"] = _clamp_tokens(cfg.get("max_tokens"))
    cfg["api_key"] = (os.environ.get(ENV_KEY_VAR) or "").strip() or (cfg.get("api_key") or "").strip()
    # The provider base URL is env-only: any value left in an older settings
    # document is deliberately ignored so an admin cannot repoint the provider.
    cfg["base_url"] = (os.environ.get(ENV_BASE_URL_VAR) or "").strip()
    # The model is not a tenant setting: the deployment picks it (COPILOT_MODEL),
    # then a stored value, then the built-in default.
    cfg["model"] = ((os.environ.get(ENV_MODEL_VAR) or "").strip()
                    or (cfg.get("model") or "").strip() or DEFAULTS["model"])
    return cfg


def _configured(cfg: dict) -> bool:
    return bool(cfg.get("enabled")) and bool(cfg.get("api_key"))


def _not_configured_message(cfg: dict) -> str:
    if not cfg.get("enabled"):
        return "Copilot is switched off for this workspace."
    return "Copilot is not configured for this deployment."


# ---------------------------------------------------------------------------
# Snapshot — assembled from the reporting helpers, never a new metrics engine
# ---------------------------------------------------------------------------

def _round(value, places=2):
    try:
        return round(float(value or 0), places)
    except (TypeError, ValueError):
        return 0.0


def _resolve_window(date_from, date_to):
    """(date_from, date_to) as ISO strings, defaulting to the last 7 days (UTC)."""
    today = datetime.now(timezone.utc).date()
    try:
        df = datetime.strptime(date_from, "%Y-%m-%d").date() if date_from else None
        dt = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Dates must be ISO (YYYY-MM-DD)")
    if df is None and dt is None:
        df, dt = today - timedelta(days=DEFAULT_WINDOW_DAYS - 1), today
    elif df is None:
        df = dt - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
    elif dt is None:
        dt = today
    if df > dt:
        df, dt = dt, df
    return df.isoformat(), dt.isoformat()


def _campaign_names(db: Session, ids) -> dict:
    ids = [int(i) for i in ids if i not in (None, "", "-1")]
    if not ids:
        return {}
    rows = db.execute(
        text("SELECT id, name FROM campaigns WHERE tenant_id = :tid AND id = ANY(:ids)"),
        {"tid": current_tenant(), "ids": ids}).fetchall()
    return {str(r[0]): r[1] for r in rows}


def build_snapshot(ch, db: Session, date_from: str, date_to: str) -> dict:
    """Compact aggregate snapshot: totals + up to MAX_SNAPSHOT_ROWS campaigns.

    Only aggregate metrics and campaign names leave this function — no visitor
    or click ids, no IPs, no emails, no sub-ids.
    """
    from clickHouse import get_metrics_series, get_report_breakdown_multi
    from schemas import Filters
    from app_pages.dashboard import _totals_from_series

    filters = {"date_from": date_from, "date_to": date_to, "campaigns": None}
    series = get_metrics_series(ch, Filters(**filters))
    totals = _totals_from_series(series)
    cost = _round(totals.get("cost"))
    revenue = _round(totals.get("revenue"))
    profit = _round(revenue - cost)

    out_totals = {
        "visits": int(totals.get("visits") or 0),
        "clicks": int(totals.get("clicks") or 0),
        "conversions": int(totals.get("conversions") or 0),
        "cost": cost,
        "revenue": revenue,
        "profit": profit,
        "roas": _round(revenue / cost) if cost > 0 else None,
    }

    rows = get_report_breakdown_multi(ch, filters, ["campaign_id"], limit=MAX_SNAPSHOT_ROWS)
    names = _campaign_names(db, [r.get("value") for r in rows])
    top = []
    for r in rows[:MAX_SNAPSHOT_ROWS]:
        c = _round(r.get("cost"))
        rev = _round(r.get("revenue"))
        cid = str(r.get("value"))
        top.append({
            "campaign": names.get(cid) or f"campaign {cid}",
            "visits": int(r.get("visits") or 0),
            "clicks": int(r.get("clicks") or 0),
            "conversions": int(r.get("conversions") or 0),
            "cost": c,
            "revenue": rev,
            "profit": _round(rev - c),
            "roas": _round(rev / c) if c > 0 else None,
        })

    days = (datetime.strptime(date_to, "%Y-%m-%d").date()
            - datetime.strptime(date_from, "%Y-%m-%d").date()).days + 1
    return {
        "window": {"date_from": date_from, "date_to": date_to, "days": days},
        "totals": out_totals,
        "top_campaigns": top,
    }


# ---------------------------------------------------------------------------
# Prompt + provider call
# ---------------------------------------------------------------------------

def build_prompt(snapshot: dict, question: Optional[str] = None):
    """(system, user) messages. Compact: the snapshot JSON plus, for /ask, the
    operator's question. The model is told to use only these numbers."""
    payload = json.dumps(snapshot, separators=(",", ":"), default=str)
    w = snapshot["window"]
    user = (f"Account snapshot for {w['date_from']} to {w['date_to']} "
            f"({w['days']} days):\n{payload}")
    if question:
        user += f"\n\nOperator question: {question.strip()}"
    return SYSTEM_PROMPT, user


def call_provider(system: str, user: str, cfg: dict):
    """Call the chat provider. Returns (text, error). Never raises, never logs
    or echoes the key."""
    base = (cfg.get("base_url") or OPENROUTER_URL).rstrip("/")
    url = base + "/chat/completions"
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": int(cfg["max_tokens"]),
        "temperature": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        "X-Title": "AAA Tracker",
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(PROVIDER_TIMEOUT_SECONDS)) as client:
            resp = client.post(url, json=body, headers=headers)
    except httpx.TimeoutException:
        return None, "Copilot timed out reaching the model provider — try again in a moment."
    except Exception:
        return None, "Copilot could not reach the model provider."
    if resp.status_code != 200:
        return None, f"Copilot model provider returned an error (HTTP {resp.status_code})."
    try:
        data = resp.json()
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return None, "Copilot model provider returned an unexpected response."
    if not text:
        return None, "Copilot model provider returned an empty answer."
    return text, None


def _run(db: Session, request: Request, date_from, date_to, question=None):
    cfg = load_copilot_settings(db)
    if not _configured(cfg):
        return {"configured": False, "message": _not_configured_message(cfg)}

    df, dt = _resolve_window(date_from, date_to)
    try:
        snapshot = build_snapshot(request.state.ch, db, df, dt)
    except HTTPException:
        raise
    except Exception as e:
        print("copilot snapshot error:", repr(e))
        return {"configured": True, "error": "Could not build the account snapshot right now."}

    system, user = build_prompt(snapshot, question)
    text, error = call_provider(system, user, cfg)

    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "copilot_ask" if question else "copilot_summary",
                "copilot", "",
                {"model": cfg["model"], "days": snapshot["window"]["days"],
                 "rows": len(snapshot["top_campaigns"]), "ok": error is None},
                request.client.host if request.client else "")

    out = {"configured": True, "model": cfg["model"],
           "window": snapshot["window"], "snapshot": snapshot}
    if error:
        out["error"] = error
    elif question:
        out["answer"] = text
    else:
        out["summary"] = text
    return out


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/status")
def copilot_status(db: Session = Depends(get_db)):
    cfg = load_copilot_settings(db)
    # The model is deliberately not reported: the deployment's upstream choice is
    # not advertised to tenants (the UI needs only readiness).
    return {"configured": _configured(cfg), "enabled": bool(cfg["enabled"])}


@router.post("/summary")
def copilot_summary(payload: WindowIn, request: Request, db: Session = Depends(get_db)):
    return _run(db, request, payload.date_from, payload.date_to)


@router.post("/ask")
def copilot_ask(payload: AskIn, request: Request, db: Session = Depends(get_db)):
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="A question is required")
    if len(question) > MAX_QUESTION_CHARS:
        question = question[:MAX_QUESTION_CHARS]
    return _run(db, request, payload.date_from, payload.date_to, question=question)
