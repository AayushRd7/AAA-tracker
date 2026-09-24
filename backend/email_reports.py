"""Scheduled daily email reports (Brevo/any SMTP) + test send.

Configuration lives in the `settings` row under the `email_reports` key:
{
    "enabled": bool,
    "smtp_host": "smtp-relay.brevo.com",
    "smtp_port": 587,
    "smtp_login": "...",
    "smtp_password": "...",
    "from_name": "AAA Tracker",
    "from_email": "...",
    "recipients": "a@x.com, b@y.com",
    "hour": 9,               # UTC hour to send the daily report
    "last_sent": "2026-09-24"  # written back after each send
}
"""
import asyncio
import json
import smtplib
import ssl
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.utils import formataddr

from db import SessionLocal
from models.settings import SettingsORM
from models.campaigns import CampaignORM
from schemas import Filters
from clickHouse import get_metrics_series, get_report_breakdown

ALL_STATUSES_METRICS = ("visits", "unique_visits", "clicks", "unique_clicks", "conversions")


def _load_email_config(db):
    row = db.query(SettingsORM).filter_by(name="settings").first()
    if not row or not row.value:
        return {}
    try:
        cfg = json.loads(row.value) or {}
    except Exception:
        return {}
    return cfg.get("email_reports") or {}


def _save_email_config(cfg):
    db = SessionLocal()
    try:
        row = db.query(SettingsORM).filter_by(name="settings").first()
        if row and row.value:
            merged = json.loads(row.value) or {}
            merged["email_reports"] = cfg
            row.value = json.dumps(merged)
            db.commit()
    finally:
        db.close()


def _sum(series, key):
    return sum(r.get(key) or 0 for r in series)


def _aggregate_metrics(series):
    visits = _sum(series, "visits")
    unique_visits = _sum(series, "unique_visits")
    clicks = _sum(series, "clicks")
    unique_clicks = _sum(series, "unique_clicks")
    conversions = _sum(series, "conversions")
    cost = _sum(series, "cost")
    revenue = _sum(series, "revenue")
    profit = revenue - cost
    cr = round(conversions / clicks * 100, 2) if clicks else 0
    epc = round(revenue / clicks, 4) if clicks else 0
    roi = round((revenue - cost) / cost * 100, 2) if cost else None
    return dict(visits=visits, unique_visits=unique_visits, clicks=clicks,
                unique_clicks=unique_clicks, conversions=conversions,
                cost=round(cost, 2), revenue=round(revenue, 2),
                profit=round(profit, 2), cr=cr, epc=epc, roi=roi)


def _fmt(v, suffix=""):
    if v is None:
        return "—"
    if isinstance(v, float):
        v = f"{v:,.2f}"
    else:
        v = f"{v:,}"
    return f"{v}{suffix}"


def build_daily_report_html(ch, db, day: datetime) -> str:
    """One day's metrics + top campaigns, as an HTML email body.

    Table-based layout with inline styles only — renders correctly in
    Gmail, Outlook, Apple Mail etc."""
    day_str = day.strftime("%Y-%m-%d")
    filters = Filters(date_from=day_str, date_to=day_str)
    series = get_metrics_series(ch, filters)
    m = _aggregate_metrics(series)

    breakdown = get_report_breakdown(ch, {"date_from": day_str, "date_to": day_str},
                                     "campaign_id", limit=10)
    names = {c.id: c.name for c in db.query(CampaignORM).all()}
    camp_rows = ""
    for r in breakdown:
        roi = r["roi"]
        roi_style = ("color:#059669;font-weight:600;" if roi and roi > 0
                     else "color:#dc2626;font-weight:600;" if roi and roi < 0
                     else "color:#98a2b3;")
        camp_rows += (
            "<tr>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef1f5;color:#101828;font-weight:500;'>"
            f"{names.get(int(r['dimension']) if r['dimension'].lstrip('-').isdigit() else -1, r['dimension'])}</td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef1f5;text-align:right;color:#475467;'>{r['visits']:,}</td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef1f5;text-align:right;color:#475467;'>{r['clicks']:,}</td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef1f5;text-align:right;color:#475467;'>{r['conversions']:,}</td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef1f5;text-align:right;color:#101828;font-weight:600;'>{_fmt(r['revenue'])}</td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid #eef1f5;text-align:right;{roi_style}'>{_fmt(roi, '%')}</td>"
            "</tr>")
    if not camp_rows:
        camp_rows = ("<tr><td colspan='6' style='padding:16px;text-align:center;"
                     "color:#98a2b3;font-size:13px;'>No campaign traffic recorded on this day</td></tr>")

    metrics_grid = [
        ("Visits", f"{m['visits']:,}"), ("Unique", f"{m['unique_visits']:,}"),
        ("Clicks", f"{m['clicks']:,}"), ("Conversions", f"{m['conversions']:,}"),
        ("CR", f"{m['cr']}%"), ("EPC", f"{m['epc']:.3f}"),
        ("Cost", f"${m['cost']:,.2f}"), ("Profit", f"${m['profit']:,.2f}"),
    ]
    cells = [
        "<td style='padding:6px;' width='25%'>"
        "<div style='background:#f8f9fb;border:1px solid #eef1f5;border-radius:8px;padding:12px 10px;text-align:center;'>"
        f"<div style='font-size:9px;letter-spacing:0.08em;text-transform:uppercase;color:#667085;font-weight:600;'>{label}</div>"
        f"<div style='font-size:16px;font-weight:700;color:#101828;margin-top:3px;'>{value}</div>"
        "</div></td>"
        for label, value in metrics_grid]
    metrics_rows = "".join(
        "<tr>" + "".join(cells[i:i + 4]) + "</tr>" for i in range(0, len(cells), 4))

    roi_color = "#059669" if (m["roi"] or 0) >= 0 else "#dc2626"
    roi_bg = "rgba(5,150,105,0.08)" if (m["roi"] or 0) >= 0 else "rgba(220,38,38,0.08)"

    return f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f5f6f8;">
<div style="display:none;font-size:1px;color:#f5f6f8;">Your daily performance summary for {day_str}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f6f8;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;">

  <!-- header -->
  <tr><td style="background:#1a2332;border-radius:12px 12px 0 0;padding:20px 28px;">
    <table role="presentation" width="100%"><tr>
      <td>
        <table role="presentation" cellpadding="0" cellspacing="0"><tr>
          <td style="padding-right:10px;">
            <table role="presentation" cellpadding="0" cellspacing="0"><tr><td style="width:34px;height:34px;background:#16a34a;border-radius:9px;text-align:center;vertical-align:middle;color:#ffffff;font-size:16px;font-weight:800;">A</td></tr></table>
          </td>
          <td>
            <div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:700;letter-spacing:0.12em;color:#ffffff;">AAA&nbsp;TRACKER</div>
            <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#8ea0b5;margin-top:1px;">Campaign Analytics</div>
          </td>
        </tr></table>
      </td>
      <td align="right" style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#8ea0b5;">{day_str}</td>
    </tr></table>
  </td></tr>

  <!-- body card -->
  <tr><td style="background:#ffffff;padding:28px;border:1px solid #e4e7ec;border-top:none;border-radius:0 0 12px 12px;">

    <!-- hero: revenue + roi -->
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      <tr>
        <td style="padding-right:12px;">
          <div style="font-size:10px;letter-spacing:0.08em;text-transform:uppercase;color:#667085;font-weight:600;">Revenue</div>
          <div style="font-family:Arial,Helvetica,sans-serif;font-size:30px;font-weight:800;color:#101828;margin-top:2px;">${m['revenue']:,.2f}</div>
          <div style="font-size:12px;color:#667085;margin-top:2px;">Profit <span style="font-weight:700;color:{'#059669' if m['profit'] >= 0 else '#dc2626'};">${m['profit']:,.2f}</span></div>
        </td>
        <td align="right">
          <table role="presentation" cellpadding="0" cellspacing="0"><tr>
            <td style="background:{roi_bg};border-radius:20px;padding:8px 16px;">
              <span style="font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:#667085;font-weight:600;">ROI&nbsp;</span>
              <span style="font-size:18px;font-weight:800;color:{roi_color};">{_fmt(m['roi'], '%')}</span>
            </td>
          </tr></table>
        </td>
      </tr>
    </table>

    <!-- metrics grid -->
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
      {metrics_rows}
    </table>

    <!-- top campaigns -->
    <div style="font-size:14px;font-weight:700;color:#101828;margin-bottom:10px;">Top campaigns by traffic</div>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eef1f5;border-radius:8px;border-collapse:separate;overflow:hidden;">
      <tr style="background:#f9fafb;">
        <th style="padding:9px 14px;text-align:left;font-size:10px;letter-spacing:0.07em;text-transform:uppercase;color:#667085;font-weight:600;">Campaign</th>
        <th style="padding:9px 14px;text-align:right;font-size:10px;letter-spacing:0.07em;text-transform:uppercase;color:#667085;font-weight:600;">Visits</th>
        <th style="padding:9px 14px;text-align:right;font-size:10px;letter-spacing:0.07em;text-transform:uppercase;color:#667085;font-weight:600;">Clicks</th>
        <th style="padding:9px 14px;text-align:right;font-size:10px;letter-spacing:0.07em;text-transform:uppercase;color:#667085;font-weight:600;">Conv.</th>
        <th style="padding:9px 14px;text-align:right;font-size:10px;letter-spacing:0.07em;text-transform:uppercase;color:#667085;font-weight:600;">Revenue</th>
        <th style="padding:9px 14px;text-align:right;font-size:10px;letter-spacing:0.07em;text-transform:uppercase;color:#667085;font-weight:600;">ROI</th>
      </tr>
      {camp_rows}
    </table>

    <div style="border-top:1px solid #eef1f5;margin-top:28px;padding-top:14px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#98a2b3;">
      Sent automatically by AAA Tracker · generated {datetime.utcnow().strftime('%d %b %Y %H:%M UTC')} · covers {day_str} (UTC)
    </div>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def send_email(cfg: dict, subject: str, html: str, recipients) -> None:
    sender_email = cfg.get("from_email") or cfg.get("smtp_login")
    sender_name = cfg.get("from_name") or "AAA Tracker"

    # Brevo API path (preferred when an api_key is set — not subject to SMTP IP restrictions)
    api_key = (cfg.get("api_key") or "").strip()
    if api_key:
        import httpx
        resp = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": api_key, "content-type": "application/json"},
            json={
                "sender": {"name": sender_name, "email": sender_email},
                "to": [{"email": r} for r in recipients],
                "subject": subject,
                "htmlContent": html,
            },
            timeout=25)
        if resp.status_code >= 400:
            raise RuntimeError(f"Brevo API error {resp.status_code}: {resp.text[:200]}")
        return

    # Generic SMTP path (STARTTLS)
    msg = MIMEText(html, "html")
    msg["Subject"] = subject
    msg["From"] = formataddr((sender_name, sender_email))
    msg["To"] = ", ".join(recipients)

    host = (cfg.get("smtp_host") or "").strip()
    port = int(cfg.get("smtp_port") or 587)
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=25) as s:
        s.starttls(context=context)
        s.login(cfg.get("smtp_login"), cfg.get("smtp_password"))
        s.sendmail(msg["From"], recipients, msg.as_string())


def send_daily_report(ch, cfg, day: datetime = None):
    """Build + send the daily report; returns (ok, detail)."""
    recipients = [r.strip() for r in (cfg.get("recipients") or "").split(",") if r.strip()]
    if not recipients:
        return False, "No recipient email configured"
    db = SessionLocal()
    try:
        day = day or datetime.utcnow() - timedelta(days=1)
        html = build_daily_report_html(ch, db, day)
        subject = f"AAA Tracker Daily Report — {day.strftime('%Y-%m-%d')}"
        send_email(cfg, subject, html, recipients)
        return True, f"Report sent to {', '.join(recipients)}"
    except Exception as e:
        return False, str(e)
    finally:
        db.close()


async def email_report_loop():
    """Background scheduler: sends the daily report at the configured UTC hour."""
    from app import app  # clickhouse client lives on app.state
    while True:
        try:
            await asyncio.sleep(60)
            db = SessionLocal()
            try:
                cfg = _load_email_config(db)
                if not cfg.get("enabled"):
                    continue
                recipients = [r.strip() for r in (cfg.get("recipients") or "").split(",") if r.strip()]
                if not recipients:
                    continue
                now = datetime.utcnow()
                today = now.strftime("%Y-%m-%d")
                hour = int(cfg.get("hour", 9) or 9)
                if now.hour != hour or cfg.get("last_sent") == today:
                    continue
                ok, detail = send_daily_report(
                    app.state.ch, cfg, now - timedelta(days=1))
                print(f"Email report ({today}): ok={ok} {detail}")
                if ok:
                    cfg["last_sent"] = today
                    _save_email_config(cfg)
            finally:
                db.close()
        except Exception as e:
            print("Email report loop error:", e)