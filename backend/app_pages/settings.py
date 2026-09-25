from fastapi import APIRouter, Depends, HTTPException
import json
import re
import httpx
from sqlalchemy.orm import Session
from sqlalchemy import text
from db import get_db
from models.settings import SettingsORM  # the settings model
from email_reports import send_daily_report

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
    for key, val in payload.items():
        val_str = json.dumps(val)
        setting = db.query(SettingsORM).filter_by(name=key).first()
        if setting:
            setting.value = val_str
        else:
            setting = SettingsORM(name=key, value=val_str)
            db.add(setting)
    db.commit()
    return {"status": "ok"}
