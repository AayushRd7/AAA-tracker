from fastapi import APIRouter, Depends, HTTPException
import json
import re
import secrets
import time
import uuid
import httpx
from sqlalchemy.orm import Session
from sqlalchemy import text
from db import get_db
from models.settings import SettingsORM  # the settings model
from email_reports import send_daily_report, send_scheduled_report, schedule_due

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

router = APIRouter()

@router.post("/clear-tracking-data")
async def clear_tracking_data(
    request: Request,
    db: Session = Depends(get_db)
):

    try:
        ch = request.state.ch
        ch.command("TRUNCATE TABLE clicks_data")
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"ClickHouse error: {str(e)}"})

    try:
        db.execute(text("TRUNCATE TABLE conversions_data RESTART IDENTITY CASCADE"))
        db.commit()
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"error": f"PostgreSQL error: {str(e)}"})

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
    token = (tg.get("bot_token") or "").strip()
    chat_id = (tg.get("chat_id") or "").strip()

    if not token or not chat_id:
        raise HTTPException(status_code=400, detail="Bot Token and Chat ID are required — fill them in and save settings first")
    if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
        raise HTTPException(status_code=400, detail="Token format looks wrong — it should look like 123456789:AAExampleTokenFormat (from @BotFather)")

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
            detail += " — check the Chat ID: message your bot once, then copy chat.id from api.telegram.org/bot<TOKEN>/getUpdates"
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
    has_api_key = (email_cfg.get("api_key") or "").strip()
    has_smtp = all((email_cfg.get(f) or "").strip() for f in ("smtp_host", "smtp_login", "smtp_password"))
    if not has_api_key and not has_smtp:
        raise HTTPException(status_code=400, detail="Either Brevo API Key or SMTP Host/Login/Password is required — fill it in and save settings first")
    ok, detail = send_daily_report(request.state.ch, email_cfg)
    if not ok:
        raise HTTPException(status_code=500, detail=detail)
    return {"status": "ok", "message": detail}


@router.get("/")
def get_settings(db: Session = Depends(get_db)):
    rows = db.query(SettingsORM).all()
    out = {}
    for row in rows:
        try:
            out[row.name] = json.loads(row.value)
        except Exception:
            out[row.name] = row.value
    return out

@router.post("/")
def save_settings(payload: dict, db: Session = Depends(get_db)):
    # Merge top-level keys server-side under a row lock: two rapid saves of
    # different keys must not clobber each other (read-modify-write race).
    for key, val in payload.items():
        val_str = json.dumps(val)
        row = db.execute(
            text("SELECT id, value FROM settings WHERE name = :n FOR UPDATE"),
            {"n": key}).fetchone()
        if row:
            try:
                existing = json.loads(row[1]) if row[1] else {}
            except Exception:
                existing = None
            if isinstance(existing, dict) and isinstance(val, dict):
                for k, v in val.items():
                    if v is None:
                        existing.pop(k, None)  # explicit null deletes the key
                    else:
                        existing[k] = v
                val_str = json.dumps(existing)
            db.execute(text("UPDATE settings SET value = :v WHERE id = :i"),
                       {"v": val_str, "i": row[0]})
        else:
            db.add(SettingsORM(name=key, value=val_str))
    db.commit()
    return {"status": "ok"}


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
