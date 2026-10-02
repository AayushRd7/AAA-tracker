"""Warehouse-load-friendly (BI) CSV exports.

A customer points a loader (BigQuery / Snowflake / Sheets) at a downloaded
file and it just works — there are no third-party credentials here and nothing
is pushed anywhere, the endpoint only returns bytes.

Three independent raw tables are offered, one file each (never a wide join):
``clicks``, ``conversions`` and ``costs``. Each download also carries a
manifest (``README.txt`` + ``_manifest.csv``) naming the datasets, their
columns/types, the row counts and the exact load hint per destination.

Format contract (deliberately strict so a loader needs no configuration):
* RFC-4180 CSV, comma separated, quoted where needed, CRLF line endings
* UTF-8, BOM optional (off by default — a Sheets import sometimes needs it)
* timestamps are ISO-8601 UTC with an explicit trailing ``Z`` (never naive local)
* snake_case headers, stable column order per dataset
* no thousands separators, no scientific notation, ``.`` decimal, '' for NULL
* gzip optional for large ranges

Routes live on the existing dashboard router (see the ``include_router`` at the
bottom of ``app_pages/dashboard.py``) so no new router is mounted in app.py.
"""
import csv
import gzip
import io
import zipfile
from datetime import date as date_cls
from datetime import datetime as datetime_cls
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from db import get_db
from clickHouse import build_filters
from app_pages.logs import _csv_safe

router = APIRouter()

# A date range is always required by the API but a missing one defaults to the
# last 7 days. The 92-day ceiling bounds one download's memory/size.
DEFAULT_RANGE_DAYS = 7
MAX_RANGE_DAYS = 92
# Hard row cap per dataset. The cap is disclosed in the manifest rather than
# silently cutting a file mid-stream, so a loader never receives a partial
# table without knowing.
ROW_CAP = 100_000

# Column key -> logical warehouse type. Types drive the manifest and the cell
# formatter; the order here is the stable column order of the file.
DatasetColumns = Sequence[Tuple[str, str]]

CLICK_COLUMNS: DatasetColumns = [
    ("received_at", "timestamp"),
    ("click_id", "string"),
    ("visitor_id", "string"),
    ("campaign_id", "integer"),
    ("offer_id", "integer"),
    ("landing_id", "string"),
    ("country", "string"),
    ("region", "string"),
    ("city", "string"),
    ("device_type", "string"),
    ("os", "string"),
    ("os_version", "string"),
    ("browser", "string"),
    ("language", "string"),
    ("isp", "string"),
    ("connection_type", "string"),
    ("url", "string"),
    ("domain", "string"),
    ("referrer", "string"),
    ("keyword", "string"),
    ("utm_source", "string"),
    ("utm_medium", "string"),
    ("utm_campaign", "string"),
    ("utm_creative", "string"),
    ("traffic_source_name", "string"),
    ("sub_id_1", "string"),
    ("sub_id_2", "string"),
    ("sub_id_3", "string"),
    ("sub_id_4", "string"),
    ("sub_id_5", "string"),
    ("sub_id_6", "string"),
    ("sub_id_7", "string"),
    ("sub_id_8", "string"),
    ("sub_id_9", "string"),
    ("sub_id_10", "string"),
    ("ip", "string"),
    ("user_agent", "string"),
    ("status", "string"),
    ("is_click", "boolean"),
    ("is_bot", "boolean"),
    ("is_using_proxy", "boolean"),
    ("impression", "boolean"),
    ("fraud_score", "integer"),
    ("cost", "decimal"),
    ("revenue", "decimal"),
    ("profit", "decimal"),
]

CLICK_SQL = {
    # received_at is stored as naive UTC; _fmt_timestamp renders it with the
    # explicit Z in Python rather than a formatDateTime literal, which would
    # collide with the driver's %-parameter binding.
    "domain": "domainWithoutWWW(url)",
    # ip_full carries the (possibly IPv6) client address; the legacy column is
    # the fallback, matching how every report displays it.
    "ip": "if(empty(ip_full), toString(ip), ip_full)",
    "is_click": "click",
}

CONVERSION_COLUMNS: DatasetColumns = [
    ("conversion_id", "integer"),
    ("received_at", "timestamp"),
    ("click_id", "string"),
    ("visitor_id", "string"),
    ("campaign_id", "integer"),
    ("offer_id", "integer"),
    ("landing_id", "integer"),
    ("status", "string"),
    ("approval", "string"),
    ("external_id", "string"),
    ("transaction_id", "string"),
    ("currency", "string"),
    ("payout", "decimal"),
    ("revenue", "decimal"),
    ("profit", "decimal"),
    ("postback_count", "integer"),
    ("last_postback_at", "timestamp"),
    ("country", "string"),
    ("region", "string"),
    ("city", "string"),
    ("ip", "string"),
    ("device_type", "string"),
    ("os", "string"),
    ("isp", "string"),
    ("is_bot", "boolean"),
    ("is_using_proxy", "boolean"),
    ("is_duplicate", "boolean"),
    ("funnel_step", "integer"),
    ("sub_id_1", "string"),
    ("sub_id_2", "string"),
    ("sub_id_3", "string"),
    ("sub_id_4", "string"),
    ("sub_id_5", "string"),
    ("sub_id_6", "string"),
    ("sub_id_7", "string"),
    ("sub_id_8", "string"),
    ("sub_id_9", "string"),
    ("sub_id_10", "string"),
    ("utm_source", "string"),
    ("utm_campaign", "string"),
    ("utm_creative", "string"),
    ("traffic_source_name", "string"),
]

COST_COLUMNS: DatasetColumns = [
    ("date", "date"),
    ("campaign_id", "integer"),
    ("cost", "decimal"),
]

DATASETS: Dict[str, DatasetColumns] = {
    "clicks": CLICK_COLUMNS,
    "conversions": CONVERSION_COLUMNS,
    "costs": COST_COLUMNS,
}
DATASET_ORDER = ("clicks", "conversions", "costs")

# Load hint per destination. The names are destinations the customer asked for,
# not internal technology of this service.
LOAD_HINTS = {
    "BigQuery": (
        "bq load --autodetect --source_format=CSV "
        "<your_dataset>.<table> <file>.csv"
    ),
    "Snowflake": (
        "COPY INTO <table> FROM @<stage>/<file>.csv "
        "FILE_FORMAT = (TYPE = CSV FIELD_OPTIONALLY_ENCLOSED_BY='\"' SKIP_HEADER = 1);"
    ),
    "Google Sheets": (
        "File > Import > Upload > choose the .csv > Import location: "
        "\"Import as CSV\" (or \"Insert new sheet(s)\")"
    ),
}


def _parse_iso_date(value, label: str) -> date_cls:
    try:
        return date_cls.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {label}: expected an ISO date (YYYY-MM-DD)")


def _resolve_range(date_from: Optional[str], date_to: Optional[str]) -> Tuple[date_cls, date_cls]:
    """Normalise/validate the requested window. A missing bound defaults (the
    UI always sends one, but a direct loader call may not)."""
    today = datetime_cls.utcnow().date()
    d_to = _parse_iso_date(date_to, "date_to") if date_to else today
    d_from = (_parse_iso_date(date_from, "date_from") if date_from
              else d_to - timedelta(days=DEFAULT_RANGE_DAYS - 1))
    if d_from > d_to:
        raise HTTPException(status_code=400, detail="date_from must not be after date_to")
    if (d_to - d_from).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Date range spans more than {MAX_RANGE_DAYS} days — narrow it")
    return d_from, d_to


def _fmt_decimal(value) -> str:
    """Plain decimal text: no thousands separator, no scientific notation, a
    single '.' and an empty field for NULL/NaN. Rounded to 6 dp to shed the
    float32 noise a per-click cost can carry without changing the value."""
    if value is None or value == "":
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return _csv_safe(str(value))
    if num != num or num in (float("inf"), float("-inf")):
        return ""
    num = round(num, 6)
    if num == int(num):
        return str(int(num))
    try:
        return format(Decimal(str(num)), "f")
    except InvalidOperation:
        return str(num)


def _fmt_integer(value) -> str:
    if value is None or value == "":
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return _csv_safe(str(value))


def _fmt_timestamp(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, datetime_cls):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = str(value)
    # A driver that returns text may already carry the zone; normalise to a
    # single explicit Z without mangling the timestamp.
    if raw.endswith("Z"):
        return raw
    if raw.endswith("+00:00"):
        return raw[:-6] + "Z"
    return raw + "Z"


def _fmt_date(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (datetime_cls, date_cls)):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _format_cell(value: Any, logical_type: str) -> str:
    if logical_type == "timestamp":
        return _fmt_timestamp(value)
    if logical_type == "date":
        return _fmt_date(value)
    if logical_type == "integer":
        return _fmt_integer(value)
    if logical_type == "decimal":
        return _fmt_decimal(value)
    if logical_type == "boolean":
        # NULL stays empty; a real flag is lowercase true/false.
        if value is None or value == "":
            return ""
        return "true" if value else "false"
    # Text cells go through the shared formula-injection guard; numeric and
    # timestamp cells must not, or a negative number would be quoted as text.
    return _csv_safe("" if value is None else str(value))


def _csv_bytes(columns: DatasetColumns, rows: List[Dict[str, Any]],
               include_bom: bool) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([key for key, _ in columns])
    for row in rows:
        writer.writerow([_format_cell(row.get(key), logical_type)
                         for key, logical_type in columns])
    data = buf.getvalue().encode("utf-8")
    if include_bom:
        data = b"\xef\xbb\xbf" + data
    return data


# ---------------------------------------------------------------------------
# per-dataset readers
# ---------------------------------------------------------------------------

def _campaign_scope(request: Request, db: Session) -> Optional[List[int]]:
    """campaigns:'own' parity for the click/cost tables. None = unscoped,
    [] = an empty scope that must yield no rows."""
    from app_pages.dashboard import _click_scope_campaign_ids
    return _click_scope_campaign_ids(request, db)


def _read_clicks(ch, d_from: date_cls, d_to: date_cls,
                 campaign_scope: Optional[List[int]]) -> Tuple[List[Dict[str, Any]], bool]:
    if campaign_scope == []:
        return [], False
    where_clause, params = build_filters({
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "campaigns": campaign_scope or None,
    })
    select = ", ".join(
        f"{CLICK_SQL.get(key, key)} AS {key}" for key, _ in CLICK_COLUMNS)
    params["cap"] = ROW_CAP + 1
    query = (f"SELECT {select} FROM clicks_data {where_clause} "
             f"ORDER BY received_at ASC LIMIT %(cap)s")
    result = ch.query(query, parameters=params)
    rows = [dict(zip(result.column_names, row)) for row in result.result_rows]
    return _apply_cap(rows)


def _read_costs(ch, d_from: date_cls, d_to: date_cls,
                campaign_scope: Optional[List[int]]) -> Tuple[List[Dict[str, Any]], bool]:
    if campaign_scope == []:
        return [], False
    where_clause, params = build_filters({
        "date_from": d_from.isoformat(),
        "date_to": d_to.isoformat(),
        "campaigns": campaign_scope or None,
    })
    params["cap"] = ROW_CAP + 1
    query = (
        "SELECT toDate(received_at) AS date, campaign_id, "
        "sum(toFloat64(cost)) AS cost "
        f"FROM clicks_data {where_clause} "
        "GROUP BY date, campaign_id ORDER BY date ASC, campaign_id ASC "
        "LIMIT %(cap)s")
    result = ch.query(query, parameters=params)
    rows = [dict(zip(result.column_names, row)) for row in result.result_rows]
    return _apply_cap(rows)


def _read_conversions(request: Request, db: Session, d_from: date_cls,
                      d_to: date_cls) -> Tuple[List[Dict[str, Any]], bool]:
    from app_pages.reports import Conversion, _conversions_scope
    start = datetime_cls.combine(d_from, datetime_cls.min.time())
    end = datetime_cls.combine(d_to, datetime_cls.min.time())
    end = end + timedelta(days=1)  # inclusive end date
    query = (db.query(Conversion)
             .filter(Conversion.received_at >= start,
                     Conversion.received_at < end))
    query = _conversions_scope(request, db, query)
    records = (query.order_by(Conversion.received_at.asc(), Conversion.id.asc())
               .limit(ROW_CAP + 1).all())
    rows = []
    for conv in records:
        row = {}
        for key, _ in CONVERSION_COLUMNS:
            if key == "conversion_id":
                row["conversion_id"] = conv.id
            else:
                row[key] = getattr(conv, key, None)
        rows.append(row)
    return _apply_cap(rows)


def _apply_cap(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    if len(rows) > ROW_CAP:
        return rows[:ROW_CAP], True
    return rows, False


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def _manifest_csv(selected: Sequence[str]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["dataset", "column", "type"])
    for dataset in selected:
        for key, logical_type in DATASETS[dataset]:
            writer.writerow([dataset, key, logical_type])
    return buf.getvalue().encode("utf-8")


def _readme(selected: Sequence[str], generated_at: str,
            date_from: date_cls, date_to: date_cls,
            meta: Dict[str, Dict[str, Any]]) -> str:
    lines = [
        "AAA Tracker — warehouse export",
        "",
        f"Generated: {generated_at}",
        f"Date range: {date_from.isoformat()} .. {date_to.isoformat()} (UTC, inclusive)",
        f"Datasets: {', '.join(selected)}",
        "",
        "FORMAT",
        "  RFC-4180 CSV, comma separated, quoted where needed, CRLF line endings.",
        "  UTF-8 encoded (a BOM is included only when the download asked for one).",
        "  Timestamps are ISO-8601 UTC with an explicit trailing Z, never local time.",
        "  Headers are snake_case with a stable column order per dataset.",
        "  Numbers use a '.' decimal with no thousands separators and no",
        "  scientific notation. NULL is an empty field.",
        "",
        "ROW COUNTS",
    ]
    truncated_any = False
    for dataset in selected:
        info = meta.get(dataset, {})
        note = ""
        if info.get("truncated"):
            truncated_any = True
            note = (f"  *** TRUNCATED at the {ROW_CAP} row limit — this file is "
                    f"NOT the full table. Narrow the date range and export again. ***")
        lines.append(f"  {dataset}.csv — {info.get('rows', 0)} rows{note}")
    if truncated_any:
        lines.insert(0, "")
        lines.insert(0, "!!! TRUNCATION WARNING: one or more files hit the row cap. !!!")
    lines += [
        "",
        "COLUMNS",
    ]
    for dataset in selected:
        lines.append(f"  {dataset}:")
        for key, logical_type in DATASETS[dataset]:
            lines.append(f"    {key}  {logical_type}")
    lines += [
        "",
        "LOAD HINTS",
    ]
    for destination, hint in LOAD_HINTS.items():
        lines.append(f"  {destination}:")
        lines.append(f"    {hint}")
    lines += [
        "",
        "  A .csv.gz file loads the same way in BigQuery and Snowflake (both",
        "  decompress automatically). Google Sheets cannot read .gz — unzip it",
        "  first.",
        "",
    ]
    return "\n".join(lines)


def _zip_bytes(selected: Sequence[str], files: Dict[str, bytes],
               readme: str, manifest: bytes) -> bytes:
    buf = io.BytesIO()
    # The archive is already deflate-compressed, so the gzip toggle is not
    # applied to members here (it still applies to single-dataset downloads).
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", readme)
        archive.writestr("_manifest.csv", manifest)
        for dataset in selected:
            archive.writestr(f"{dataset}.csv", files[dataset])
    return buf.getvalue()


def _collect(request: Request, db: Session, selected: Sequence[str],
             d_from: date_cls, d_to: date_cls,
             include_bom: bool) -> Tuple[Dict[str, bytes], Dict[str, Dict[str, Any]]]:
    ch = request.state.ch
    campaign_scope = _campaign_scope(request, db)
    files: Dict[str, bytes] = {}
    meta: Dict[str, Dict[str, Any]] = {}
    for dataset in selected:
        if dataset == "clicks":
            rows, truncated = _read_clicks(ch, d_from, d_to, campaign_scope)
        elif dataset == "conversions":
            rows, truncated = _read_conversions(request, db, d_from, d_to)
        else:
            rows, truncated = _read_costs(ch, d_from, d_to, campaign_scope)
        files[dataset] = _csv_bytes(DATASETS[dataset], rows, include_bom)
        meta[dataset] = {"rows": len(rows), "truncated": truncated}
    return files, meta


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# endpoints (mounted under /api/dashboard by dashboard.py)
# ---------------------------------------------------------------------------

# Declared before /exports/{dataset} so the literal path wins the match.
@router.get("/exports/zip")
def export_zip(request: Request,
               datasets: Optional[str] = None,
               date_from: Optional[str] = None,
               date_to: Optional[str] = None,
               bom: Optional[str] = None,
               db: Session = Depends(get_db)):
    """All selected datasets as one ZIP containing the CSVs + a manifest."""
    d_from, d_to = _resolve_range(date_from, date_to)
    if datasets:
        selected = [d.strip() for d in datasets.split(",") if d.strip()]
    else:
        selected = list(DATASET_ORDER)
    unknown = [d for d in selected if d not in DATASETS]
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"Unknown dataset(s): {', '.join(unknown)}")
    if not selected:
        raise HTTPException(status_code=400, detail="Select at least one dataset")

    include_bom = _truthy(bom)
    files, meta = _collect(request, db, selected, d_from, d_to, include_bom)
    generated_at = datetime_cls.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = _zip_bytes(
        selected, files,
        _readme(selected, generated_at, d_from, d_to, meta),
        _manifest_csv(selected))
    filename = f"warehouse_export_{d_from.isoformat()}_{d_to.isoformat()}.zip"
    return Response(content=payload, media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.get("/exports/{dataset}")
def export_dataset(dataset: str,
                   request: Request,
                   date_from: Optional[str] = None,
                   date_to: Optional[str] = None,
                   bom: Optional[str] = None,
                   # aliased so the module-level `gzip` import is not shadowed
                   gzip_: Optional[str] = Query(None, alias="gzip"),
                   db: Session = Depends(get_db)):
    """One dataset as a standalone CSV (optionally gzipped)."""
    if dataset not in DATASETS:
        raise HTTPException(status_code=404, detail=f"Unknown dataset: {dataset}")
    d_from, d_to = _resolve_range(date_from, date_to)
    include_bom = _truthy(bom)
    use_gzip = _truthy(gzip_)
    files, meta = _collect(request, db, [dataset], d_from, d_to, include_bom)
    payload = files[dataset]
    info = meta[dataset]
    filename = f"{dataset}_{d_from.isoformat()}_{d_to.isoformat()}.csv"
    headers = {
        "Content-Disposition": f"attachment; filename={filename}",
        "X-Export-Rows": str(info["rows"]),
        "X-Export-Row-Limit": str(ROW_CAP),
        "X-Export-Truncated": "1" if info["truncated"] else "0",
    }
    if use_gzip:
        # A real .gz download, not a Content-Encoding the browser would
        # transparently inflate — a loader that sees a .gz should get gzip
        # bytes on disk.
        payload = gzip.compress(payload)
        headers["Content-Disposition"] = f"attachment; filename={filename}.gz"
        return Response(content=payload, media_type="application/gzip", headers=headers)
    return Response(content=payload, media_type="text/csv", headers=headers)
