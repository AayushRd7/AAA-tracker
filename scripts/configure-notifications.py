#!/usr/bin/env python3
"""One-shot notification setup:
1. reads the chat id from the Telegram bot (getUpdates)
2. saves Telegram + Brevo email settings
3. sends a Telegram test message and a test daily report email
Run from the repo root:  python3 scripts/configure-notifications.py

Credentials come from environment variables — no secrets are stored in the repo:
  TELEGRAM_BOT_TOKEN   from @BotFather
  BREVO_SMTP_LOGIN / BREVO_SMTP_KEY   from Brevo → SMTP & API
  BREVO_API_KEY        from Brevo → SMTP & API → API keys (recommended; not IP-restricted)
  REPORT_FROM_EMAIL / REPORT_RECIPIENTS
"""
import json
import os
import urllib.request
import urllib.error

BASE = "http://localhost/backend"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SMTP = {
    "smtp_host": os.environ.get("BREVO_SMTP_HOST", "smtp-relay.brevo.com"),
    "smtp_port": 587,
    "smtp_login": os.environ.get("BREVO_SMTP_LOGIN", ""),
    "smtp_password": os.environ.get("BREVO_SMTP_KEY", ""),
    "api_key": os.environ.get("BREVO_API_KEY", ""),
    "from_name": "AAA Tracker",
    "from_email": os.environ.get("REPORT_FROM_EMAIL", ""),
    "recipients": os.environ.get("REPORT_RECIPIENTS", ""),
    "hour": 9,
}


def http(method, url, data=None, headers=None):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(data).encode() if data else None,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main():
    if not BOT_TOKEN:
        print("ERROR: set TELEGRAM_BOT_TOKEN (and the Brevo variables) first — see the docstring.")
        return

    # 1. chat id from the bot's updates
    with urllib.request.urlopen(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", timeout=15) as r:
        updates = json.load(r)
    chat_id = None
    if updates.get("ok"):
        for u in updates.get("result", []):
            msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
            c = msg.get("chat") or {}
            if c:
                chat_id = str(c["id"])
                print(f"found chat: id={chat_id} type={c.get('type')} name={c.get('first_name') or c.get('title')}")
                break
    if not chat_id:
        print("ERROR: no chats found for this bot. Send it any message in Telegram first, then re-run.")
        return

    # 2. login and save settings
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    resp = opener.open(urllib.request.Request(
        f"{BASE}/api/login", method="POST",
        data=json.dumps({"username": "tracker_admin", "password": "admin"}).encode(),
        headers={"Content-Type": "application/json"}), timeout=15)
    resp.read()

    payload = {
        "settings": {
            "domain": "localhost", "currency": "USD", "timezone": "UTC",
            "telegram": {
                "enabled": True, "bot_token": BOT_TOKEN, "chat_id": chat_id,
                "statuses": {"lead": True, "sale": True, "upsale": True,
                             "rejected": True, "hold": True, "trash": True},
            },
            "email_reports": dict(SMTP, enabled=True),
        },
        "subIdMapping": [],
    }
    req = urllib.request.Request(
        f"{BASE}/api/settings/", method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    opener.open(req, timeout=15).read()
    print("settings saved")

    # 3. tests
    try:
        r = opener.open(urllib.request.Request(
            f"{BASE}/api/settings/telegram-test", method="POST"), timeout=30)
        print("telegram test:", r.status, json.loads(r.read() or b"{}"))
    except urllib.error.HTTPError as e:
        print("telegram test FAILED:", e.code, e.read().decode()[:300])
    try:
        r = opener.open(urllib.request.Request(
            f"{BASE}/api/settings/email-test", method="POST"), timeout=60)
        print("email test:", r.status, json.loads(r.read() or b"{}"))
    except urllib.error.HTTPError as e:
        print("email test FAILED:", e.code, e.read().decode()[:300])


if __name__ == "__main__":
    main()