from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, or_, and_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import or_
from sqlalchemy import text
from sqlalchemy import func, case
from db import get_db
from tenant_context import current_tenant
from auth import (request_hidden_metrics, strip_hidden_metrics,
                  strip_hidden_metrics_rows, filter_hidden_fields)
from models.base import Base, TenantMixin
from models.settings import SettingsORM
from models.campaigns import CampaignORM

import csv
import io
import json
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from clickHouse import normalize_attribution, ATTRIBUTION_DEFAULTS
from app_pages import rates as fx_rates

router = APIRouter()

class Conversion(TenantMixin, Base):
    __tablename__ = "conversions_data"

    id = Column(Integer, primary_key=True, index=True)
    received_at = Column(DateTime)
    click_id = Column(String)
    campaign_id = Column(Integer)
    offer_id = Column(Integer)
    landing_id = Column(Integer)
    status = Column(String)
    external_id = Column(String)
    payout = Column(Float)
    revenue = Column(Float)
    profit = Column(Float)
    currency = Column(String)
    transaction_id = Column(String)
    country = Column(String)
    region = Column(String)
    city = Column(String)
    ip = Column(String)
    visitor_id = Column(String)
    sub_id_1 = Column(String)
    sub_id_2 = Column(String)
    sub_id_3 = Column(String)
    sub_id_4 = Column(String)
    sub_id_5 = Column(String)
    sub_id_6 = Column(String)
    sub_id_7 = Column(String)
    sub_id_8 = Column(String)
    sub_id_9 = Column(String)
    sub_id_10 = Column(String)
    utm_campaign = Column(String)
    utm_creative = Column(String)
    utm_source = Column(String)
    traffic_source_name = Column(String)
    os = Column(String)
    isp = Column(String)
    is_using_proxy = Column(Boolean)
    is_bot = Column(Boolean)
    device_type = Column(String)
    postback_count = Column(Integer)
    last_postback_at = Column(DateTime)
    approval = Column(String)
    is_duplicate = Column(Boolean)
    funnel_step = Column(Integer)
    events = Column(JSONB)

VALID_STATUSES = {"lead", "sale", "upsale", "rejected", "hold", "trash"}

# Wave 19B — reconciliation lifecycle for networks that approve conversions.
VALID_APPROVALS = {"pending", "approved", "declined", "other"}

# Upper bound on the visitor ids pulled from ClickHouse for click_date
# attribution. Above this the id set is truncated (with a loud log) so a busy
# window can never turn the conversions list/export into a giant Postgres
# IN(...) that exhausts memory.
CONVERSION_CLICK_WINDOW_ID_CAP = 50_000

# Attribution config (G28), stored per workspace under settings.attribution.
# Read through a short TTL cache so a conversion list/export (and the bulk
# ingestion path) does not hit the settings row once per conversion. The
# tracking plane (frontend/app.py) keeps its own reader of the same block.
_ATTRIBUTION_CACHE_TTL = 30.0
_attribution_cache: dict = {}


def _attribution_config(db: Session) -> dict:
    """This workspace's validated attribution config (30s TTL cache).

    Falls back to the defaults (today's behaviour) if the row is missing or
    unreadable — a bad read must never change how conversions are attributed."""
    tid = int(current_tenant() or 1)
    now = time.monotonic()
    hit = _attribution_cache.get(tid)
    if hit is not None and now - hit[0] < _ATTRIBUTION_CACHE_TTL:
        return hit[1]
    cfg = dict(ATTRIBUTION_DEFAULTS)
    try:
        row = db.query(SettingsORM).filter_by(name="settings").first()
        if row and row.value:
            cfg = normalize_attribution(json.loads(row.value).get("attribution"))
    except Exception:
        return cfg
    _attribution_cache[tid] = (now, cfg)
    return cfg


def _escape_like(term: str) -> str:
    """Escape SQL LIKE wildcards so user text matches literally (Postgres's
    default escape character is the backslash)."""
    return str(term).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_iso_dt(value, label: str) -> datetime:
    """Validate an ISO date/datetime query param — a bad value used to 500 in
    fromisoformat downstream."""
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {label}: expected an ISO date (YYYY-MM-DD)")


def _conversions_scope(request: Request, db: Session, query):
    """G63/D1c parity with campaigns:'own' — a scoped caller only sees
    conversions whose campaign they own; unattributable rows (NULL campaign_id)
    are hidden from them. Admins and regular users pass through."""
    from auth import get_caller, membership_for
    username, is_admin = get_caller(request)
    if is_admin or not username:
        return query
    member = membership_for(db, username)
    raw = member[2] if member else {}
    if raw.get("campaigns") != "own" or not member:
        return query
    owned = db.query(CampaignORM.id).filter(CampaignORM.owner_id == member[0])
    return query.filter(Conversion.campaign_id.in_(owned))


def _require_conversion_access(request: Request, db: Session, conv: "Conversion") -> None:
    """Mutation counterpart of _conversions_scope: scoped users may only touch
    conversions on campaigns they own (unattributable rows are off-limits)."""
    from auth import get_caller, membership_for
    username, is_admin = get_caller(request)
    if is_admin or not username:
        return
    member = membership_for(db, username)
    raw = member[2] if member else {}
    if raw.get("campaigns") != "own" or not member:
        return
    if conv.campaign_id is None:
        raise HTTPException(status_code=403,
                            detail="You can only modify conversions of your own campaigns")
    owner_id = db.query(CampaignORM.owner_id).filter_by(id=conv.campaign_id).scalar()
    if owner_id != member[0]:
        raise HTTPException(status_code=403,
                            detail="You can only modify conversions of your own campaigns")


def normalize_status(status: str) -> str:
    """Slugify a status to lowercase_underscore (case/space tolerant)."""
    return re.sub(r"[^a-z0-9]+", "_", str(status or "").strip().lower()).strip("_")


def _parse_occurred_at(value) -> Optional[datetime]:
    """Parse an ISO-8601 conversion timestamp into a naive UTC datetime, or
    None when it is not parseable. A trailing Z is normalized for Python
    versions whose fromisoformat predates that shorthand; an aware timestamp is
    converted to UTC so it can be stored in the naive DateTime column."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(tz=timezone.utc).replace(tzinfo=None)
    return parsed


def all_valid_statuses(db: Session) -> set:
    """Built-ins plus custom statuses configured in the settings row."""
    statuses = set(VALID_STATUSES)
    row = db.query(SettingsORM).filter_by(name="settings").first()
    if row and row.value:
        try:
            cfg = json.loads(row.value)
            for s in cfg.get("custom_statuses") or []:
                slug = normalize_status(s.get("name") if isinstance(s, dict) else s)
                if slug:
                    statuses.add(slug)
        except Exception:
            pass
    return statuses


@router.get("/")
def get_conversions(request: Request, limit: int = 100, offset: Optional[int] = None,
                    db: Session = Depends(get_db)):
    query = _build_conversions_query(request, db)
    total = None
    if offset is not None:
        # Paginated mode: `offset` opts in so legacy callers keep the bare-list
        # shape; total comes from count(), never len(items).
        total = query.count()
        offset = max(int(offset or 0), 0)
    rows = (query.order_by(Conversion.received_at.desc(), Conversion.id.desc())
                 .limit(min(max(int(limit or 50), 1), 5000)))
    if offset is not None:
        rows = rows.offset(offset)
    # One FX store read for the whole page: each row's money is converted from
    # its own currency to the workspace base currency.
    store = fx_rates.load_store(db)
    items = [_conversion_to_dict(row, store) for row in rows.all()]
    hidden = request_hidden_metrics(request, db)
    strip_hidden_metrics_rows(items, hidden)
    if total is not None:
        out = {"items": items, "total": total, "limit": limit, "offset": offset}
        meta = fx_rates.fx_meta(store)
        if meta:
            out["fx"] = meta
        truncation = _attribution_truncation(request)
        if truncation:
            out.update(truncation)
        return out
    return items


def _conversion_to_dict(conv: "Conversion", store: Optional[dict] = None) -> dict:
    """Serialize one conversion row, adding ``dedupe_token`` — the
    transaction/external id the dedupe guard matches on (derived, no column).

    ``payout``/``revenue``/``profit`` are reported in the workspace base
    currency: each row is converted from its own ``currency`` (a blank currency
    is already base). No store => values unchanged."""
    item = dict(conv.__dict__)
    item.pop("_sa_instance_state", None)
    item["dedupe_token"] = conv.transaction_id or conv.external_id
    if store and fx_rates.store_active(store):
        fx_rates.convert_money(item, conv.currency, store)
    return item


CONVERSION_EXPORT_FIELDS = ["id", "received_at", "click_id", "campaign_id", "offer_id",
                            "landing_id", "status", "approval", "external_id",
                            "transaction_id", "visitor_id", "country", "payout", "revenue",
                            "profit", "currency", "postback_count"]


@router.get("/export")
def export_conversions(request: Request, db: Session = Depends(get_db)):
    """CSV export of the conversion log with the same filters as the list."""
    from fastapi.responses import Response
    query = _build_conversions_query(request, db)
    rows = query.order_by(Conversion.received_at.desc(), Conversion.id.desc()).limit(50000).all()
    # Header and rows drop the same hidden metric columns together (revenue
    # also covers its payout alias).
    fields = filter_hidden_fields(
        CONVERSION_EXPORT_FIELDS, request_hidden_metrics(request, db))
    store = fx_rates.load_store(db)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(fields)
    for conv in rows:
        writer.writerow([_csv_safe(_conversion_export_value(conv, k, store))
                         for k in fields])
    headers = {"Content-Disposition": "attachment; filename=conversions.csv"}
    if _attribution_truncation(request):
        headers["X-Attribution-Truncated"] = "true"
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv",
                    headers=headers)


def _conversion_export_value(conv: "Conversion", key: str, store: Optional[dict]):
    """One export cell, with payout/revenue/profit converted to base currency."""
    value = getattr(conv, key, None)
    if key in ("payout", "revenue", "profit") and store:
        value = fx_rates.convert(value, conv.currency, store)
    return value


def _csv_safe(value):
    """Prefix cells that would start a spreadsheet formula (=,+,-,@) with an
    apostrophe so exported CSVs can't smuggle live formulas into Excel/Sheets."""
    s = "" if value is None else (value if isinstance(value, str) else str(value))
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def _build_conversions_query(request: Request, db: Session):
    """Full filtered conversions query shared by the list and CSV export."""
    # fields allowed for filtering
    ALLOWED_FILTER_FIELDS = {
        "campaign_id", "offer_id", "landing_id", "status", "approval", "click_id",
        "external_id",
        "sub_id_1", "sub_id_2", "sub_id_3", "sub_id_4", "sub_id_5",
        "sub_id_6", "sub_id_7", "sub_id_8", "sub_id_9", "sub_id_10",
        "utm_source", "utm_campaign", "utm_creative", "traffic_source_name"
    }

    params = dict(request.query_params)
    # G55: attribution basis — click_date (default): conversions attributed to the
    # click's received_at; conversion_date: counted on the conversion received_at.
    # The join key is the first-party visitor cookie (aaa_vid): the tracking
    # plane stamps it on both the ClickHouse click row and the Postgres
    # conversion row. visitor_id is NOT the conversion's click_id — joining on
    # that pair always yields empty.
    date_basis = params.get("date_basis") or "click_date"
    date_from = params.get("date_from")
    date_to = params.get("date_to")
    if date_from and date_to:
        # validate once up front — these flow into ClickHouse toDate() and
        # Postgres fromisoformat() below
        _parse_iso_dt(date_from, "date_from")
        _parse_iso_dt(date_to, "date_to")

    # Attribution model/window for this workspace. Defaults (last_click, 7 days)
    # leave the query below byte-identical to before; first_click selects the
    # visitor's EARLIEST click instead. A linear/position-based model is not
    # offered: it needs the visitor's full ordered touch chain, which we do not
    # store (clicks_data has rows, not a per-visitor chain).
    attr_cfg = _attribution_config(db)
    first_click = attr_cfg.get("model") == "first_click"
    attr_window_days = int(attr_cfg.get("window_days") or ATTRIBUTION_DEFAULTS["window_days"])

    visitor_ids_in_window = None
    if date_from and date_to and date_basis == "click_date":
        ch = request.state.ch
        # G63 click_date basis: attribute each conversion to the date of the
        # CLICK it came from (visitor cookie join). The click window lives in
        # ClickHouse, conversions in Postgres, so we collect the window's
        # visitor ids and filter the PG side with an IN(...) — an unbounded set
        # here used to build a millions-element query (OOM footgun) on every
        # list page and CSV export. We keep the correct join semantics (a
        # conversion may land days after its click and must still count on the
        # click's date) but bound the id set to CONVERSION_CLICK_WINDOW_ID_CAP
        # and degrade loudly rather than silently, instead of pushing the
        # conversion's own received_at window onto the PG side — that would
        # hide exactly the late-landing conversions click_date is meant to keep.
        try:
            if first_click:
                # First click: a visitor is in-window when their EARLIEST click
                # counted in the report's date range. The attribution window
                # bounds how far before the range we look for that earliest
                # click, so a scan can never reach back unboundedly.
                res = ch.query(
                    "SELECT visitor_id FROM clicks_data "
                    "WHERE tenant_id = %(tenant_id)s "
                    "AND visitor_id != '' "
                    f"AND received_at >= toDate(%(df)s) - INTERVAL {int(attr_window_days)} DAY "
                    "AND received_at < toDate(%(dt)s) + INTERVAL 1 DAY "
                    "GROUP BY visitor_id "
                    "HAVING toDate(min(received_at)) BETWEEN toDate(%(df)s) AND toDate(%(dt)s) "
                    "LIMIT %(cap)s",
                    parameters={"df": date_from, "dt": date_to,
                                "tenant_id": current_tenant(),
                                "cap": CONVERSION_CLICK_WINDOW_ID_CAP + 1},
                )
            else:
                res = ch.query(
                    "SELECT DISTINCT visitor_id FROM clicks_data "
                    "WHERE tenant_id = %(tenant_id)s "
                    "AND toDate(received_at) BETWEEN toDate(%(df)s) AND toDate(%(dt)s) "
                    "AND visitor_id != '' "
                    "LIMIT %(cap)s",
                    parameters={"df": date_from, "dt": date_to,
                                "tenant_id": current_tenant(),
                                "cap": CONVERSION_CLICK_WINDOW_ID_CAP + 1},
                )
            ids = [row[0] for row in res.result_rows if row and row[0]]
            if len(ids) > CONVERSION_CLICK_WINDOW_ID_CAP:
                warning = (f"Click window {date_from}..{date_to} has more than "
                           f"{CONVERSION_CLICK_WINDOW_ID_CAP} visitor ids; "
                           f"click_date attribution was truncated — narrow the "
                           f"date range for exact numbers")
                print(f"CONVERSIONS CLICK WINDOW CAPPED: {warning}")
                ids = ids[:CONVERSION_CLICK_WINDOW_ID_CAP]
                # Surface the truncation to the caller (flag on the list/summary
                # payload, header on the CSV export) instead of returning
                # silently-partial numbers.
                request.state.attribution_truncated = True
                request.state.attribution_warning = warning
            visitor_ids_in_window = set(ids)
        except Exception as e:
            # Never silently empty the list: log loudly and fall back to the
            # legacy own-date window for NULL-visitor rows only.
            print("CONVERSIONS CLICK WINDOW ERROR: ClickHouse visitor-id lookup "
                  f"failed ({e!r}) — falling back to conversion-date window for "
                  f"{date_from}..{date_to}")
            visitor_ids_in_window = set()

    query = db.query(Conversion)
    query = _conversions_scope(request, db, query)

    # Apply the filters
    for key, value in request.query_params.items():
        if key in ALLOWED_FILTER_FIELDS:
            column = getattr(Conversion, key, None)
            if column is not None:
                query = query.filter(column == value)
        elif key in ("date_from", "date_to", "date_basis", "limit", "offset"):
            continue
        elif key == "search":
            # free-text search across the identifier columns
            term = f"%{_escape_like(value)}%"
            query = query.filter(
                (Conversion.click_id.ilike(term)) |
                (Conversion.external_id.ilike(term)) |
                (Conversion.transaction_id.ilike(term)) |
                (Conversion.visitor_id.ilike(term))
            )
        elif key == "url":
            # substring match against the bound offer's URL — conversion rows
            # carry no URL column themselves
            from models.offers import OfferORM
            term = f"%{_escape_like(value)}%"
            query = query.join(OfferORM, Conversion.offer_id == OfferORM.id) \
                         .filter(OfferORM.url.ilike(term))

    if date_from and date_to:
        if date_basis == "conversion_date":
            query = query.filter(Conversion.received_at >= _parse_iso_dt(date_from, "date_from"))
            query = query.filter(
                Conversion.received_at < _parse_iso_dt(date_to, "date_to") + timedelta(days=1))
        elif visitor_ids_in_window is not None:
            # Linked conversions: window by their CLICK date (visitor cookie).
            # Legacy rows written before visitor attribution have no link —
            # fall back to their own conversion date for the window so the log
            # doesn't silently empty out on reload.
            window_start = _parse_iso_dt(date_from, "date_from")
            window_end = _parse_iso_dt(date_to, "date_to") + timedelta(days=1)
            query = query.filter(or_(
                Conversion.visitor_id.in_(visitor_ids_in_window or ["__none__"]),
                and_(Conversion.visitor_id.is_(None),
                     Conversion.received_at >= window_start,
                     Conversion.received_at < window_end),
            ))

    return query


def _attribution_truncation(request: Request) -> Optional[dict]:
    """Response fields describing a capped click_date visitor-id lookup.

    ``_build_conversions_query`` sets the flag on ``request.state`` when the
    ClickHouse id lookup hit CONVERSION_CLICK_WINDOW_ID_CAP; consumers merge
    these fields so a client can distinguish exact from partial attribution.
    Returns None on the normal (untruncated) path."""
    if getattr(request.state, "attribution_truncated", False):
        return {"truncated": True,
                "warning": getattr(request.state, "attribution_warning",
                                   "click_date attribution was truncated")}
    return None


@router.get("/funnel/{campaign_id}")
def get_funnel_report(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    """G10 funnel report: per-step visits/click-outs/conversions for a funnel
    campaign. Visits come from ClickHouse (flow_index IS the step index for
    funnel campaigns); conversion-side aggregates from conversions_data
    (funnel_step = the step at click-out time). Non-funnel campaigns 404."""
    row = db.execute(
        text("SELECT id, name, config, owner_id FROM campaigns "
             "WHERE id = :cid AND tenant_id = :tid"),
        {"cid": campaign_id, "tid": current_tenant()}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # campaigns:'own' parity with the list/export: a scoped caller only reads
    # funnels of campaigns they own (404, since it's invisible to them).
    from auth import get_caller, membership_for
    username, is_admin = get_caller(request)
    if not is_admin and username:
        member = membership_for(db, username)
        if member and (member[2] or {}).get("campaigns") == "own" \
                and row["owner_id"] != member[0]:
            raise HTTPException(status_code=404, detail="Campaign not found")

    raw_config = row["config"]
    if isinstance(raw_config, dict):
        config = raw_config
    else:
        try:
            config = json.loads(raw_config or "{}")
        except (TypeError, json.JSONDecodeError):
            config = {}
    funnel = config.get("funnel") or {}
    steps_cfg = [s for s in (funnel.get("steps") or []) if isinstance(s, dict)]
    if not funnel.get("enabled") or not steps_cfg:
        raise HTTPException(status_code=404, detail="Campaign has no active funnel")

    # Visits per step — funnel campaigns stamp the step into flow_index.
    visits_by_step: dict = {}
    ch = request.state.ch
    try:
        res = ch.query(
            "SELECT flow_index, count() FROM clicks_data "
            "WHERE campaign_id = %(cid)s AND tenant_id = %(tenant_id)s "
            "GROUP BY flow_index",
            parameters={"cid": campaign_id, "tenant_id": current_tenant()})
        visits_by_step = {int(r[0]): int(r[1]) for r in res.result_rows}
    except Exception:
        visits_by_step = {}

    # Click-out rows per step. Every conversions_data row originates from a
    # click-out at /c, so COUNT(*) is the click-out count (status was 'lead'
    # at insert time; a postback may later change it, the row still counts).
    # Conversions = rows whose current status is not rejected/trash.
    fx_store = fx_rates.load_store(db)
    fx_factor = fx_rates.sql_factor(fx_store)
    rev_expr = f"revenue * ({fx_factor})" if fx_factor else "revenue"
    profit_expr = f"profit * ({fx_factor})" if fx_factor else "profit"
    agg_rows = db.execute(text(f"""
        SELECT funnel_step,
               COUNT(*) AS clickouts,
               COUNT(*) FILTER (WHERE status NOT IN ('rejected', 'trash')) AS conversions,
               COALESCE(SUM({rev_expr}) FILTER (WHERE status NOT IN ('rejected', 'trash')), 0) AS revenue,
               COALESCE(SUM({profit_expr})  FILTER (WHERE status NOT IN ('rejected', 'trash')), 0) AS profit
        FROM conversions_data
        WHERE tenant_id = :tid AND campaign_id = :cid AND funnel_step IS NOT NULL
        GROUP BY funnel_step
    """), {"cid": campaign_id, "tid": current_tenant()}).mappings().all()
    agg_by_step = {int(r["funnel_step"]): r for r in agg_rows}

    steps_out = []
    hidden = request_hidden_metrics(request, db)
    cumulative_conversions = 0
    prev_visits = None
    for i, step in enumerate(steps_cfg):
        visits = visits_by_step.get(i, 0)
        agg = agg_by_step.get(i, {})
        clickouts = int(agg.get("clickouts") or 0)
        conversions = int(agg.get("conversions") or 0)
        cumulative_conversions += conversions
        step_out = {
            "step": i,
            "name": step.get("name") or f"Step {i + 1}",
            "landing_id": step.get("landing"),
            "offers": step.get("offers") or [],
            "schema": step.get("schema") or "landing_offer",
            "visits": visits,
            "clickouts": clickouts,
            "conversions": conversions,
            "revenue": float(agg.get("revenue") or 0),
            "profit": float(agg.get("profit") or 0),
            "cr": (clickouts / visits) if visits else 0,
            "cumulative_conversions": cumulative_conversions,
            "cumulative_cr": (cumulative_conversions / visits_by_step.get(0, 0))
                             if visits_by_step.get(0) else 0,
            "drop_off_pct": None if prev_visits in (None, 0)
                            else round(100 * (prev_visits - visits) / prev_visits, 2),
        }
        steps_out.append(strip_hidden_metrics(step_out, hidden))
        prev_visits = visits

    out = {"campaign_id": campaign_id, "campaign_name": row["name"],
           "funnel": {"enabled": True}, "steps": steps_out}
    meta = fx_rates.fx_meta(fx_store)
    if meta:
        out["fx"] = meta
    return out


class ConversionImport(BaseModel):
    lines: str


def _event_entry(status: str, payout: float, source: str,
                 extra: Optional[dict] = None) -> dict:
    """One conversion event. ``extra`` carries inbound metadata that has no
    column of its own (phone, note) so an ingestion payload is not silently
    dropped — it rides the existing events JSONB history instead."""
    entry = {"status": status, "payout": payout,
             "received_at": datetime.utcnow().isoformat(), "source": source}
    if extra:
        entry.update({k: v for k, v in extra.items() if v not in (None, "")})
    return entry


def _append_event(conv: Conversion, status: str, payout: float, source: str,
                  extra: Optional[dict] = None) -> None:
    """Accumulate a conversion event onto a row (LTV semantics, mirrors the engine)."""
    events = list(conv.events) if isinstance(conv.events, list) else []
    events.append(_event_entry(status, payout, source, extra))
    conv.events = events  # new list object — JSONB needs reassignment to be flagged dirty
    conv.status = status
    conv.payout = float(conv.payout or 0) + payout
    conv.revenue = float(conv.revenue or 0) + payout
    conv.profit = conv.revenue  # conversions_data has no cost column
    conv.postback_count = (conv.postback_count or 0) + 1
    conv.last_postback_at = datetime.utcnow()


@router.post("/import")
def import_conversions(data: ConversionImport, request: Request,
                       db: Session = Depends(get_db)):
    """Manual conversion import (G25): one conversion per CSV line
    'click_id,payout,transaction_id,status'. Existing rows accumulate per the
    LTV semantics; unknown click ids create unattributed rows (click_id 'none').
    Returns a per-line result list — never fails the whole batch."""
    from audit_logger import audit_event
    from auth import get_caller
    statuses = all_valid_statuses(db)
    results = []
    rows = [r for r in csv.reader(io.StringIO(data.lines or ""))
            if r and any(str(c).strip() for c in r)]

    for i, cells in enumerate(rows, 1):
        cells = [str(c).strip() for c in cells]
        # tolerate a header row
        if i == 1 and cells[0].lower() in ("subid", "click_id", "clickid"):
            continue
        if len(cells) < 2 or not cells[0]:
            results.append({"line": i, "ok": False,
                            "detail": "malformed line (need at least click_id,payout)"})
            continue
        subid, payout_raw = cells[0], cells[1]
        tid = cells[2] if len(cells) > 2 and cells[2] else None
        status = normalize_status(cells[3]) if len(cells) > 3 and cells[3] else "sale"
        # Optional columns 5/6: the manual path accepts the same occurred_at /
        # currency the automated ingestion endpoint does, so both routes store
        # the same thing. Absent columns keep the old behavior (now / NULL).
        occurred_raw = cells[4] if len(cells) > 4 and cells[4] else ""
        currency = cells[5] if len(cells) > 5 and cells[5] else None
        try:
            payout = float(payout_raw.strip().replace(",", "."))
        except ValueError:
            results.append({"line": i, "ok": False,
                            "detail": f"invalid payout '{payout_raw}'"})
            continue
        if status not in statuses:
            results.append({"line": i, "ok": False,
                            "detail": f"invalid status '{cells[3] if len(cells) > 3 else ''}'"})
            continue
        occurred_at = None
        if occurred_raw:
            occurred_at = _parse_occurred_at(occurred_raw)
            if occurred_at is None:
                results.append({"line": i, "ok": False,
                                "detail": f"invalid occurred_at '{occurred_raw}'"})
                continue

        conv = db.query(Conversion).filter(or_(
            Conversion.click_id == subid,
            Conversion.external_id == subid,
            Conversion.transaction_id == subid,
        )).first()
        if conv:
            _append_event(conv, status, payout, source="import")
            # This row was matched by the dedupe lookup and accumulated onto
            # instead of inserting a new one — the dedupe path.
            conv.is_duplicate = True
            if tid:
                conv.transaction_id = tid
            if currency:
                conv.currency = currency
            db.add(conv)
            results.append({"line": i, "ok": True, "detail": f"updated conversion {conv.id}"})
        else:
            now = occurred_at or datetime.utcnow()
            conv = Conversion(
                click_id="none", status=status, payout=payout, revenue=payout, profit=payout,
                external_id=tid, postback_count=1, currency=currency,
                approval="pending", is_duplicate=False,
                received_at=now, last_postback_at=now,
                events=[{"status": status, "payout": payout,
                         "received_at": now.isoformat(), "source": "import"}])
            db.add(conv)
            db.flush()
            results.append({"line": i, "ok": True,
                            "detail": f"created unattributed conversion {conv.id}"})

    db.commit()
    imported = sum(1 for r in results if r["ok"])
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "import", "conversions", "bulk",
                {"imported": imported, "failed": len(results) - imported},
                request.client.host if request.client else "")
    return {"results": results, "imported": imported, "failed": len(results) - imported}


class ConversionUpdate(BaseModel):
    status: Optional[str] = None
    approval: Optional[str] = None
    payout: Optional[float] = None
    revenue: Optional[float] = None
    external_id: Optional[str] = None
    transaction_id: Optional[str] = None


def normalize_approval(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


@router.patch("/{conversion_id}")
def update_conversion(conversion_id: int, data: ConversionUpdate,
                      request: Request, db: Session = Depends(get_db)):
    conv = db.query(Conversion).filter_by(id=conversion_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversion not found")
    _require_conversion_access(request, db, conv)

    updates = data.dict(exclude_none=True)
    if "status" in updates:
        status = normalize_status(updates["status"])
        if status not in all_valid_statuses(db):
            raise HTTPException(status_code=400, detail=f"Invalid status. Allowed: {', '.join(sorted(all_valid_statuses(db)))}")
        conv.status = status
    if "approval" in updates:
        approval = normalize_approval(updates["approval"])
        if approval not in VALID_APPROVALS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid approval. Allowed: {', '.join(sorted(VALID_APPROVALS))}")
        conv.approval = approval
    # Payout drives revenue (conversions carry no cost → profit = revenue),
    # consistent with record_conversion's accumulate semantics.
    if "payout" in updates and "revenue" not in updates:
        updates["revenue"] = updates["payout"]
    for field in ("payout", "revenue"):
        if field in updates:
            setattr(conv, field, updates[field])
    for field in ("external_id", "transaction_id"):
        if field in updates:
            setattr(conv, field, updates[field])
    if "payout" in updates or "revenue" in updates:
        conv.profit = float(conv.revenue or 0)

    db.commit()
    return {"message": "Conversion updated"}


class BulkConversionIds(BaseModel):
    ids: List[int]
    approval: Optional[str] = None
    status: Optional[str] = None


def _resolve_conversions(db: Session, ids: List[int]) -> List["Conversion"]:
    clean = [int(i) for i in (ids or [])]
    if not clean:
        return []
    return db.query(Conversion).filter(Conversion.id.in_(clean)).all()


@router.post("/bulk-approval")
def bulk_set_approval(data: BulkConversionIds, request: Request, db: Session = Depends(get_db)):
    """Bulk-set the reconciliation approval on the given conversion ids."""
    from audit_logger import audit_event
    from auth import get_caller
    approval = normalize_approval(data.approval)
    if approval not in VALID_APPROVALS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid approval. Allowed: {', '.join(sorted(VALID_APPROVALS))}")
    rows = _resolve_conversions(db, data.ids)
    applied = []
    for conv in rows:
        _require_conversion_access(request, db, conv)
        conv.approval = approval
        applied.append(conv.id)
    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "conversions", "bulk",
                {"action": "approval", "approval": approval, "ids": applied},
                request.client.host if request.client else "")
    return {"message": f"Approval set to {approval}", "updated": len(applied), "ids": applied}


@router.post("/bulk-status")
def bulk_set_status(data: BulkConversionIds, request: Request, db: Session = Depends(get_db)):
    """Bulk-set the conversion status on the given ids (same validation as the
    single-conversion PATCH: built-ins plus configured custom statuses)."""
    from audit_logger import audit_event
    from auth import get_caller
    status = normalize_status(data.status)
    valid = all_valid_statuses(db)
    if status not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Allowed: {', '.join(sorted(valid))}")
    rows = _resolve_conversions(db, data.ids)
    applied = []
    for conv in rows:
        _require_conversion_access(request, db, conv)
        conv.status = status
        applied.append(conv.id)
    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "conversions", "bulk",
                {"action": "status", "status": status, "ids": applied},
                request.client.host if request.client else "")
    return {"message": f"Status set to {status}", "updated": len(applied), "ids": applied}


class ConversionCreate(BaseModel):
    """Operator-entered offline conversion (manual add — no CSV)."""
    status: Optional[str] = None
    approval: Optional[str] = None
    payout: Optional[float] = None
    revenue: Optional[float] = None
    click_id: Optional[str] = None
    external_id: Optional[str] = None
    transaction_id: Optional[str] = None
    sub_ids: Optional[dict] = None


def _write_conversion(db: Session, *, source: str, status: str, approval: str,
                      payout: float, revenue: Optional[float],
                      click_id: Optional[str] = None,
                      external_id: Optional[str] = None,
                      transaction_id: Optional[str] = None,
                      sub_fields: Optional[dict] = None,
                      occurred_at: Optional[datetime] = None,
                      currency: Optional[str] = None,
                      match_sub_ids: bool = False,
                      apply_approval: bool = False,
                      event_extra: Optional[dict] = None):
    """The one conversion writer. Matches an existing row in the documented
    order — click_id → external_id → transaction_id — then (when
    ``match_sub_ids`` is set, i.e. the inbound ingestion path) falls back to a
    unique sub_id_1..5 click within the last 7 days, mirroring the tracking
    plane's record_conversion. A match accumulates LTV-style via _append_event;
    no match inserts an unattributed ``click_id='none'`` row rather than
    dropping the conversion.

    Returns ``(conv, created, attributed)``.
    """
    sub_fields = dict(sub_fields or {})
    click_id = str(click_id or "").strip()
    external_id = str(external_id or "").strip() or None
    transaction_id = str(transaction_id or "").strip() or None

    match_conds = []
    if click_id:
        match_conds.append(Conversion.click_id == click_id)
    if external_id:
        match_conds.append(Conversion.external_id == external_id)
    if transaction_id:
        match_conds.append(Conversion.transaction_id == transaction_id)
    conv = None
    if match_conds:
        conv = db.query(Conversion).filter(or_(*match_conds)).first()

    if conv is None and match_sub_ids and sub_fields:
        # Clickless attribution fallback: the sub ids (1..5) uniquely matching
        # one real click inside the workspace's attribution window (default 7
        # days) attach this conversion to that click, so an offline sale with no
        # click id still lands on the row it belongs to. Ambiguous (2+
        # candidates) or absent stays unattributed — uniqueness is what keeps
        # two different customers from being merged by a shared sub id.
        subs = {k: v for k, v in sub_fields.items()
                if re.fullmatch(r"sub_id_[1-5]", k) and v}
        if subs:
            attr_cfg = _attribution_config(db)
            try:
                window_days = int(attr_cfg.get("window_days")
                                  or ATTRIBUTION_DEFAULTS["window_days"])
            except (TypeError, ValueError):
                window_days = ATTRIBUTION_DEFAULTS["window_days"]
            cutoff = datetime.utcnow() - timedelta(days=window_days)
            # first_click would prefer the earliest match, but only a unique
            # match is ever attached, so this ordering documents intent without
            # weakening the no-merge guarantee.
            order = (Conversion.received_at.asc()
                     if attr_cfg.get("model") == "first_click"
                     else Conversion.received_at.desc())
            candidates = db.query(Conversion).filter(
                Conversion.click_id.isnot(None),
                Conversion.click_id != "none",
                Conversion.received_at >= cutoff,
                *[getattr(Conversion, k) == v for k, v in subs.items()]
            ).order_by(order).limit(2).all()
            if len(candidates) == 1:
                conv = candidates[0]
                if not click_id:
                    click_id = conv.click_id or ""

    now = occurred_at or datetime.utcnow()
    attributed = bool(click_id and click_id.lower() not in ("none", "0"))
    if conv:
        _append_event(conv, status, payout, source=source, extra=event_extra)
        conv.is_duplicate = True
        if transaction_id:
            conv.transaction_id = transaction_id
        if external_id:
            conv.external_id = external_id
        if revenue is not None:
            conv.revenue = revenue
            conv.profit = revenue
        if apply_approval:
            conv.approval = approval
        if currency:
            conv.currency = currency
        for k, v in sub_fields.items():
            setattr(conv, k, v)
        created = False
        attributed = attributed or bool(
            conv.click_id and str(conv.click_id).lower() not in ("none", "0"))
    else:
        conv = Conversion(
            click_id=click_id or "none", status=status, approval=approval,
            is_duplicate=False,
            payout=payout, revenue=revenue if revenue is not None else payout,
            profit=revenue if revenue is not None else payout,
            external_id=external_id, transaction_id=transaction_id,
            currency=currency, postback_count=1,
            received_at=now, last_postback_at=now,
            events=[_event_entry(status, payout, source, event_extra)],
            **sub_fields)
        db.add(conv)
        db.flush()
        created = True
    return conv, created, attributed


@router.post("/conversion")
def create_conversion(data: ConversionCreate, request: Request, db: Session = Depends(get_db)):
    """Create (or accumulate onto) a single conversion from the manual-add
    dialog. Mirrors import_conversions' LTV dedupe semantics but accepts the
    richer field set (revenue, sub ids) the dialog offers."""
    from audit_logger import audit_event
    from auth import get_caller
    status = normalize_status(data.status) if data.status else "sale"
    if status not in all_valid_statuses(db):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Allowed: {', '.join(sorted(all_valid_statuses(db)))}")
    approval = normalize_approval(data.approval) if data.approval else "pending"
    if approval not in VALID_APPROVALS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid approval. Allowed: {', '.join(sorted(VALID_APPROVALS))}")
    click_id = str(data.click_id or "").strip()
    external_id = str(data.external_id or "").strip() or None
    transaction_id = str(data.transaction_id or "").strip() or None
    if not (click_id or external_id or transaction_id):
        raise HTTPException(
            status_code=400,
            detail="Provide a click id, external id or transaction id")
    payout = float(data.payout or 0)
    revenue = float(data.revenue) if data.revenue is not None else None

    sub_fields = {}
    for k, v in (data.sub_ids or {}).items():
        if re.fullmatch(r"sub_id_\d{1,2}", str(k)) and v not in (None, ""):
            sub_fields[str(k)] = str(v)[:50]

    conv, created, _ = _write_conversion(
        db, source="manual", status=status, approval=approval, payout=payout,
        revenue=revenue, click_id=click_id, external_id=external_id,
        transaction_id=transaction_id, sub_fields=sub_fields,
        apply_approval=bool(data.approval))

    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "create", "conversions", str(conv.id),
                {"source": "manual", "created": created, "status": status,
                 "approval": approval},
                request.client.host if request.client else "")
    return {"message": "Conversion created" if created else "Conversion updated",
            "id": conv.id, "created": created}


def _ingest_sub_fields(payload: dict) -> dict:
    """Collect sub ids from either flat ``sub_id_N`` keys or a nested
    ``sub_ids`` object (the shape the manual-add dialog and create_conversion
    use), truncated to the column width."""
    out = {}
    nested = payload.get("sub_ids")
    sources = [payload]
    if isinstance(nested, dict):
        sources.append(nested)
    for src in sources:
        for k, v in src.items():
            key = str(k)
            if re.fullmatch(r"sub_id_\d{1,2}", key) and v not in (None, ""):
                out[key] = str(v)[:50]
    return out


def _ingest_float(value) -> Optional[float]:
    """Payout parser tolerant of a comma decimal separator (the CSV importer
    accepts '1,25'); returns None when the value is present but not numeric."""
    if value is None or str(value).strip() == "":
        return 0.0
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


@router.post("/ingest")
async def ingest_conversion(request: Request, db: Session = Depends(get_db)):
    """Inbound ingestion for a CRM / call platform (workspace API token via
    ``Authorization: Bearer``; the router's reports write dependency enforces
    it, and the request runs tenant-scoped). Accepts JSON or form-encoded
    fields and reuses the single _write_conversion writer, so a repeat
    transaction id updates the existing row instead of duplicating it. Every
    bad payload is answered with a clear JSON ``rejected`` result, never a 500.
    """
    from audit_logger import audit_event
    from auth import get_caller

    def reject(reason: str, code: int = 400):
        return JSONResponse(status_code=code,
                            content={"result": "rejected", "reason": reason})

    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    try:
        body = await request.body()
        if ctype == "application/json" or body.lstrip()[:1] in (b"{", b"["):
            payload = json.loads(body.decode("utf-8") or "{}")
        else:
            # form-encoded (and any non-JSON body) parsed without pulling in a
            # multipart dependency.
            parsed = urllib.parse.parse_qs(body.decode("utf-8"),
                                           keep_blank_values=True)
            payload = {k: v[0] for k, v in parsed.items()}
    except Exception:
        return reject("Malformed request body: expected a JSON object or "
                      "form-encoded fields")
    if not isinstance(payload, dict):
        return reject("Malformed request body: expected a JSON object")

    def field(*names) -> str:
        for name in names:
            value = payload.get(name)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    click_id = field("click_id", "clickid")
    external_id = field("external_id") or None
    transaction_id = field("transaction_id") or None
    sub_fields = _ingest_sub_fields(payload)
    if not (click_id or external_id or transaction_id or sub_fields):
        return reject("Provide a click_id, external_id, transaction_id or sub_id")

    status = normalize_status(field("status") or "sale")
    if status not in all_valid_statuses(db):
        return reject(f"Invalid status. Allowed: "
                      f"{', '.join(sorted(all_valid_statuses(db)))}")

    approval = normalize_approval(field("approval") or "pending")
    if approval not in VALID_APPROVALS:
        return reject(f"Invalid approval. Allowed: "
                      f"{', '.join(sorted(VALID_APPROVALS))}")

    payout = _ingest_float(payload.get("payout"))
    if payout is None:
        return reject(f"Invalid payout '{payload.get('payout')}'")

    occurred_raw = field("occurred_at")
    occurred_at = _parse_occurred_at(occurred_raw) if occurred_raw else None
    if occurred_raw and occurred_at is None:
        return reject(f"Invalid occurred_at '{occurred_raw}': expected ISO-8601")

    currency = field("currency") or None
    phone = field("phone")
    note = field("note")
    # conversions_data has no phone/note column; the values ride the events
    # JSONB history rather than inventing columns.
    conv, created, attributed = _write_conversion(
        db, source="ingest", status=status, approval=approval, payout=payout,
        revenue=None, click_id=click_id, external_id=external_id,
        transaction_id=transaction_id, sub_fields=sub_fields,
        occurred_at=occurred_at, currency=currency, match_sub_ids=True,
        apply_approval=False, event_extra={"phone": phone, "note": note})
    db.commit()

    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "create" if created else "update",
                "conversions", str(conv.id),
                {"source": "ingest", "created": created, "attributed": attributed},
                request.client.host if request.client else "")
    return {"result": "created" if created else "updated", "id": conv.id,
            "created": created, "duplicate": bool(conv.is_duplicate),
            "attributed": attributed, "click_id": conv.click_id,
            "transaction_id": conv.transaction_id, "status": conv.status}


@router.get("/summary")
def conversions_summary(request: Request, db: Session = Depends(get_db)):
    """Approval reconciliation totals over the same filters as the list:
    pending / approved / declined / other plus approval and decline rates
    (percentage, divide-by-zero safe). Aggregated in Postgres."""
    query = _build_conversions_query(request, db)
    approval_col = func.coalesce(Conversion.approval, "pending")
    total, approved, declined, pending, other = query.with_entities(
        func.count(Conversion.id),
        func.sum(case((approval_col == "approved", 1), else_=0)),
        func.sum(case((approval_col == "declined", 1), else_=0)),
        func.sum(case((approval_col == "pending", 1), else_=0)),
        func.sum(case((approval_col == "other", 1), else_=0)),
    ).one()
    total = int(total or 0)
    approved = int(approved or 0)
    declined = int(declined or 0)
    result = {
        "total": total,
        "approved": approved,
        "declined": declined,
        "pending": int(pending or 0),
        "other": int(other or 0),
        "approval_rate": round(approved / total * 100, 2) if total else 0.0,
        "decline_rate": round(declined / total * 100, 2) if total else 0.0,
    }
    # The reconciliation summary carries no hideable metric today, but the
    # strip keeps the guarantee uniform if a money column is added later.
    strip_hidden_metrics(result, request_hidden_metrics(request, db))
    truncation = _attribution_truncation(request)
    if truncation:
        result.update(truncation)
    return result


@router.delete("/{conversion_id}")
def delete_conversion(conversion_id: int, request: Request, db: Session = Depends(get_db)):
    conv = db.query(Conversion).filter_by(id=conversion_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversion not found")
    _require_conversion_access(request, db, conv)

    db.delete(conv)
    db.commit()
    return {"message": "Conversion deleted"}
