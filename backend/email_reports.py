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
import html
import json
import smtplib
import ssl
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.utils import formataddr

from db import SessionLocal
from sqlalchemy import text
from models.settings import SettingsORM
from models.campaigns import CampaignORM
from schemas import Filters
from clickHouse import get_metrics_series, get_report_breakdown, get_report_breakdown_multi, sum_rows

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
    """Merge the email_reports block into the settings row under a row lock —
    a plain read-modify-write clobbers concurrent saves of other keys."""
    db = SessionLocal()
    try:
        row = db.execute(
            text("SELECT value FROM settings WHERE name = 'settings' FOR UPDATE")
        ).fetchone()
        if row and row[0]:
            merged = json.loads(row[0]) or {}
            merged["email_reports"] = cfg
            db.execute(text("UPDATE settings SET value = :v WHERE name = 'settings'"),
                       {"v": json.dumps(merged)})
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
            f"{html.escape(str(names.get(int(r['dimension']) if r['dimension'].lstrip('-').isdigit() else -1, r['dimension'])))}</td>"
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


# ---------------------------------------------------------------------------
# G53: per-report scheduled emails — a saved report's breakdown mailed on a
# daily/weekly cadence. Schedules live under 'report_email_schedules'.
# ---------------------------------------------------------------------------

def schedule_due(schedule: dict, now: datetime) -> bool:
    """True when the schedule should fire at ``now`` (UTC).

    Fires once per day at hour_utc; weekly additionally requires 7+ days
    since the last send (or never sent). last_sent holds a YYYY-MM-DD date.
    """
    if now.hour != int(schedule.get("hour_utc", 9) or 9):
        return False
    last = (schedule.get("last_sent") or "").strip()
    today = now.strftime("%Y-%m-%d")
    if last == today:
        return False
    if (schedule.get("frequency") or "daily") == "weekly":
        if not last:
            return True
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d")
        except ValueError:
            return True
        return (now - last_dt).days >= 7
    return True


def build_saved_report_html(ch, saved_report: dict) -> str:
    """Compact HTML table of a saved report's breakdown (level-1 rows)."""
    from clickHouse import REPORT_DIMENSIONS

    cfg = saved_report.get("config") or {}
    dimensions = [d for d in (cfg.get("dimensions") or []) if d in REPORT_DIMENSIONS][:5] or ["campaign_id"]
    date_range = cfg.get("date_range") or []
    filters = {
        "date_from": date_range[0] if len(date_range) > 0 else None,
        "date_to": date_range[1] if len(date_range) > 1 else None,
        "campaigns": cfg.get("campaigns") or [],
    }
    rows = get_report_breakdown_multi(
        ch, filters, dimensions,
        sort_by=cfg.get("sortBy"), sort_dir=cfg.get("sortDir") or "desc",
    )
    totals = sum_rows(rows)

    dim_labels = [d.replace("_", " ").title() for d in dimensions]
    cols = (cfg.get("columns") or ["visits", "clicks", "conversions", "revenue", "cr", "epc", "roi"])
    cols = [c for c in cols if c in REPORT_BREAKDOWN_EXPORTABLE] or ["visits", "clicks", "conversions", "revenue", "roi"]

    head = "".join(
        f"<th style='padding:8px 12px;text-align:left;font-size:10px;letter-spacing:0.07em;"
        f"text-transform:uppercase;color:#667085;font-weight:600;'>{d}</th>" for d in dim_labels)
    head += "".join(
        f"<th style='padding:8px 12px;text-align:right;font-size:10px;letter-spacing:0.07em;"
        f"text-transform:uppercase;color:#667085;font-weight:600;'>{REPORT_BREAKDOWN_EXPORTABLE[c]}</th>"
        for c in cols)

    def cell(v, c):
        if c in ("cost", "revenue", "profit"):
            return f"${float(v or 0):,.2f}"
        if c in ("cr", "roi", "rejected_rate", "click_through_rate"):
            return _fmt(float(v or 0), "%")
        if c == "epc":
            return f"{float(v or 0):.5f}"
        return _fmt(v)

    body = ""
    for r in rows:
        chain = ((r.get("parent_key") or "").split("\x1f") if r.get("parent_key") else []) + [r.get("value") or "(empty)"]
        body += "<tr>"
        for i in range(len(dimensions)):
            v = chain[i] if i < len(chain) else ""
            body += (f"<td style='padding:8px 12px;border-bottom:1px solid #eef1f5;"
                     f"color:#101828;'>{html.escape(str(v))}</td>")
        for c in cols:
            v = r.get(c)
            style = "padding:8px 12px;border-bottom:1px solid #eef1f5;text-align:right;color:#475467;"
            if c == "profit":
                style += f"font-weight:600;color:{'#059669' if (v or 0) >= 0 else '#dc2626'};"
            if c == "roi":
                style += f"font-weight:600;color:{'#059669' if (v or 0) >= 0 else '#dc2626'};"
            body += f"<td style='{style}'>{cell(v, c)}</td>"
        body += "</tr>"
    if not body:
        body = (f"<tr><td colspan='{len(dimensions) + len(cols)}' style='padding:16px;text-align:center;"
                f"color:#98a2b3;font-size:13px;'>No data for this report's date range</td></tr>")

    totals_row = ""
    for i in range(len(dimensions)):
        totals_row += "<td style='padding:8px 12px;font-weight:700;color:#101828;'>Total</td>" if i == 0 \
            else "<td style='padding:8px 12px;'></td>"
    for c in cols:
        totals_row += (f"<td style='padding:8px 12px;text-align:right;font-weight:700;color:#101828;'>"
                       f"{cell(totals.get(c), c)}</td>")

    date_note = " — ".join(x for x in (filters["date_from"], filters["date_to"]) if x) or "all time"
    name = html.escape(str(saved_report.get("name") or "Saved report"))
    return f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f5f6f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f6f8;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;">
  <tr><td style="background:#1a2332;border-radius:12px 12px 0 0;padding:18px 24px;">
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:700;letter-spacing:0.12em;color:#ffffff;">AAA&nbsp;TRACKER</div>
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#8ea0b5;margin-top:2px;">Scheduled report · {name} · {date_note} (UTC)</div>
  </td></tr>
  <tr><td style="background:#ffffff;padding:20px 24px;border:1px solid #e4e7ec;border-top:none;border-radius:0 0 12px 12px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eef1f5;border-collapse:separate;">
      <tr style="background:#f9fafb;">{head}</tr>
      {body}
      <tr style="background:#f9fafb;">{totals_row}</tr>
    </table>
    <div style="border-top:1px solid #eef1f5;margin-top:20px;padding-top:12px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#98a2b3;">
      Sent automatically by AAA Tracker · generated {datetime.utcnow().strftime('%d %b %Y %H:%M UTC')}
    </div>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


# breakdown columns allowed in the emailed table (key -> header label)
REPORT_BREAKDOWN_EXPORTABLE = {
    "visits": "Visits", "unique_visits": "Unique", "clicks": "Clicks",
    "unique_clicks": "Unique Clicks", "leads": "Leads", "conversions": "Conv.",
    "rejected": "Rejected", "cost": "Cost", "revenue": "Revenue", "profit": "Profit",
    "cr": "CR %", "epc": "EPC", "roi": "ROI %",
    "rejected_rate": "Rej. %", "click_through_rate": "CTR %",
}


def send_scheduled_report(ch, email_cfg: dict, schedule: dict) -> tuple:
    """Build + send one scheduled saved report; returns (ok, detail)."""
    recipients = [r.strip() for r in (schedule.get("recipients") or "").split(",") if r.strip()]
    if not recipients:
        return False, "No recipients configured for this schedule"
    if not (email_cfg.get("api_key") or "").strip() and not all(
            (email_cfg.get(f) or "").strip() for f in ("smtp_host", "smtp_login", "smtp_password")):
        return False, "Email is not configured — set a Brevo API key or SMTP credentials in Settings first"

    db = SessionLocal()
    try:
        row = db.query(SettingsORM).filter_by(name="saved_reports").first()
        reports = []
        if row and row.value:
            try:
                reports = json.loads(row.value) or []
            except Exception:
                reports = []
        report = next((r for r in reports if r.get("id") == schedule.get("report_id")), None)
        if not report:
            return False, "Saved report no longer exists"
        html = build_saved_report_html(ch, report)
        subject = f"AAA Tracker Report — {report.get('name')} — {datetime.utcnow().strftime('%Y-%m-%d')}"
        send_email(email_cfg, subject, html, recipients)
        return True, f"Report sent to {', '.join(recipients)}"
    except Exception as e:
        return False, str(e)
    finally:
        db.close()


def _mark_schedule_sent(db, report_id: str, today: str):
    row = db.execute(
        text("SELECT value FROM settings WHERE name = 'report_email_schedules' FOR UPDATE")
    ).fetchone()
    if not row or not row[0]:
        return
    try:
        schedules = json.loads(row[0]) or []
    except Exception:
        return
    changed = False
    for s in schedules:
        if s.get("report_id") == report_id:
            s["last_sent"] = today
            changed = True
    if changed:
        db.execute(text("UPDATE settings SET value = :v WHERE name = 'report_email_schedules'"),
                   {"v": json.dumps(schedules)})
        db.commit()


async def email_report_loop():
    """Background scheduler: sends the daily report at the configured UTC hour,
    plus any due per-report schedules (G53)."""
    from clickHouse import get_clickhouse_client
    while True:
        try:
            await asyncio.sleep(60)
            db = SessionLocal()
            try:
                cfg = _load_email_config(db)
                now = datetime.utcnow()
                today = now.strftime("%Y-%m-%d")

                # Global daily report (unchanged behavior)
                if cfg.get("enabled"):
                    recipients = [r.strip() for r in (cfg.get("recipients") or "").split(",") if r.strip()]
                    if recipients:
                        hour = int(cfg.get("hour", 9) or 9)
                        if now.hour == hour and cfg.get("last_sent") != today:
                            ch = get_clickhouse_client()
                            try:
                                ok, detail = await asyncio.to_thread(
                                    send_daily_report, ch, cfg, now - timedelta(days=1))
                            finally:
                                ch.close()
                            print(f"Email report ({today}): ok={ok} {detail}")
                            if ok:
                                cfg["last_sent"] = today
                                _save_email_config(cfg)

                # G53: per-report schedules — send each due report
                sched_row = db.query(SettingsORM).filter_by(name="report_email_schedules").first()
                schedules = []
                if sched_row and sched_row.value:
                    try:
                        schedules = json.loads(sched_row.value) or []
                    except Exception:
                        schedules = []
                for schedule in schedules:
                    try:
                        if not schedule_due(schedule, now):
                            continue
                        ch = get_clickhouse_client()
                        try:
                            ok, detail = await asyncio.to_thread(
                                send_scheduled_report, ch, cfg, schedule)
                        finally:
                            ch.close()
                        print(f"Email scheduled report {schedule.get('report_id')} ({today}): ok={ok} {detail}")
                        if ok:
                            _mark_schedule_sent(db, schedule.get("report_id"), today)
                    except Exception as e:
                        print("Email scheduled report error:", e)
            finally:
                db.close()
        except Exception as e:
            print("Email report loop error:", e)