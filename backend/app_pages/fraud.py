"""Fraud & cloaking dashboard.

Admin-plane readout over ClickHouse fraud signals (is_bot, fraud_score) plus
the campaign-level "shield" cloaking blocks stored in campaigns.config:
  {"enabled": bool, "whitelists": {"ips": [CIDR], "referers": [substrings],
   "ua_regex": str}, "action": "blank"|"404"|"allow", "honeypot": bool}

Endpoints:
  GET /api/fraud/summary        — 24h cards, top fraud IPs / UAs, shield stats
  GET /api/fraud/feed?after=    — live feed of bot/high-score clicks (polling)
  GET/PUT /api/fraud/bot-lists  — global editable lists in settings "bot_lists"
  GET /api/fraud/honeypot-hits  — recent PG honeypot_hits rows
  GET /api/fraud/evidence.csv   — refund-claim evidence, one flat CSV table
  GET /api/fraud/evidence.html  — same evidence as a printable self-contained page
"""
import ipaddress
import json
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from tenant_context import current_tenant
from models.settings import SettingsORM

router = APIRouter()

FRAUD_SCORE_THRESHOLD = 50
FEED_LIMIT = 100


# ---------------------------------------------------------------------------
# Schema (idempotent — coordinates with the tracking-plane migration)
# ---------------------------------------------------------------------------

def ensure_fraud_schema(ch) -> None:
    """clicks_data.fraud_score (parallel agent's inserts rely on it existing);
    PG honeypot_hits landing table. Both are IF-NOT-EXISTS/no-op safe."""
    try:
        ch.command("ALTER TABLE clicks_data ADD COLUMN IF NOT EXISTS fraud_score UInt8 DEFAULT 0")
    except Exception as e:
        print("fraud schema (clickhouse):", e)
    from db import engine
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS honeypot_hits (
                    id BIGSERIAL PRIMARY KEY,
                    visitor_key TEXT,
                    ip VARCHAR(64),
                    ua TEXT,
                    campaign_id INTEGER,
                    received_at TIMESTAMP NOT NULL DEFAULT now(),
                    tenant_id INTEGER NOT NULL DEFAULT 1
                )"""))
            # A database created before multi-tenancy (or by an older build of
            # this module) needs the column added in place.
            conn.execute(text("ALTER TABLE honeypot_hits "
                              "ADD COLUMN IF NOT EXISTS tenant_id INTEGER"))
            conn.execute(text("UPDATE honeypot_hits SET tenant_id = 1 "
                              "WHERE tenant_id IS NULL"))
            conn.execute(text("ALTER TABLE honeypot_hits "
                              "ALTER COLUMN tenant_id SET NOT NULL"))
            conn.commit()
    except Exception as e:
        print("fraud schema (postgres):", e)


# ---------------------------------------------------------------------------
# Settings helpers (settings row 'settings', JSON block "bot_lists")
# ---------------------------------------------------------------------------

BOT_LIST_KEYS = ("ua_regex", "ip_cidrs", "referer_regex")


def _load_main_settings(db: Session) -> dict:
    row = db.query(SettingsORM).filter_by(name="settings").first()
    if row and row.value:
        try:
            return json.loads(row.value) or {}
        except Exception:
            return {}
    return {}


def _validate_bot_lists(block: dict) -> dict:
    if not isinstance(block, dict):
        raise HTTPException(status_code=422, detail="bot_lists must be an object")
    out = {}
    for key in BOT_LIST_KEYS:
        val = block.get(key)
        if val is None:
            val = []
        if not isinstance(val, list):
            raise HTTPException(status_code=422, detail=f"{key} must be a list")
        out[key] = [str(v).strip() for v in val if str(v).strip()]
    for key in ("ua_regex", "referer_regex"):
        for pat in out[key]:
            try:
                re.compile(pat)
            except re.error as e:
                raise HTTPException(status_code=422,
                                    detail=f"Invalid {key} pattern {pat!r}: {e}")
    for cidr in out["ip_cidrs"]:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"Invalid ip_cidr {cidr!r}: {e}")
    return out


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/summary")
def fraud_summary(request: Request, db: Session = Depends(get_db)):
    ch = request.state.ch
    # Interpolated (not parameterised): the value is an int from the request
    # context, never user input.
    period = (f"tenant_id = {int(current_tenant())} "
              "AND received_at >= now() - toIntervalHour(24)")

    row = ch.query(f"""
        SELECT count() AS total,
               countIf(is_bot = true) AS bot_clicks,
               avgOrNull(fraud_score) AS avg_fraud_score,
               avgOrNull(cost) AS avg_cost
        FROM clicks_data
        WHERE {period}""").result_rows
    total, bot_clicks, avg_fraud_score, avg_cost = (row[0] if row else (0, 0, None, None))
    total, bot_clicks = int(total or 0), int(bot_clicks or 0)

    # Est. savings: bot clicks x the average cost of a NON-bot click in the
    # same window (blocked/untracked clicks cannot be counted directly).
    row = ch.query(f"""
        SELECT avgOrNull(cost) AS avg_human_cost
        FROM clicks_data
        WHERE {period} AND NOT (is_bot = true)""").result_rows
    avg_human_cost = float(row[0][0] or 0) if row else 0.0
    est_savings = round(bot_clicks * avg_human_cost, 4)

    fraud_where = f"({period}) AND (is_bot = true OR fraud_score >= {FRAUD_SCORE_THRESHOLD})"
    ip_rows = ch.query(f"""
        SELECT if(empty(ip_full), toString(ip), ip_full) AS ip, count() AS hits,
               round(avg(fraud_score), 1) AS avg_score,
               max(received_at) AS last_seen
        FROM clicks_data
        WHERE {fraud_where}
        GROUP BY ip
        ORDER BY hits DESC
        LIMIT 10""").result_rows
    top_ips = [{"ip": r[0], "hits": int(r[1]), "avg_score": float(r[2] or 0),
                "last_seen": str(r[3])} for r in ip_rows]

    ua_rows = ch.query(f"""
        SELECT browser AS ua, count() AS hits,
               round(avg(fraud_score), 1) AS avg_score,
               max(received_at) AS last_seen
        FROM clicks_data
        WHERE {fraud_where}
        GROUP BY browser
        ORDER BY hits DESC
        LIMIT 10""").result_rows
    top_uas = [{"ua": r[0], "hits": int(r[1]), "avg_score": float(r[2] or 0),
                "last_seen": str(r[3])} for r in ua_rows]

    shield_rows = db.execute(text(
        "SELECT id, name, config FROM campaigns "
        "WHERE archived = false AND tenant_id = :tid"),
        {"tid": current_tenant()}).fetchall()
    shields = []
    for cid, cname, config in shield_rows:
        shield = (config or {}).get("shield") or {}
        if not isinstance(shield, dict) or not shield.get("enabled"):
            continue
        shields.append({
            "campaign_id": cid, "campaign_name": cname,
            "action": shield.get("action") or "blank",
            "honeypot": bool(shield.get("honeypot")),
            "whitelist_ips": len((shield.get("whitelists") or {}).get("ips") or []),
            "whitelist_referers": len((shield.get("whitelists") or {}).get("referers") or []),
            "whitelist_ua_regex": bool((shield.get("whitelists") or {}).get("ua_regex")),
        })

    return {
        "period_hours": 24,
        "total_clicks": total,
        "bot_clicks": bot_clicks,
        "bot_share_pct": round(bot_clicks / total * 100, 2) if total else 0.0,
        "avg_fraud_score": round(float(avg_fraud_score or 0), 2),
        "avg_cost_non_bot": round(avg_human_cost, 4),
        "est_savings": est_savings,
        "top_ips": top_ips,
        "top_uas": top_uas,
        "shields": shields,
    }


@router.get("/feed")
def fraud_feed(request: Request, after: str = None, limit: int = FEED_LIMIT):
    ch = request.state.ch
    limit = min(max(int(limit or FEED_LIMIT), 1), 500)
    conditions = ["tenant_id = %(tenant_id)s",
                  f"(is_bot = true OR fraud_score >= {FRAUD_SCORE_THRESHOLD})"]
    params = {"limit": limit, "tenant_id": current_tenant()}
    if after:
        conditions.append("received_at > %(after)s")
        params["after"] = after
    where_clause = f"WHERE {' AND '.join(conditions)}"

    query = f"""
        SELECT
            received_at, visitor_id,
            if(empty(ip_full), toString(ip), ip_full) AS ip,
            campaign_id, country, device_type,
            os, browser, referrer, url, status, is_bot, is_using_proxy,
            fraud_score
        FROM clicks_data
        {where_clause}
        ORDER BY received_at DESC
        LIMIT %(limit)s
    """
    result = ch.query(query, parameters=params)
    columns = result.column_names
    return [dict(zip(columns, row)) for row in result.result_rows]


# ---------------------------------------------------------------------------
# Refund-claim evidence export
#
# The artefact a media buyer sends an ad network when disputing fraudulent
# traffic: the flagged clicks in a date range aggregated per offending
# dimension, plus the totals and the exact filter set so the claim is
# self-describing and reproducible. Two formats:
#   GET /evidence.csv   — one flat table; a `section` column preserves shape
#   GET /evidence.html  — self-contained printable page ("Print to PDF")
# A single flat CSV is chosen over a zip of per-section files on purpose: a
# refund claim is emailed as one attachment, and one table with a `section`
# column opens directly in any spreadsheet while staying filterable by section
# (no unzip step, no locked-down archive tooling on the recipient's side).
# No credential and no storage/serving technology name appears in the output.
# ---------------------------------------------------------------------------

EVIDENCE_SCORE_THRESHOLD = FRAUD_SCORE_THRESHOLD  # one threshold drives card + export
EVIDENCE_ROW_LIMIT = 500        # per dimension; the artefact says when it truncates
EVIDENCE_MAX_RANGE_DAYS = 62    # ~two months — a longer window is a different report

# (key, human title, grouping expression). Empty values tombstone to "(unknown)"
# so a blank bucket is visible in the evidence rather than silently dropped.
EVIDENCE_DIMENSIONS = (
    ("ip", "IP address", "if(empty(ip_full), toString(ip), ip_full)"),
    ("user_agent", "User agent",
     "if(empty(user_agent), if(empty(browser), '(unknown)', browser), user_agent)"),
    ("country", "Country", "if(empty(country), '(unknown)', country)"),
    ("traffic_source", "Traffic source",
     "if(empty(traffic_source_name), '(unknown)', traffic_source_name)"),
    ("sub_id", "Sub-ID", "if(empty(sub_id_1), '(unknown)', sub_id_1)"),
    ("campaign", "Campaign", "toString(coalesce(campaign_id, -1))"),
)

# Flat CSV columns. meta/totals rows carry the key in `key` and the figure in
# `value`; a dimension row carries the offending value in `key` (leaving
# `value` blank) and its counts across the remaining columns.
EVIDENCE_CSV_FIELDS = ("section", "key", "value", "hits", "bot_hits",
                       "high_score_hits", "avg_fraud_score", "max_fraud_score",
                       "cost", "first_seen", "last_seen")


def _parse_evidence_range(date_from: str, date_to: str) -> tuple:
    """Default to the last 7 days; reject a bad format, an inverted range or an
    oversized window — a bounded query is the contract here, not a hope."""
    from datetime import timedelta
    today = datetime.utcnow().date()
    try:
        start = (datetime.strptime(str(date_from), "%Y-%m-%d").date()
                 if date_from else today - timedelta(days=6))
        end = (datetime.strptime(str(date_to), "%Y-%m-%d").date()
               if date_to else today)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400,
                            detail="date_from and date_to must be YYYY-MM-DD")
    if start > end:
        raise HTTPException(status_code=400, detail="date_from must not be after date_to")
    if (end - start).days + 1 > EVIDENCE_MAX_RANGE_DAYS:
        raise HTTPException(status_code=400,
                            detail=f"Date range too large (max {EVIDENCE_MAX_RANGE_DAYS} days)")
    return start.isoformat(), end.isoformat()


def _evidence_where(tenant) -> str:
    """Tenant-scoped, date-bounded predicate; the dates ride in as query params
    (only the tenant is interpolated — an int from the request context)."""
    return (f"tenant_id = {int(tenant)} "
            "AND toDate(received_at) BETWEEN toDate(%(date_from)s) AND toDate(%(date_to)s)")


def _evidence_totals(ch, tenant, date_from, date_to) -> dict:
    flag = f"(is_bot = true OR fraud_score >= {EVIDENCE_SCORE_THRESHOLD})"
    rows = ch.query(f"""
        SELECT count() AS tracked_events,
               countIf(click = true) AS clicks,
               countIf({flag}) AS flagged_events,
               countIf(click = true AND {flag}) AS flagged_clicks,
               countIf(is_bot = true) AS bot_events,
               round(avg(fraud_score), 2) AS avg_fraud_score,
               max(fraud_score) AS max_fraud_score,
               round(sum(toFloat64(coalesce(cost, 0))), 4) AS total_spend,
               round(sumIf(toFloat64(coalesce(cost, 0)), {flag}), 4) AS wasted_spend
        FROM clicks_data
        WHERE {_evidence_where(tenant)}""",
        parameters={"date_from": date_from, "date_to": date_to}).result_rows
    r = rows[0] if rows else (0,) * 9
    tracked = int(r[0] or 0)
    bot_events = int(r[4] or 0)
    totals = {
        "tracked_events": tracked,
        "clicks": int(r[1] or 0),
        "flagged_events": int(r[2] or 0),
        "flagged_clicks": int(r[3] or 0),
        "bot_events": bot_events,
        "bot_share_pct": round(bot_events / tracked * 100, 2) if tracked else 0.0,
        "avg_fraud_score": float(r[5] or 0),
        "max_fraud_score": int(r[6] or 0),
        "total_spend": float(r[7] or 0),
        "wasted_spend": float(r[8] or 0),
    }
    totals["flagged_share_pct"] = (round(totals["flagged_events"] / tracked * 100, 2)
                                   if tracked else 0.0)
    return totals


def _evidence_section_rows(ch, tenant, dim_expr, date_from, date_to) -> tuple:
    """Aggregate one dimension over flagged clicks only. Over-fetches by one to
    detect truncation, then drops the sentinel so the reported cap stays exact."""
    flag = f"(is_bot = true OR fraud_score >= {EVIDENCE_SCORE_THRESHOLD})"
    rows = ch.query(f"""
        SELECT {dim_expr} AS value,
               count() AS hits,
               countIf(is_bot = true) AS bot_hits,
               countIf(fraud_score >= {EVIDENCE_SCORE_THRESHOLD}) AS high_score_hits,
               round(avg(fraud_score), 1) AS avg_fraud_score,
               max(fraud_score) AS max_fraud_score,
               round(sum(toFloat64(coalesce(cost, 0))), 4) AS cost,
               min(received_at) AS first_seen,
               max(received_at) AS last_seen
        FROM clicks_data
        WHERE {_evidence_where(tenant)} AND {flag}
        GROUP BY value
        ORDER BY hits DESC, value ASC
        LIMIT {EVIDENCE_ROW_LIMIT + 1}""",
        parameters={"date_from": date_from, "date_to": date_to}).result_rows
    truncated = len(rows) > EVIDENCE_ROW_LIMIT
    out = []
    for r in rows[:EVIDENCE_ROW_LIMIT]:
        bot_hits, high_hits = int(r[2] or 0), int(r[3] or 0)
        reasons = []
        if bot_hits:
            reasons.append("bot")
        if high_hits:
            reasons.append(f"score>={EVIDENCE_SCORE_THRESHOLD}")
        out.append({
            "value": str(r[0]),
            "hits": int(r[1] or 0),
            "bot_hits": bot_hits,
            "high_score_hits": high_hits,
            "avg_fraud_score": float(r[4] or 0),
            "max_fraud_score": int(r[5] or 0),
            "cost": float(r[6] or 0),
            "first_seen": str(r[7]) if r[7] is not None else "",
            "last_seen": str(r[8]) if r[8] is not None else "",
            "flag_reasons": "+".join(reasons) or "flagged",
        })
    return out, truncated


def _campaign_names(db: Session) -> dict:
    """id -> name for the tenant, so a campaign bucket reads as a claimable
    reference rather than a bare number."""
    rows = db.execute(text("SELECT id, name FROM campaigns WHERE tenant_id = :tid"),
                      {"tid": current_tenant()}).fetchall()
    return {int(r[0]): (r[1] or "") for r in rows}


def _build_evidence(request: Request, db: Session, date_from: str, date_to: str) -> dict:
    ch = request.state.ch
    tenant = current_tenant()
    totals = _evidence_totals(ch, tenant, date_from, date_to)
    names = _campaign_names(db)
    sections, notes = [], []
    if totals["flagged_events"] == 0:
        notes.append("No flagged traffic in the selected range.")
    for key, title, expr in EVIDENCE_DIMENSIONS:
        rows, truncated = _evidence_section_rows(ch, tenant, expr, date_from, date_to)
        if key == "campaign":
            for row in rows:
                cid = row["value"]
                name = names.get(int(cid)) if cid.lstrip("-").isdigit() else None
                row["value"] = f"{name} (#{cid})" if name else f"#{cid}"
        if truncated:
            notes.append(f"{title}: showing the top {EVIDENCE_ROW_LIMIT} groups by hit "
                         "count — the list was cut off.")
        sections.append({"key": key, "title": title, "rows": rows, "truncated": truncated})
    return {
        "title": "Fraud evidence report",
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "date_from": date_from,
        "date_to": date_to,
        "workspace_id": int(tenant),
        "flag_rule": f"is_bot = true OR fraud_score >= {EVIDENCE_SCORE_THRESHOLD}",
        "score_threshold": EVIDENCE_SCORE_THRESHOLD,
        "row_limit": EVIDENCE_ROW_LIMIT,
        "totals": totals,
        "sections": sections,
        "notes": notes,
    }


def _evidence_csv_response(evidence: dict):
    """One flat, UTF-8-BOM CSV with the formula-injection guard from the log
    exports (reused, not reimplemented)."""
    import csv as _csv
    import io as _io
    from fastapi.responses import Response
    from app_pages.logs import _csv_safe

    buf = _io.StringIO()
    writer = _csv.writer(buf, lineterminator="\n")
    writer.writerow(EVIDENCE_CSV_FIELDS)

    def row(cells: dict):
        writer.writerow([_csv_safe(cells.get(f, "")) for f in EVIDENCE_CSV_FIELDS])

    for key, value in (("generated_at", evidence["generated_at"]),
                       ("date_from", evidence["date_from"]),
                       ("date_to", evidence["date_to"]),
                       ("workspace_id", evidence["workspace_id"]),
                       ("flag_rule", evidence["flag_rule"]),
                       ("row_limit", evidence["row_limit"])):
        row({"section": "meta", "key": key, "value": value})
    for key, value in evidence["totals"].items():
        row({"section": "totals", "key": key, "value": value})
    for note in evidence["notes"]:
        row({"section": "note", "key": note})
    for section in evidence["sections"]:
        for r in section["rows"]:
            row({"section": section["key"], "key": r["value"],
                 "hits": r["hits"], "bot_hits": r["bot_hits"],
                 "high_score_hits": r["high_score_hits"],
                 "avg_fraud_score": r["avg_fraud_score"],
                 "max_fraud_score": r["max_fraud_score"], "cost": r["cost"],
                 "first_seen": r["first_seen"], "last_seen": r["last_seen"]})
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             'attachment; filename="fraud-evidence.csv"'})


def _evidence_html_response(evidence: dict):
    """A single self-contained page: inline CSS only, a print button hidden when
    printing, every dynamic value HTML-escaped (UA/sub-id strings are visitor
    input and must never become markup)."""
    import html as _html
    from fastapi.responses import HTMLResponse

    esc = _html.escape
    t = evidence["totals"]

    def money(v):
        return f"${float(v or 0):,.4f}"

    tables = []
    for section in evidence["sections"]:
        if section["rows"]:
            body = "".join(
                "<tr>"
                f"<td class='mono'>{esc(str(r['value']))}</td>"
                f"<td class='num'>{r['hits']:,}</td>"
                f"<td class='num'>{r['bot_hits']:,}</td>"
                f"<td class='num'>{r['high_score_hits']:,}</td>"
                f"<td class='num'>{r['avg_fraud_score']:.1f}</td>"
                f"<td class='num'>{r['max_fraud_score']:,}</td>"
                f"<td class='num'>{money(r['cost'])}</td>"
                f"<td class='mono small'>{esc(r['first_seen'])}</td>"
                f"<td class='mono small'>{esc(r['last_seen'])}</td>"
                f"<td>{esc(r['flag_reasons'])}</td>"
                "</tr>" for r in section["rows"])
        else:
            body = ("<tr><td colspan='10' class='empty'>"
                    "No flagged traffic in this dimension.</td></tr>")
        tables.append(
            f"<h2>{esc(section['title'])}</h2>"
            "<table><thead><tr>"
            "<th>Value</th><th class='num'>Flagged hits</th><th class='num'>Bot hits</th>"
            "<th class='num'>High-score hits</th><th class='num'>Avg score</th>"
            "<th class='num'>Max score</th><th class='num'>Wasted spend</th>"
            "<th>First seen</th><th>Last seen</th><th>Reason</th>"
            "</tr></thead><tbody>" + body + "</tbody></table>")

    notes = "".join(f"<li>{esc(n)}</li>" for n in evidence["notes"])
    notes_html = (f"<div class='note'><strong>Notes</strong><ul>{notes}</ul></div>"
                  if notes else "")
    empty_banner = ("<p class='empty-banner'>No flagged traffic in the selected range.</p>"
                    if t["flagged_events"] == 0 else "")
    totals_items = [
        ("Tracked events", f"{t['tracked_events']:,}"),
        ("Clicks", f"{t['clicks']:,}"),
        ("Flagged events", f"{t['flagged_events']:,}"),
        ("Flagged clicks", f"{t['flagged_clicks']:,}"),
        ("Bot share", f"{t['bot_share_pct']:.2f}%"),
        ("Flagged share", f"{t['flagged_share_pct']:.2f}%"),
        ("Avg fraud score", f"{t['avg_fraud_score']:.2f}"),
        ("Max fraud score", f"{t['max_fraud_score']:,}"),
        ("Total spend", money(t["total_spend"])),
        ("Est. wasted spend", money(t["wasted_spend"])),
    ]
    cards = "".join(f"<div class='card'><div class='k'>{esc(k)}</div>"
                    f"<div class='v'>{v}</div></div>" for k, v in totals_items)

    css = """
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: #1f2937; margin: 24px; }
h1 { font-size: 22px; margin: 0 0 6px; }
h2 { font-size: 15px; margin: 26px 0 8px; }
.sub { color: #6b7280; font-size: 12px; margin-bottom: 3px; }
table { border-collapse: collapse; width: 100%; font-size: 12px; }
th, td { border: 1px solid #e5e7eb; padding: 5px 7px; text-align: left; vertical-align: top; }
th { background: #f3f4f6; }
td.num, th.num { text-align: right; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        word-break: break-all; }
.small { font-size: 11px; color: #6b7280; white-space: nowrap; }
.empty { color: #6b7280; text-align: center; }
.cards { display: flex; flex-wrap: wrap; gap: 10px; margin: 12px 0; }
.card { border: 1px solid #e5e7eb; border-radius: 6px; padding: 8px 12px; min-width: 150px; }
.card .k { font-size: 11px; color: #6b7280; }
.card .v { font-size: 17px; font-weight: 600; margin-top: 2px; }
.note { border: 1px solid #f59e0b; background: #fffbeb; padding: 8px 12px;
        border-radius: 6px; margin: 14px 0; font-size: 12px; }
.note ul { margin: 4px 0 0 18px; padding: 0; }
.empty-banner { border: 1px solid #e5e7eb; background: #f9fafb; padding: 12px;
                border-radius: 6px; font-size: 13px; }
.toolbar { margin-bottom: 16px; }
.toolbar button { padding: 8px 14px; border: 1px solid #2563eb; background: #2563eb;
                  color: #fff; border-radius: 6px; cursor: pointer; font-size: 13px; }
@media print { .toolbar { display: none; } body { margin: 0; }
               h2 { page-break-after: avoid; } tr { page-break-inside: avoid; } }
"""
    header = (
        f"<h1>{esc(evidence['title'])}</h1>"
        f"<div class='sub'>Date range: {esc(evidence['date_from'])} to "
        f"{esc(evidence['date_to'])} (inclusive)</div>"
        f"<div class='sub'>Generated: {esc(evidence['generated_at'])}</div>"
        f"<div class='sub'>Workspace scope: {evidence['workspace_id']}</div>"
        f"<div class='sub'>Flag rule: {esc(evidence['flag_rule'])} &middot; "
        f"score threshold {evidence['score_threshold']} &middot; "
        f"row cap {evidence['row_limit']} per dimension</div>"
        f"<div class='cards'>{cards}</div>")
    return HTMLResponse(
        content=(
            "<!doctype html>\n<html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{esc(evidence['title'])}</title><style>{css}</style></head><body>"
            "<div class='toolbar'><button onclick='window.print()'>Print / Save as PDF"
            "</button></div>"
            + header + empty_banner + notes_html
            + "".join(tables) +
            "</body></html>"),
        media_type="text/html")


@router.get("/evidence.csv")
def fraud_evidence_csv(request: Request, date_from: str = None, date_to: str = None,
                       db: Session = Depends(get_db)):
    start, end = _parse_evidence_range(date_from, date_to)
    return _evidence_csv_response(_build_evidence(request, db, start, end))


@router.get("/evidence.html")
def fraud_evidence_html(request: Request, date_from: str = None, date_to: str = None,
                        db: Session = Depends(get_db)):
    start, end = _parse_evidence_range(date_from, date_to)
    return _evidence_html_response(_build_evidence(request, db, start, end))


@router.get("/bot-lists")
def get_bot_lists(db: Session = Depends(get_db)):
    block = (_load_main_settings(db).get("bot_lists") or {})
    out = {key: [] for key in BOT_LIST_KEYS}
    if isinstance(block, dict):
        for key in BOT_LIST_KEYS:
            val = block.get(key)
            if isinstance(val, list):
                out[key] = [str(v) for v in val]
    return {"bot_lists": out}


@router.put("/bot-lists")
def put_bot_lists(payload: dict, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    block = _validate_bot_lists((payload or {}).get("bot_lists") or payload or {})

    row = db.execute(
        text("SELECT id, value FROM settings "
             "WHERE name = 'settings' AND tenant_id = :tid FOR UPDATE"),
        {"tid": current_tenant()}
    ).fetchone()
    if row:
        try:
            existing = json.loads(row[1]) if row[1] else {}
        except Exception:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing["bot_lists"] = block
        db.execute(text("UPDATE settings SET value = :v WHERE id = :i"),
                   {"v": json.dumps(existing), "i": row[0]})
    else:
        db.add(SettingsORM(name="settings", value=json.dumps({"bot_lists": block})))
    db.commit()

    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "bot_lists", "",
                {k: len(v) for k, v in block.items()},
                request.client.host if request.client else "")
    return {"bot_lists": block}


@router.get("/honeypot-hits")
def honeypot_hits(request: Request, limit: int = 100, db: Session = Depends(get_db)):
    limit = min(max(int(limit or 100), 1), 500)
    rows = db.execute(text(
        "SELECT id, visitor_key, ip, ua, campaign_id, received_at "
        "FROM honeypot_hits WHERE tenant_id = :tid "
        "ORDER BY received_at DESC LIMIT :l"),
        {"l": limit, "tid": current_tenant()}).fetchall()
    return {"hits": [{
        "id": r[0], "visitor_key": r[1], "ip": r[2], "ua": r[3],
        "campaign_id": r[4],
        "received_at": r[5].isoformat() if r[5] else None,
    } for r in rows]}


# ---------------------------------------------------------------------------
# G44 — traffic-quality blacklists (settings block "blacklists")
# ---------------------------------------------------------------------------

BLACKLIST_FIELDS = ("sub_id_1", "sub_id_2", "sub_id_3", "sub_id_4", "sub_id_5",
                    "sub_id_6", "sub_id_7", "sub_id_8", "sub_id_9", "sub_id_10",
                    "country", "city", "device_type", "os", "browser", "ip")
BLACKLIST_ACTIONS = ("mark", "block")
BLACKLIST_SCOPES = ("global", "campaign")
BLACKLIST_MAX_VALUES = 5000


def _validate_blacklist(payload: dict, existing: dict = None) -> dict:
    out = dict(existing or {})
    name = str(payload.get("name", out.get("name") or "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Blacklist name is required")
    out["name"] = name[:255]

    field = payload.get("field", out.get("field"))
    if field not in BLACKLIST_FIELDS:
        raise HTTPException(status_code=400,
                            detail=f"field must be one of: {', '.join(BLACKLIST_FIELDS)}")
    out["field"] = field

    raw_values = payload.get("values", out.get("values"))
    if not isinstance(raw_values, list):
        raise HTTPException(status_code=400, detail="values must be a list")
    values = []
    for v in raw_values:
        v = str(v).strip()
        if v:
            values.append(v[:512])
    if not values:
        raise HTTPException(status_code=400, detail="At least one value is required")
    if len(values) > BLACKLIST_MAX_VALUES:
        raise HTTPException(status_code=400,
                            detail=f"Too many values (max {BLACKLIST_MAX_VALUES})")
    if field == "ip":
        import ipaddress
        for v in values:
            if "/" in v:
                try:
                    ipaddress.ip_network(v, strict=False)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"Invalid CIDR {v!r}: {e}")
            else:
                try:
                    ipaddress.ip_address(v)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"Invalid IP {v!r}: {e}")
    out["values"] = values

    scope = payload.get("scope", out.get("scope") or "global")
    if scope not in BLACKLIST_SCOPES:
        raise HTTPException(status_code=400,
                            detail=f"scope must be one of: {', '.join(BLACKLIST_SCOPES)}")
    out["scope"] = scope
    campaign_id = payload.get("campaign_id", out.get("campaign_id"))
    if scope == "campaign":
        try:
            campaign_id = int(campaign_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400,
                                detail="campaign_id is required for campaign scope")
        exists = db_campaign_exists(campaign_id)
        if not exists:
            raise HTTPException(status_code=400, detail=f"Campaign {campaign_id} not found")
    else:
        campaign_id = None
    out["campaign_id"] = campaign_id

    action = payload.get("action", out.get("action") or "mark")
    if action not in BLACKLIST_ACTIONS:
        raise HTTPException(status_code=400,
                            detail=f"action must be one of: {', '.join(BLACKLIST_ACTIONS)}")
    out["action"] = action

    out["enabled"] = bool(payload.get("enabled", out.get("enabled", True)))
    return out


def db_campaign_exists(campaign_id: int) -> bool:
    from db import SessionLocal
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT 1 FROM campaigns "
                              "WHERE id = :i AND tenant_id = :tid"),
                         {"i": campaign_id, "tid": current_tenant()}).fetchone()
        return row is not None
    finally:
        db.close()


def _save_blacklists_block(db: Session, lists: list) -> None:
    row = db.execute(
        text("SELECT id, value FROM settings "
             "WHERE name = 'settings' AND tenant_id = :tid FOR UPDATE"),
        {"tid": current_tenant()}
    ).fetchone()
    if row:
        try:
            existing = json.loads(row[1]) if row[1] else {}
        except Exception:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing["blacklists"] = lists
        db.execute(text("UPDATE settings SET value = :v WHERE id = :i"),
                   {"v": json.dumps(existing), "i": row[0]})
    else:
        db.add(SettingsORM(name="settings", value=json.dumps({"blacklists": lists})))


def _blacklist_audit(request: Request, action: str, bl: dict) -> None:
    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", action, "blacklist", bl.get("id") or "",
                {"name": bl.get("name"), "field": bl.get("field"),
                 "values": len(bl.get("values") or []), "scope": bl.get("scope"),
                 "campaign_id": bl.get("campaign_id"), "action": bl.get("action"),
                 "enabled": bl.get("enabled")},
                request.client.host if request.client else "")


@router.get("/blacklists")
def get_blacklists(db: Session = Depends(get_db)):
    block = _load_main_settings(db).get("blacklists")
    lists = block if isinstance(block, list) else []
    return {"blacklists": [b for b in lists if isinstance(b, dict)]}


@router.post("/blacklists")
def create_blacklist(payload: dict, request: Request, db: Session = Depends(get_db)):
    bl = _validate_blacklist(payload or {})
    bl["id"] = uuid.uuid4().hex[:12]
    bl["created_at"] = datetime.utcnow().isoformat()

    lists = get_blacklists(db)["blacklists"]
    lists.append(bl)
    _save_blacklists_block(db, lists)
    db.commit()
    _blacklist_audit(request, "create", bl)
    return bl


@router.put("/blacklists/{bl_id}")
def update_blacklist(bl_id: str, payload: dict, request: Request,
                     db: Session = Depends(get_db)):
    lists = get_blacklists(db)["blacklists"]
    for i, bl in enumerate(lists):
        if bl.get("id") == bl_id:
            updated = _validate_blacklist(payload or {}, existing=bl)
            updated["id"] = bl_id
            updated["created_at"] = bl.get("created_at")
            lists[i] = updated
            _save_blacklists_block(db, lists)
            db.commit()
            _blacklist_audit(request, "update", updated)
            return updated
    raise HTTPException(status_code=404, detail="Blacklist not found")


@router.delete("/blacklists/{bl_id}")
def delete_blacklist(bl_id: str, request: Request, db: Session = Depends(get_db)):
    lists = get_blacklists(db)["blacklists"]
    remaining = [b for b in lists if b.get("id") != bl_id]
    if len(remaining) == len(lists):
        raise HTTPException(status_code=404, detail="Blacklist not found")
    _save_blacklists_block(db, remaining)
    db.commit()
    _blacklist_audit(request, "delete", {"id": bl_id})
    return {"status": "deleted", "id": bl_id}
