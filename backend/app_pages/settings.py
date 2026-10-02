from fastapi import APIRouter, Depends, HTTPException
import hashlib
import json
import re
import secrets
import time
import uuid
import httpx
from sqlalchemy.orm import Session
from sqlalchemy import text
from db import get_db
from tenant_context import current_tenant
from models.settings import SettingsORM  # the settings model
from models.capi_pixels import (
    CapiPixelORM, CapiPixelBindingORM, CapiChannelSettingORM,
)
from email_reports import send_daily_report, send_scheduled_report, schedule_due
from env_config import (email_configured, resolve_email_config, telegram_bot_token,
                        telegram_bot_url)
from clickHouse import compile_formula, FORMULA_METRIC_KEYS

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

router = APIRouter()

@router.post("/clear-tracking-data")
async def clear_tracking_data(
    request: Request,
    db: Session = Depends(get_db)
):
    from audit_logger import audit_event
    from auth import get_caller

    try:
        ch = request.state.ch
        # Per tenant, not TRUNCATE: the admin of one workspace must not wipe
        # another workspace's tracking data.
        ch.command("ALTER TABLE clicks_data DELETE WHERE tenant_id = %(tid)s",
                   parameters={"tid": current_tenant()},
                   settings={"mutations_sync": 1})
    except Exception as e:
        print("clear-tracking-data: ClickHouse error:", repr(e))
        return JSONResponse(status_code=500, content={"error": "ClickHouse error clearing clicks_data"})

    try:
        db.execute(text("DELETE FROM conversions_data WHERE tenant_id = :tid"),
                   {"tid": current_tenant()})
        db.commit()
    except Exception as e:
        db.rollback()
        print("clear-tracking-data: PostgreSQL error:", repr(e))
        return JSONResponse(status_code=500, content={"error": "PostgreSQL error clearing conversions_data"})

    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "clear_tracking_data", "settings", "",
                {"clicks": "truncated", "conversions": "truncated"},
                request.client.host if request.client else "")
    return {"status": "ok", "message": "Tracking data cleared from ClickHouse and PostgreSQL"}



@router.post("/telegram-test")
def telegram_test(db: Session = Depends(get_db)):
    """Send a test message using the saved Telegram settings."""
    row = db.query(SettingsORM).filter_by(name="settings").first()
    cfg = {}
    if row and row.value:
        try:
            cfg = json.loads(row.value) or {}
        except Exception:
            cfg = {}
    tg = cfg.get("telegram") or {}
    # The bot belongs to the deployment (TELEGRAM_BOT_TOKEN); the chat is the
    # workspace's saved setting.
    token = telegram_bot_token(tg.get("bot_token") or "")
    chat_id = (tg.get("chat_id") or "").strip()

    if not token:
        raise HTTPException(status_code=400,
                            detail="Telegram is not configured for this deployment")
    if not chat_id:
        raise HTTPException(status_code=400,
                            detail="Add the Chat ID and save settings first — the "
                                   "tracker sends to the chat you choose")
    if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
        raise HTTPException(status_code=400, detail="The deployment's bot token looks wrong")

    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id,
                  "text": "✅ <b>Test message</b> from AAA Tracker\n\nTelegram notifications are set up correctly!",
                  "parse_mode": "HTML"},
            timeout=10)
        data = resp.json()
        if data.get("ok"):
            return {"status": "ok", "message": "Test message sent — check your Telegram"}
        detail = data.get("description", "Telegram API error")
        if "chat not found" in detail.lower():
            detail += (" — check the Chat ID: open the bot, press Start and send it a "
                       "message, then copy chat.id from api.telegram.org/bot<TOKEN>/getUpdates")
        raise HTTPException(status_code=400, detail=detail)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Could not reach Telegram: {e}")


@router.post("/email-test")
def email_test(request: Request, db: Session = Depends(get_db)):
    """Send yesterday's daily report right now using the saved email settings."""
    cfg = {}
    row = db.query(SettingsORM).filter_by(name="settings").first()
    if row and row.value:
        try:
            cfg = json.loads(row.value) or {}
        except Exception:
            cfg = {}
    email_cfg = cfg.get("email_reports") or {}
    if not (email_cfg.get("recipients") or "").strip():
        raise HTTPException(status_code=400, detail="Recipients is required — fill it in and save settings first")
    if not email_configured(email_cfg):
        raise HTTPException(status_code=400,
                            detail="Email delivery is not configured for this deployment")
    ok, detail = send_daily_report(request.state.ch, email_cfg)
    if not ok:
        raise HTTPException(status_code=500, detail=detail)
    return {"status": "ok", "message": detail}


# Placeholder returned in place of a real secret to callers who are not admin.
_SECRET_MASK = "\u2022" * 8


def _mask_secrets(value):
    """Recursively replace secret-looking string leaves with a fixed mask."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and SECRET_KEY_RE.search(k) and v not in (None, ""):
                out[k] = _SECRET_MASK
            else:
                out[k] = _mask_secrets(v)
        return out
    if isinstance(value, list):
        return [_mask_secrets(v) for v in value]
    return value


def _merge_section(existing, incoming):
    """Replace-merge one settings section (the pre-existing behavior: the
    incoming object wins), except a masked secret keeps its stored value so a
    non-admin's masked GET round-trip cannot overwrite the real secret."""
    out = dict(incoming)
    if isinstance(existing, dict):
        for k, v in incoming.items():
            if v == _SECRET_MASK and isinstance(existing.get(k), str) and existing.get(k):
                out[k] = existing[k]
    return out


def _merge_settings(existing, incoming):
    """Merge the incoming settings sections over the stored ones. A null deletes
    the key; a nested object replaces the stored one (an empty object clears it)."""
    if not isinstance(existing, dict) or not isinstance(incoming, dict):
        return incoming
    out = dict(existing)
    for k, v in incoming.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_section(out[k], v)
        else:
            out[k] = v
    return out


_VOLATILE_SETTINGS_KEYS = ("insights_last",)


def _settings_rev(raw) -> str:
    """Content hash of the operator-managed settings document.

    It is the token the Settings page echoes back: the client can only save the
    document it actually loaded, and a page that never loaded has none (its
    in-memory defaults must never overwrite the real configuration).

    Blocks written on a background schedule (``insights_last``) are excluded, so
    a loop refreshing them does not invalidate an open page's token. The hash is
    over sorted keys, so a re-serialisation that changes only key order (e.g.
    another page's read-modify-write) does not either.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            pass
    if isinstance(raw, dict):
        raw = {k: v for k, v in raw.items() if k not in _VOLATILE_SETTINGS_KEYS}
    if isinstance(raw, (dict, list)):
        raw = json.dumps(raw, sort_keys=True)
    return hashlib.sha256(str(raw or "").encode("utf-8")).hexdigest()[:16]


def _current_settings_rev(db: Session) -> str:
    row = db.query(SettingsORM).filter_by(name="settings").first()
    return _settings_rev(row.value if row else "")


# --- user-defined report columns (workspace.custom_columns) -----------------
# A name plus a formula over the metric whitelist, evaluated by the report
# builder's safe formula engine. Validated here so an unknown metric or a bad
# formula is a clear 400 at save time instead of a broken report later.
MAX_CUSTOM_COLUMNS = 10
MAX_CUSTOM_COLUMN_NAME_LEN = 40
MAX_CUSTOM_COLUMN_FORMULA_LEN = 200


def _validate_custom_columns(raw) -> list:
    """Normalise + validate ``workspace.custom_columns``.

    Returns the cleaned ``[{name, formula}]`` list. Raises HTTPException(400)
    on any invalid entry (empty/duplicate name, empty/oversized formula, an
    unknown metric or malformed arithmetic — compile_formula rejects both)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HTTPException(status_code=400,
                            detail="workspace.custom_columns must be a list")
    if len(raw) > MAX_CUSTOM_COLUMNS:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_CUSTOM_COLUMNS} custom columns are allowed")
    out, seen = [], set()
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail=f"Custom column #{i} must be an object with name and formula")
        name = str(item.get("name") or "").strip()
        formula = str(item.get("formula") or "").strip()
        if not name:
            raise HTTPException(status_code=400,
                                detail=f"Custom column #{i}: name is required")
        if len(name) > MAX_CUSTOM_COLUMN_NAME_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"Custom column '{name[:20]}…': name is too long "
                       f"(max {MAX_CUSTOM_COLUMN_NAME_LEN})")
        if name in seen:
            raise HTTPException(status_code=400,
                                detail=f"Custom column name '{name}' is duplicated")
        if not formula:
            raise HTTPException(status_code=400,
                                detail=f"Custom column '{name}': formula is required")
        if len(formula) > MAX_CUSTOM_COLUMN_FORMULA_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"Custom column '{name}': formula is too long "
                       f"(max {MAX_CUSTOM_COLUMN_FORMULA_LEN})")
        try:
            evaluate = compile_formula(formula)
        except ValueError as e:
            raise HTTPException(status_code=400,
                                detail=f"Custom column '{name}': {e}")
        # Compiling is not enough: 'revenue /' compiles and then fails on every
        # row, which would leave a permanently empty column. Probe it once against
        # a zero-valued row so the operator sees the mistake at save time.
        try:
            evaluate({m: 0 for m in FORMULA_METRIC_KEYS})
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Custom column '{name}': formula cannot be evaluated ({e})")
        seen.add(name)
        out.append({"name": name, "formula": formula})
    return out


# Sentinel distinguishing "the client sent no _base_rev" (a legacy/partial-block
# save from another page — allowed) from "_base_rev is null" (the client says it
# never loaded — refused).
_MISSING = object()

# Copilot keys that belong to the deployment, not the tenant: the provider
# endpoint and the upstream model are set from the environment and are never
# handed to a tenant-facing reader (the Settings page or a settings export), so
# the product does not advertise what it runs on. They stay writable through the
# API for deployments that cannot set environment variables.
_COPILOT_DEPLOYMENT_ONLY = ("base_url", "model")

# Settings keys owned by the deployment rather than by a workspace: the email
# relay credentials, the Telegram bot token, and the ad-platform Graph endpoint.
# They are read from the environment (see ``env_config``) and are never asked
# for in the product UI. A tenant-facing reader never sees them, and a save that
# omits or blanks one keeps whatever the deployment already stored — so a page
# that no longer renders the field cannot wipe it.
_DEPLOYMENT_ONLY = {
    "telegram": ("bot_token",),
    "email_reports": ("smtp_host", "smtp_port", "smtp_login", "smtp_password",
                      "api_key", "from_name", "from_email"),
    "meta_ads": ("access_token", "api_version", "graph_base_url"),
    # The Conversions API's Graph endpoint is test-only plumbing, not a tenant
    # choice; the fallback dataset/token above stay the tenant's own.
    "meta_capi": ("api_version", "graph_base_url"),
}


def _strip_copilot_deployment_keys(doc):
    """Drop the deployment-only copilot keys from a settings document (in place)."""
    if isinstance(doc, dict) and isinstance(doc.get("copilot"), dict):
        for key in _COPILOT_DEPLOYMENT_ONLY:
            doc["copilot"].pop(key, None)
    return doc


def _strip_deployment_keys(doc):
    """Drop every deployment-owned key from a settings document (in place)."""
    _strip_copilot_deployment_keys(doc)
    if isinstance(doc, dict):
        for section, keys in _DEPLOYMENT_ONLY.items():
            block = doc.get(section)
            if isinstance(block, dict):
                for key in keys:
                    block.pop(key, None)
    return doc


def _keep_deployment_keys(stored: dict, merged: dict) -> dict:
    """Preserve deployment-owned keys a tenant-facing save left absent or blank.

    The Settings page no longer renders these fields, so it submits no value (or
    an empty string) for them. Without this the section merge would drop or
    blank them, wiping the fallback a pre-environment installation needs."""
    if not isinstance(stored, dict) or not isinstance(merged, dict):
        return merged
    for section, keys in _DEPLOYMENT_ONLY.items():
        live = stored.get(section)
        incoming = merged.get(section)
        if not isinstance(live, dict) or not isinstance(incoming, dict):
            continue
        for key in keys:
            if key in live and not str(incoming.get(key) or "").strip():
                incoming[key] = live[key]
    return merged


def deployment_status(doc: dict) -> dict:
    """What the deployment has configured, for the Settings page's status lines.

    Reports readiness, plus the From address recipients will see. It never
    reports a credential or an endpoint. ``doc`` is the stored (unstripped)
    settings document so a pre-environment installation's saved credentials
    still count as configured."""
    doc = doc if isinstance(doc, dict) else {}
    email_block = doc.get("email_reports")
    tg_block = doc.get("telegram")
    email_block = email_block if isinstance(email_block, dict) else {}
    tg_block = tg_block if isinstance(tg_block, dict) else {}
    return {
        "email_configured": email_configured(email_block),
        # The From address is not a secret — recipients see it — so the card can
        # name it instead of offering a field the deployment would ignore.
        "email_from": (resolve_email_config(email_block).get("from_email") or ""),
        "telegram_bot_configured": bool(telegram_bot_token(tg_block.get("bot_token") or "")),
        "telegram_bot_url": telegram_bot_url(),
    }


@router.get("/")
def get_settings(request: Request, db: Session = Depends(get_db)):
    from auth import get_caller
    _, is_admin = get_caller(request)
    rows = db.query(SettingsORM).all()
    out = {}
    raw_settings = ""
    stored_doc = {}
    for row in rows:
        try:
            value = json.loads(row.value)
        except Exception:
            value = row.value
        if row.name == "settings":
            raw_settings = row.value or ""
            stored_doc = value if isinstance(value, dict) else {}
        # Secret-looking values (tokens, keys, passwords) are masked for
        # non-admins; an admin still sees the real value to configure it.
        out[row.name] = value if is_admin else _mask_secrets(value)
    # The Copilot provider key is stricter: it is masked for *every* reader so
    # no endpoint can ever hand it back (the environment is its normal source).
    doc = out.get("settings")
    if isinstance(doc, dict) and isinstance(doc.get("copilot"), dict) \
            and doc["copilot"].get("api_key"):
        doc["copilot"]["api_key"] = _SECRET_MASK
    # Deployment-owned keys are dropped for every reader: the UI never shows
    # them, so nothing should be able to read them back either.
    _strip_deployment_keys(doc)
    # Readiness is computed from the stored document, before the strip.
    out["deployment"] = deployment_status(stored_doc)
    # Optimistic-concurrency token for the Settings page, hashed from the raw
    # stored value (never the masked view). Additive, so nothing that reads the
    # response today is affected.
    out["settings_rev"] = _settings_rev(raw_settings)
    return out

@router.post("/")
def save_settings(payload: dict, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)
    ip = request.client.host if request.client else ""
    # `_base_rev` is a control field, never a settings row. Present = the caller
    # opted into the rev protocol (the Settings page). Null = it never loaded;
    # mismatched = another writer changed the document since it loaded. Either
    # way the write is refused before anything is touched.
    base_rev = payload.pop("_base_rev", _MISSING)
    # Copilot's provider base URL is env-only (OPENROUTER_BASE_URL): drop it
    # from any incoming document so an admin cannot repoint the provider — and
    # the environment API key with it — at an arbitrary host.
    _copilot_block = (payload.get("settings") or {}).get("copilot") \
        if isinstance(payload.get("settings"), dict) else None
    if isinstance(_copilot_block, dict):
        _copilot_block.pop("base_url", None)
    if base_rev is not _MISSING:
        current_rev = _current_settings_rev(db)
        if not base_rev:
            audit_event(caller or "api_token", "save_rejected", "settings",
                        "settings", {"reason": "not_loaded"}, ip)
            raise HTTPException(
                status_code=409,
                detail="Settings were not loaded on this page — refusing to "
                       "save, so the stored configuration cannot be overwritten "
                       "by defaults. Reload the page and try again.")
        if str(base_rev) != current_rev:
            audit_event(caller or "api_token", "save_rejected", "settings",
                        "settings", {"reason": "stale_rev"}, ip)
            raise HTTPException(
                status_code=409,
                detail="Settings changed since this page loaded — reload the "
                       "page before saving.")
    # Validate the user-defined report columns before they are written: an
    # unknown metric or malformed formula must be a clear 400, never stored to
    # break every later report. Only touched when the caller sends the key, so
    # a partial workspace save from another page keeps its meaning.
    _workspace_block = payload.get("settings", {}).get("workspace") \
        if isinstance(payload.get("settings"), dict) else None
    if isinstance(_workspace_block, dict):
        if "custom_columns" in _workspace_block:
            _workspace_block["custom_columns"] = _validate_custom_columns(
                _workspace_block["custom_columns"])
        if "grouping_view" in _workspace_block:
            # JSON truthiness would turn the string "false" into True; only a
            # real boolean True enables the grouped view.
            _workspace_block["grouping_view"] = _workspace_block["grouping_view"] is True
    # Merge top-level keys server-side under a row lock: two rapid saves of
    # different keys must not clobber each other (read-modify-write race).
    for key, val in payload.items():
        val_str = json.dumps(val)
        row = db.execute(
            text("SELECT id, value FROM settings "
                 "WHERE name = :n AND tenant_id = :tid FOR UPDATE"),
            {"n": key, "tid": current_tenant()}).fetchone()
        if row:
            try:
                existing = json.loads(row[1]) if row[1] else {}
            except Exception:
                existing = None
            if isinstance(existing, dict) and isinstance(val, dict):
                existing = _keep_deployment_keys(existing, _merge_settings(existing, val))
                val_str = json.dumps(existing)
            db.execute(text("UPDATE settings SET value = :v WHERE id = :i"),
                       {"v": val_str, "i": row[0]})
        else:
            db.add(SettingsORM(name=key, value=val_str))
    db.commit()
    audit_event(caller or "api_token", "save", "settings", ", ".join(payload.keys())[:64],
                {"keys": list(payload.keys())}, ip)
    return {"status": "ok", "settings_rev": _current_settings_rev(db)}


# --- Saved report builder configs (G47), stored under the 'saved_reports' key ---

def _load_saved_reports(db: Session) -> list:
    row = db.query(SettingsORM).filter_by(name="saved_reports").first()
    if not row or not row.value:
        return []
    try:
        data = json.loads(row.value)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _store_saved_reports(db: Session, reports: list) -> None:
    val_str = json.dumps(reports)
    row = db.query(SettingsORM).filter_by(name="saved_reports").first()
    if row:
        row.value = val_str
    else:
        db.add(SettingsORM(name="saved_reports", value=val_str))
    db.commit()


@router.get("/saved-reports")
def list_saved_reports(db: Session = Depends(get_db)):
    return {"reports": _load_saved_reports(db)}


@router.post("/saved-reports")
def create_saved_report(payload: dict, db: Session = Depends(get_db)):
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Report name is required")
    config = payload.get("config") or {}
    reports = _load_saved_reports(db)
    report = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "config": config,
        "created_at": int(time.time()),
    }
    reports.append(report)
    _store_saved_reports(db, reports)
    return {"report": report}


@router.delete("/saved-reports/{report_id}")
def delete_saved_report(report_id: str, db: Session = Depends(get_db)):
    reports = _load_saved_reports(db)
    remaining = [r for r in reports if r.get("id") != report_id]
    if len(remaining) == len(reports):
        raise HTTPException(status_code=404, detail="Saved report not found")
    _store_saved_reports(db, remaining)
    return {"status": "ok"}


# --- G52: sharing — a saved report gets an unguessable public token ---

@router.post("/saved-reports/{report_id}/share")
def share_saved_report(report_id: str, db: Session = Depends(get_db)):
    reports = _load_saved_reports(db)
    report = next((r for r in reports if r.get("id") == report_id), None)
    if not report:
        raise HTTPException(status_code=404, detail="Saved report not found")
    # Reuse an existing live share so the URL stays stable across toggles.
    share = report.get("share")
    if not share or not share.get("token"):
        share = {"token": secrets.token_urlsafe(32), "created_at": int(time.time())}
        report["share"] = share
        _store_saved_reports(db, reports)
    return {"share": share}


@router.delete("/saved-reports/{report_id}/share")
def revoke_saved_report_share(report_id: str, db: Session = Depends(get_db)):
    reports = _load_saved_reports(db)
    report = next((r for r in reports if r.get("id") == report_id), None)
    if not report:
        raise HTTPException(status_code=404, detail="Saved report not found")
    if not report.get("share"):
        raise HTTPException(status_code=404, detail="Report is not shared")
    report.pop("share", None)
    _store_saved_reports(db, reports)
    return {"status": "ok"}


# --- Wave 19A: saved column templates (stored under 'column_templates') ---
# Shaped {scope: [{name, columns: [...]}]}; one settings row, no new table.
# The Logs tabs and the report builder use these to switch a table's columns.

def _load_column_templates(db: Session) -> dict:
    row = db.query(SettingsORM).filter_by(name="column_templates").first()
    if not row or not row.value:
        return {}
    try:
        data = json.loads(row.value)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for scope, items in data.items():
        if not isinstance(scope, str) or not isinstance(items, list):
            continue
        clean = []
        for t in items:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            cols = t.get("columns")
            clean.append({"name": str(t["name"])[:100],
                          "columns": [str(c)[:80] for c in cols] if isinstance(cols, list) else []})
        out[scope] = clean
    return out


def _store_column_templates(db: Session, data: dict) -> None:
    val_str = json.dumps(data)
    row = db.query(SettingsORM).filter_by(name="column_templates").first()
    if row:
        row.value = val_str
    else:
        db.add(SettingsORM(name="column_templates", value=val_str))
    db.commit()


@router.get("/column-templates")
def list_column_templates(scope: str = None, db: Session = Depends(get_db)):
    data = _load_column_templates(db)
    if scope:
        return {"scope": scope, "templates": data.get(scope, [])}
    return {"templates": data}


@router.put("/column-templates")
def save_column_template(payload: dict, db: Session = Depends(get_db)):
    scope = str(payload.get("scope") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not scope:
        raise HTTPException(status_code=400, detail="scope is required")
    if not name:
        raise HTTPException(status_code=400, detail="Template name is required")
    raw_cols = payload.get("columns")
    if not isinstance(raw_cols, list) or not raw_cols:
        raise HTTPException(status_code=400, detail="columns must be a non-empty list")
    columns = [str(c) for c in raw_cols if str(c).strip()]
    data = _load_column_templates(db)
    items = data.get(scope, [])
    template = {"name": name, "columns": columns}
    for i, t in enumerate(items):
        if t.get("name") == name:
            items[i] = template
            break
    else:
        items.append(template)
    data[scope] = items
    _store_column_templates(db, data)
    return {"template": template, "templates": items}


@router.delete("/column-templates")
def delete_column_template(scope: str, name: str, db: Session = Depends(get_db)):
    data = _load_column_templates(db)
    items = data.get(scope, [])
    remaining = [t for t in items if t.get("name") != name]
    if len(remaining) == len(items):
        raise HTTPException(status_code=404, detail="Column template not found")
    if remaining:
        data[scope] = remaining
    else:
        data.pop(scope, None)
    _store_column_templates(db, data)
    return {"status": "ok"}


# --- G57: chart annotations (admin-wide, stored under 'annotations') ---

ANNOTATION_COLORS = ("success", "warning", "danger", "info")


def _load_annotations(db: Session) -> list:
    row = db.query(SettingsORM).filter_by(name="annotations").first()
    if not row or not row.value:
        return []
    try:
        data = json.loads(row.value)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _store_annotations(db: Session, items: list) -> None:
    val_str = json.dumps(items)
    row = db.query(SettingsORM).filter_by(name="annotations").first()
    if row:
        row.value = val_str
    else:
        db.add(SettingsORM(name="annotations", value=val_str))
    db.commit()


@router.get("/annotations")
def list_annotations(db: Session = Depends(get_db)):
    items = sorted(_load_annotations(db), key=lambda a: a.get("date") or "")
    return {"annotations": items}


@router.post("/annotations")
def create_annotation(payload: dict, db: Session = Depends(get_db)):
    date = str(payload.get("date") or "").strip()
    text = str(payload.get("text") or "").strip()
    color = str(payload.get("color") or "info").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        raise HTTPException(status_code=400, detail="Annotation date must be YYYY-MM-DD")
    if not text:
        raise HTTPException(status_code=400, detail="Annotation text is required")
    if color not in ANNOTATION_COLORS:
        raise HTTPException(status_code=400, detail=f"color must be one of: {', '.join(ANNOTATION_COLORS)}")
    items = _load_annotations(db)
    item = {"id": uuid.uuid4().hex[:12], "date": date, "text": text[:500], "color": color}
    items.append(item)
    _store_annotations(db, items)
    return {"annotation": item}


@router.delete("/annotations/{annotation_id}")
def delete_annotation(annotation_id: str, db: Session = Depends(get_db)):
    items = _load_annotations(db)
    remaining = [a for a in items if a.get("id") != annotation_id]
    if len(remaining) == len(items):
        raise HTTPException(status_code=404, detail="Annotation not found")
    _store_annotations(db, remaining)
    return {"status": "ok"}


# --- G53: per-report email schedules (stored under 'report_email_schedules') ---

def _load_report_schedules(db: Session) -> list:
    row = db.query(SettingsORM).filter_by(name="report_email_schedules").first()
    if not row or not row.value:
        return []
    try:
        data = json.loads(row.value)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _store_report_schedules(db: Session, items: list) -> None:
    val_str = json.dumps(items)
    row = db.query(SettingsORM).filter_by(name="report_email_schedules").first()
    if row:
        row.value = val_str
    else:
        db.add(SettingsORM(name="report_email_schedules", value=val_str))
    db.commit()


def _schedule_public(s: dict) -> dict:
    """Schedule view for the UI: due flag from the same logic the email loop uses."""
    from datetime import datetime as dt
    out = dict(s)
    out["due"] = schedule_due(s, dt.utcnow())
    return out


@router.get("/report-email-schedules")
def list_report_schedules(db: Session = Depends(get_db)):
    return {"schedules": [_schedule_public(s) for s in _load_report_schedules(db)]}


@router.post("/report-email-schedules")
def save_report_schedule(payload: dict, request: Request, db: Session = Depends(get_db)):
    report_id = str(payload.get("report_id") or "").strip()
    reports = _load_saved_reports(db)
    report = next((r for r in reports if r.get("id") == report_id), None)
    if not report:
        raise HTTPException(status_code=404, detail="Saved report not found")
    recipients = str(payload.get("recipients") or "").strip()
    if not recipients:
        raise HTTPException(status_code=400, detail="Recipients are required")
    try:
        hour_utc = int(payload.get("hour_utc"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="hour_utc must be an integer 0-23")
    if not 0 <= hour_utc <= 23:
        raise HTTPException(status_code=400, detail="hour_utc must be 0-23")
    frequency = str(payload.get("frequency") or "daily").strip()
    if frequency not in ("daily", "weekly"):
        raise HTTPException(status_code=400, detail="frequency must be daily or weekly")

    schedules = _load_report_schedules(db)
    existing = next((s for s in schedules if s.get("report_id") == report_id), None)
    if existing:
        existing.update({"recipients": recipients, "hour_utc": hour_utc, "frequency": frequency})
        schedule = existing
    else:
        schedule = {"report_id": report_id, "recipients": recipients,
                    "hour_utc": hour_utc, "frequency": frequency, "last_sent": None}
        schedules.append(schedule)
    _store_report_schedules(db, schedules)
    return {"schedule": _schedule_public(schedule)}


@router.delete("/report-email-schedules/{report_id}")
def delete_report_schedule(report_id: str, db: Session = Depends(get_db)):
    schedules = _load_report_schedules(db)
    remaining = [s for s in schedules if s.get("report_id") != report_id]
    if len(remaining) == len(schedules):
        raise HTTPException(status_code=404, detail="Schedule not found")
    _store_report_schedules(db, remaining)
    return {"status": "ok"}


@router.post("/report-email-schedules/{report_id}/test")
def test_report_schedule(report_id: str, request: Request, db: Session = Depends(get_db)):
    """Send the scheduled report right now (G53 test button)."""
    row = db.query(SettingsORM).filter_by(name="settings").first()
    cfg = {}
    if row and row.value:
        try:
            cfg = json.loads(row.value) or {}
        except Exception:
            cfg = {}
    email_cfg = cfg.get("email_reports") or {}
    schedule = next((s for s in _load_report_schedules(db) if s.get("report_id") == report_id), None)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    ok, detail = send_scheduled_report(request.state.ch, email_cfg, schedule)
    if not ok:
        raise HTTPException(status_code=500, detail=detail)
    return {"status": "ok", "message": detail}


# --- Settings backup: export (secrets redacted) / import (secrets restored) ---

# Key names that look like credentials — their values are nulled on export so
# the downloaded document is safe to share/store, while the key itself stays
# so the document round-trips through import.
SECRET_KEY_RE = re.compile(r"(token|api[_-]?key|secret|password|postback[_-]?key)",
                           re.IGNORECASE)


def _sanitize_for_export(value):
    """Recursively replace secret-looking values with null, keeping the key."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and SECRET_KEY_RE.search(k):
                out[k] = None
            else:
                out[k] = _sanitize_for_export(v)
        return out
    if isinstance(value, list):
        return [_sanitize_for_export(v) for v in value]
    return value


def _restore_secrets(imported, current):
    """Deep-merge an imported section over the live one: a null leaf in the
    import means "redacted on export" — keep the live value so restoring a
    backup never wipes credentials.

    List sections (saved_reports, report_email_schedules, annotations, …) are
    matched item-by-item on a stable key (id / report_id / name) so nulled
    secrets inside a list item (e.g. a saved report's share token) restore
    from the matching live item; items with no live counterpart get their
    null secret keys stripped instead of storing null."""
    if isinstance(imported, dict) and isinstance(current, dict):
        return {k: _restore_secrets(v, current.get(k)) for k, v in imported.items()}
    if isinstance(imported, list) and isinstance(current, list):
        live_by_key = {}
        for live in current:
            k, v = _list_item_key(live)
            if k is not None:
                live_by_key[v] = live
        out = []
        for item in imported:
            k, v = _list_item_key(item)
            live = live_by_key.get(v) if k is not None else None
            if isinstance(item, dict) and isinstance(live, dict):
                out.append(_restore_secrets(item, live))
            else:
                out.append(_strip_secret_nulls(item))
        return out
    if imported is None:
        return current
    return imported


_LIST_ITEM_KEYS = ("id", "report_id", "name")


def _list_item_key(item):
    """(key_name, value) for the first stable identity key the item carries."""
    if isinstance(item, dict):
        for k in _LIST_ITEM_KEYS:
            if k in item:
                return k, item[k]
    return None, None


def _strip_secret_nulls(value):
    """Remove redacted (null) secret-looking keys so they are skipped rather
    than stored as null when there is no live value to restore from."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if v is None and isinstance(k, str) and SECRET_KEY_RE.search(k):
                continue
            out[k] = _strip_secret_nulls(v)
        return out
    if isinstance(value, list):
        return [_strip_secret_nulls(v) for v in value]
    return value


@router.get("/export")
def export_settings(db: Session = Depends(get_db)):
    """Download the full settings document as JSON. Secret-looking values
    (API tokens, postback keys, Telegram tokens, passwords) are replaced
    with null — the key remains so the file can be re-imported."""
    rows = db.query(SettingsORM).all()
    data = {}
    for row in rows:
        try:
            data[row.name] = json.loads(row.value)
        except Exception:
            data[row.name] = row.value
    # Ad-platform OAuth tokens live in their own table (not settings), but they
    # are exported too so a backup shows the connections — the token itself is
    # nulled by _sanitize_for_export (access_token matches SECRET_KEY_RE).
    try:
        conn_rows = db.execute(text(
            "SELECT platform, access_token, token_type, expires_at, scopes, "
            "account_label FROM integration_connections "
            "WHERE tenant_id = :tid ORDER BY platform"),
            {"tid": current_tenant()}).fetchall()
        data["integration_connections"] = [
            {"platform": r[0], "access_token": r[1], "token_type": r[2],
             "expires_at": r[3].isoformat() if r[3] else None,
             "scopes": r[4], "account_label": r[5]} for r in conn_rows]
    except Exception:
        pass
    doc = {"exported_at": int(time.time()), "data": _sanitize_for_export(data)}
    # The export is a tenant-facing document: no upstream model or endpoint, and
    # no deployment-owned relay / bot / Graph credentials.
    _strip_deployment_keys((doc["data"] or {}).get("settings"))
    return JSONResponse(
        content=doc,
        headers={"Content-Disposition": 'attachment; filename="settings-backup.json"'})


@router.post("/import")
async def import_settings(request: Request, db: Session = Depends(get_db)):
    """Restore a document produced by /export. Unknown shapes are rejected;
    redacted (null) secret leaves keep the live values."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400,
                            detail="Request body must be a JSON settings document")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400,
                            detail="Invalid settings document: expected a JSON object")
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if not isinstance(data, dict) or not data:
        raise HTTPException(status_code=400,
                            detail="Invalid settings document: expected a non-empty "
                                   "JSON object of settings sections")
    for name, value in data.items():
        if not isinstance(name, str) \
                or not isinstance(value, (dict, list)) or isinstance(value, bool):
            raise HTTPException(status_code=400,
                                detail=f"Invalid settings document: section '{name}' "
                                       "must be an object or a list")
        row = db.query(SettingsORM).filter_by(name=name).first()
        current = None
        if row and row.value:
            try:
                current = json.loads(row.value)
            except Exception:
                current = None
        merged = _restore_secrets(value, current)
        if name == "settings":
            # The export strips deployment-owned keys, so a restore must not read
            # their absence as "delete" — keep whatever the deployment stored.
            merged = _keep_deployment_keys(
                current if isinstance(current, dict) else {}, merged)
        val_str = json.dumps(merged)
        if row:
            row.value = val_str
        else:
            db.add(SettingsORM(name=name, value=val_str))
    db.commit()
    return {"status": "ok", "imported": len(data)}
# --- CAPI integrations: pixels as first-class records + channel/offer bindings ---

CAPI_PLATFORMS = ("meta", "snapchat", "tiktok", "google", "pinterest",
                  "applovin", "openai")
CAPI_SCOPES = ("channel", "offer")


def _capi_pixel_view(row, is_admin: bool) -> dict:
    """Pixel record for the API/UI. Secret fields are masked for non-admins."""
    d = {
        "id": row.id,
        "title": row.title,
        "platform": row.platform,
        "pixel_id": row.pixel_id,
        "default_event_name": row.default_event_name or "",
        "event_url": row.event_url or "",
        "action_source": row.action_source or "website",
        "custom_matching": bool(row.custom_matching),
        "conversion_matching": row.conversion_matching or [],
        "payout_customisations": row.payout_customisations or [],
        "status": row.status or "active",
        "access_token": row.access_token or "",
        "data_quality_token": row.data_quality_token or "",
    }
    return d if is_admin else _mask_secrets(d)


def _clean_conversion_matching(items) -> list:
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        ct = str(item.get("conversion_type") or "").strip()
        ev = str(item.get("event_name") or "").strip()
        if ct:
            out.append({"conversion_type": ct[:100], "event_name": ev[:100]})
    return out


def _clean_payout_customisations(items) -> list:
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        ct = str(item.get("conversion_type") or "").strip()
        if not ct:
            continue
        value = item.get("value")
        if value in ("", None):
            value = None
        else:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
        out.append({"conversion_type": ct[:100], "value": value,
                    "currency": str(item.get("currency") or "").strip()[:10]})
    return out


def _apply_capi_pixel(payload: dict, pixel: CapiPixelORM) -> None:
    """Validate + apply an incoming pixel payload over an ORM row in place."""
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    platform = str(payload.get("platform") or "meta").strip().lower()
    if platform not in CAPI_PLATFORMS:
        raise HTTPException(status_code=400,
                            detail=f"platform must be one of: {', '.join(CAPI_PLATFORMS)}")
    pixel_id = str(payload.get("pixel_id") or "").strip()
    if not pixel_id:
        raise HTTPException(status_code=400, detail="Pixel / Dataset ID is required")
    status = str(payload.get("status") or "active").strip().lower()
    if status not in ("active", "inactive"):
        raise HTTPException(status_code=400, detail="status must be active or inactive")

    pixel.title = title[:255]
    pixel.platform = platform
    pixel.pixel_id = pixel_id[:255]
    pixel.default_event_name = str(payload.get("default_event_name") or "").strip()[:100]
    pixel.event_url = str(payload.get("event_url") or "").strip() or None
    pixel.action_source = str(payload.get("action_source") or "website").strip()[:64]
    pixel.custom_matching = bool(payload.get("custom_matching", False))
    pixel.conversion_matching = _clean_conversion_matching(payload.get("conversion_matching"))
    pixel.payout_customisations = _clean_payout_customisations(
        payload.get("payout_customisations"))
    pixel.status = status
    # A masked secret means "unchanged" (a non-admin's own GET round-trip).
    for field, key in (("access_token", "access_token"),
                       ("data_quality_token", "data_quality_token")):
        if key in payload:
            v = payload.get(key)
            if v is None or v == _SECRET_MASK:
                continue
            setattr(pixel, field, str(v))


@router.get("/capi-pixels")
def list_capi_pixels(request: Request, db: Session = Depends(get_db)):
    from auth import get_caller
    _, is_admin = get_caller(request)
    rows = db.query(CapiPixelORM).order_by(CapiPixelORM.id.asc()).all()
    return {"pixels": [_capi_pixel_view(r, is_admin) for r in rows]}


@router.post("/capi-pixels")
def create_capi_pixel(payload: dict, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    pixel = CapiPixelORM()
    _apply_capi_pixel(payload, pixel)
    db.add(pixel)
    db.commit()
    db.refresh(pixel)
    caller, is_admin = get_caller(request)
    audit_event(caller or "api_token", "create", "capi_pixels", str(pixel.id),
                {"title": pixel.title, "platform": pixel.platform},
                request.client.host if request.client else "")
    return {"pixel": _capi_pixel_view(pixel, is_admin)}


@router.put("/capi-pixels/{pixel_id}")
def update_capi_pixel(pixel_id: int, payload: dict, request: Request,
                      db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    pixel = db.query(CapiPixelORM).filter_by(id=pixel_id).first()
    if not pixel:
        raise HTTPException(status_code=404, detail="Pixel not found")
    _apply_capi_pixel(payload, pixel)
    db.commit()
    db.refresh(pixel)
    caller, is_admin = get_caller(request)
    audit_event(caller or "api_token", "update", "capi_pixels", str(pixel_id),
                {"title": pixel.title}, request.client.host if request.client else "")
    return {"pixel": _capi_pixel_view(pixel, is_admin)}


@router.delete("/capi-pixels/{pixel_id}")
def delete_capi_pixel(pixel_id: int, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    pixel = db.query(CapiPixelORM).filter_by(id=pixel_id).first()
    if not pixel:
        raise HTTPException(status_code=404, detail="Pixel not found")
    db.delete(pixel)
    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "delete", "capi_pixels", str(pixel_id),
                {"title": pixel.title}, request.client.host if request.client else "")
    return {"status": "ok"}


@router.get("/capi-bindings")
def get_capi_bindings(scope: str, scope_id: int, db: Session = Depends(get_db)):
    scope = str(scope or "").strip().lower()
    if scope not in CAPI_SCOPES:
        raise HTTPException(status_code=400,
                            detail=f"scope must be one of: {', '.join(CAPI_SCOPES)}")
    rows = db.query(CapiPixelBindingORM).filter_by(scope=scope, scope_id=scope_id).all()
    out = {"scope": scope, "scope_id": scope_id,
           "pixel_ids": sorted(r.pixel_id for r in rows),
           "active": True, "impression_cost_sync": False}
    if scope == "channel":
        cs = db.query(CapiChannelSettingORM).filter_by(source_id=scope_id).first()
        if cs:
            out["active"] = bool(cs.active)
            out["impression_cost_sync"] = bool(cs.impression_cost_sync)
    return out


@router.put("/capi-bindings")
def set_capi_bindings(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Replace the pixel selection for one channel/offer scope.

    Also carries the channel block's Active + Impression cost sync toggles."""
    from audit_logger import audit_event
    from auth import get_caller
    scope = str(payload.get("scope") or "").strip().lower()
    if scope not in CAPI_SCOPES:
        raise HTTPException(status_code=400,
                            detail=f"scope must be one of: {', '.join(CAPI_SCOPES)}")
    try:
        scope_id = int(payload.get("scope_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="scope_id must be an integer")

    pixel_ids = payload.get("pixel_ids") or []
    valid = set()
    if pixel_ids:
        found = db.query(CapiPixelORM.id).filter(CapiPixelORM.id.in_(pixel_ids)).all()
        valid = {r[0] for r in found}

    db.query(CapiPixelBindingORM).filter_by(scope=scope, scope_id=scope_id).delete()
    for pid in valid:
        db.add(CapiPixelBindingORM(pixel_id=pid, scope=scope, scope_id=scope_id))

    active = True
    impression_cost_sync = False
    if scope == "channel":
        cs = db.query(CapiChannelSettingORM).filter_by(source_id=scope_id).first()
        if not cs:
            cs = CapiChannelSettingORM(source_id=scope_id, active=True,
                                       impression_cost_sync=False)
            db.add(cs)
        if "active" in payload:
            cs.active = bool(payload.get("active"))
        if "impression_cost_sync" in payload:
            cs.impression_cost_sync = bool(payload.get("impression_cost_sync"))
        active = bool(cs.active)
        impression_cost_sync = bool(cs.impression_cost_sync)
    db.commit()

    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "bind", "capi_pixels",
                f"{scope}:{scope_id}", {"pixels": sorted(valid)},
                request.client.host if request.client else "")
    return {"status": "ok", "scope": scope, "scope_id": scope_id,
            "pixel_ids": sorted(valid), "active": active,
            "impression_cost_sync": impression_cost_sync}