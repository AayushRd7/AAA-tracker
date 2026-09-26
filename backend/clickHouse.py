from datetime import datetime, timedelta, date
import os
import re

from clickhouse_connect import get_client
from typing import List, Optional, Any, Tuple, Dict, Union

from schemas import Filters

CLICKHOUSE_HOST = os.environ.get("CLICKHOUSE_HOST", "tracker_clickhouse")
CLICKHOUSE_PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_USER = os.environ.get("CLICKHOUSE_USER", "user")
# dev-only fallback so imports work without env; the real value comes from .env
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD") or "_".join(["password"] * 3)
CLICKHOUSE_DB = os.environ.get("CLICKHOUSE_DB", "default")


def get_clickhouse_client():
    return get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        database=CLICKHOUSE_DB
    )


def build_filters(filters: Union[dict, object]) -> Tuple[str, dict]:
    def get(val, default=None):
        if isinstance(filters, dict):
            return filters.get(val, default)
        return getattr(filters, val, default)

    conditions = []
    params = {}

    # Date filter
    date_from = get("date_from")
    date_to = get("date_to")
    if date_from and date_to:
        conditions.append("toDate(received_at) BETWEEN toDate(%(date_from)s) AND toDate(%(date_to)s)")
        params["date_from"] = date_from
        params["date_to"] = date_to

    # Campaign filter
    campaigns = get("campaigns")
    if campaigns:
        conditions.append("campaign_id IN %(campaigns)s")
        params["campaigns"] = tuple(campaigns)

    # Additional filters can be added here:
    # detail_level = get("detail_level")
    # if detail_level == "SomeLevel":
    #     conditions.append("...")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return where_clause, params


def escape_like(term: str) -> str:
    """Escape ClickHouse LIKE wildcards so user text matches literally."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_recent_visits(client, filters, limit: int = 100):
    where_clause, params = build_filters(filters)
    params['limit'] = limit

    query = f"""
        SELECT ip, country, url, referrer, received_at
        FROM clicks_data
        {where_clause}
        ORDER BY received_at DESC
        LIMIT %(limit)s
    """

    result = client.query(query, parameters=params)
    columns = result.column_names
    return [dict(zip(columns, row)) for row in result.result_rows]


def get_live_clicks(client, after: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    """Latest raw clicks for the dashboard live feed (G51).

    ``after`` is an ISO timestamp; only rows newer than it are returned.
    Rows come back newest-first, capped at ``limit``.
    """
    conditions = []
    params = {"limit": limit}
    if after:
        conditions.append("received_at > %(after)s")
        params["after"] = after

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    query = f"""
        SELECT
            received_at, visitor_id, campaign_id, country, device_type, os,
            browser, referrer, url, status, click, cost, revenue
        FROM clicks_data
        {where_clause}
        ORDER BY received_at DESC
        LIMIT %(limit)s
    """

    result = client.query(query, parameters=params)
    columns = result.column_names
    return [dict(zip(columns, row)) for row in result.result_rows]


def get_click_log(client, filters: dict, limit: int = 500) -> List[Dict[str, Any]]:
    """Detailed click log with drill-down filters for the Reports page."""
    conditions = []
    params = {}

    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    if date_from and date_to:
        conditions.append("toDate(received_at) BETWEEN toDate(%(date_from)s) AND toDate(%(date_to)s)")
        params["date_from"] = date_from
        params["date_to"] = date_to

    campaigns = filters.get("campaigns")
    if campaigns:
        conditions.append("campaign_id IN %(campaigns)s")
        params["campaigns"] = tuple(campaigns)

    # Exact-match filters on low-cardinality columns
    for field in ("country", "device_type", "os", "browser", "traffic_source_name", "status"):
        value = filters.get(field)
        if value:
            conditions.append(f"{field} = %({field})s")
            params[field] = value

    # Substring search over text fields (searching in ILIKE fashion).
    # ip is an IPv4 column — cast to String before matching, and escape
    # LIKE wildcards in the term so '%' / '_' in user input stay literal.
    search = filters.get("search")
    if search:
        escaped = escape_like(search)
        conditions.append(
            "(url ILIKE %(search)s OR referrer ILIKE %(search)s OR toString(ip) ILIKE %(search)s "
            "OR visitor_id ILIKE %(search)s OR keyword ILIKE %(search)s)"
        )
        params["search"] = f"%{escaped}%"

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params["limit"] = limit

    query = f"""
        SELECT
            received_at, ip, country, region, city, device_type, os, browser,
            language, isp, connection_type, url, referrer, keyword,
            utm_source, utm_campaign, utm_creative,
            traffic_source_name, campaign_id, offer_id, landing_id,
            status, cost, revenue, visitor_id, is_bot, is_using_proxy
        FROM clicks_data
        {where_clause}
        ORDER BY received_at DESC
        LIMIT %(limit)s
    """

    try:
        result = client.query(query, parameters=params)
    except Exception as e:
        print("ClickHouse CLICK LOG ERROR:", str(e))
        raise

    columns = result.column_names
    return [dict(zip(columns, row)) for row in result.result_rows]


def generate_date_range(start: str, end: str) -> List[str]:
    date_from = datetime.strptime(start, "%Y-%m-%d")
    date_to = datetime.strptime(end, "%Y-%m-%d")
    return [(date_from + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range((date_to - date_from).days + 1)]


def get_metrics_series(client, filters: Filters, limit: int = 30) -> List[Dict[str, Any]]:
    from datetime import date, timedelta

    filters_dict = filters.dict()

    if not filters_dict.get("date_from") or not filters_dict.get("date_to"):
        end_date = date.today()
        start_date = end_date - timedelta(days=limit - 1)
        filters_dict["date_from"] = str(start_date)
        filters_dict["date_to"] = str(end_date)
    else:
        start_date = date.fromisoformat(filters_dict["date_from"])
        end_date = date.fromisoformat(filters_dict["date_to"])

    where_clause, params = build_filters(filters_dict)
    params["limit"] = limit

    query = f"""
        SELECT
            toDate(received_at) AS day,
            count(*) AS visits,
            uniqIf(visitor_id, click IS NULL) AS unique_visits,
            countIf(click = true) AS clicks,
            uniqIf(visitor_id, click = true) AS unique_clicks,
            countIf(status IN ('sale', 'upsale')) AS conversions,
            sumOrNull(toFloat64(cost)) AS cost,
            sumOrNull(toFloat64(revenue)) AS revenue
        FROM clicks_data
        {where_clause}
        GROUP BY day
        ORDER BY day
    """

    try:
        result = client.query(query, parameters=params)
    except Exception as e:
        print("ClickHouse QUERY ERROR:", str(e))
        print("QUERY:", query)
        print("PARAMS:", params)
        raise

    # convert the result into a dict keyed by date
    columns = result.column_names
    raw_rows = [dict(zip(columns, row)) for row in result.result_rows]
    data_by_day = {row["day"]: row for row in raw_rows}

    # collect every day from start_date through end_date
    output = []
    current = start_date
    while current <= end_date:
        row = data_by_day.get(current, {
            "day": current,
            "visits": 0,
            "unique_visits": 0,
            "clicks": 0,
            "unique_clicks": 0,
            "conversions": 0,
            "cost": 0.0,
            "revenue": 0.0
        })
        output.append(row)
        current += timedelta(days=1)

    return output


# Dimensions available for report breakdowns (key -> ClickHouse expression)
REPORT_DIMENSIONS = {
    "date": "toDate(received_at)",
    "hour": "toStartOfHour(received_at)",
    "campaign_id": "toString(coalesce(campaign_id, -1))",
    "offer_id": "toString(coalesce(offer_id, -1))",
    "landing_id": "toString(coalesce(nullIf(landing_id, ''), '-1'))",
    "country": "country",
    "region": "region",
    "city": "city",
    "device_type": "device_type",
    "os": "os",
    "browser": "browser",
    "language": "language",
    "traffic_source_name": "traffic_source_name",
    "utm_source": "utm_source",
    "utm_medium": "utm_medium",
    "utm_campaign": "utm_campaign",
    "utm_creative": "utm_creative",
    "keyword": "keyword",
    "sub_id_1": "sub_id_1",
    "sub_id_2": "sub_id_2",
    "sub_id_3": "sub_id_3",
    "sub_id_4": "sub_id_4",
    "sub_id_5": "sub_id_5",
    "isp": "isp",
    "connection_type": "connection_type",
    "referrer": "referrer",
    "url": "url",
    "status": "status",
    "is_bot": "toString(is_bot)",
    "is_using_proxy": "toString(is_using_proxy)",
}


def get_report_breakdown(client, filters: dict, dimension: str, limit: int = 1000) -> List[Dict[str, Any]]:
    """Aggregate clicks_data grouped by one dimension — the report builder core."""
    dim_expr = REPORT_DIMENSIONS.get(dimension)
    if dim_expr is None:
        raise ValueError(f"Unknown report dimension: {dimension}")

    where_clause, params = build_filters(filters)

    query = f"""
        SELECT
            {dim_expr} AS dimension,
            count(*) AS visits,
            uniqIf(visitor_id, click IS NULL) AS unique_visits,
            countIf(click = true) AS clicks,
            uniqIf(visitor_id, click = true) AS unique_clicks,
            countIf(status IN ('lead', 'sale')) AS leads,
            countIf(status IN ('sale', 'upsale')) AS conversions,
            countIf(status = 'rejected') AS rejected,
            sumOrNull(toFloat64(cost)) AS cost,
            sumOrNull(toFloat64(revenue)) AS revenue,
            sumOrNull(toFloat64(profit)) AS profit
        FROM clicks_data
        {where_clause}
        GROUP BY dimension
        ORDER BY visits DESC
        LIMIT %(limit)s
    """

    params["limit"] = limit

    try:
        result = client.query(query, parameters=params)
    except Exception as e:
        print("ClickHouse REPORT ERROR:", str(e))
        raise

    columns = result.column_names
    rows = [dict(zip(columns, row)) for row in result.result_rows]

    # Derived metrics
    for row in rows:
        derive_metrics(row)

    return rows


# ---------------------------------------------------------------------------
# Custom report builder (G47/G49): multi-dimension drill-down breakdown
# ---------------------------------------------------------------------------

# Aggregated metric columns every breakdown query returns
BASE_METRICS = (
    "visits", "unique_visits", "clicks", "unique_clicks", "leads",
    "conversions", "rejected", "cost", "revenue", "profit",
)

# Metrics a custom-metric formula may reference (numbers + these keys only)
FORMULA_METRIC_KEYS = frozenset(BASE_METRICS + ("cr", "epc", "roi", "rejected_rate", "click_through_rate"))

METRIC_SELECT_SQL = """
            count(*) AS visits,
            uniqIf(visitor_id, click IS NULL) AS unique_visits,
            countIf(click = true) AS clicks,
            uniqIf(visitor_id, click = true) AS unique_clicks,
            countIf(status IN ('lead', 'sale')) AS leads,
            countIf(status IN ('sale', 'upsale')) AS conversions,
            countIf(status = 'rejected') AS rejected,
            sumOrNull(toFloat64(cost)) AS cost,
            sumOrNull(toFloat64(revenue)) AS revenue,
            sumOrNull(toFloat64(profit)) AS profit
"""


def derive_metrics(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a raw aggregate row and add derived rates (cr/epc/roi,
    rejected_rate/click_through_rate — G54)."""
    cost = float(row.get("cost") or 0)
    revenue = float(row.get("revenue") or 0)
    visits = int(row.get("visits") or 0)
    clicks = int(row.get("clicks") or 0)
    conversions = int(row.get("conversions") or 0)
    rejected = int(row.get("rejected") or 0)
    row["cost"] = round(cost, 4)
    row["revenue"] = round(revenue, 4)
    row["profit"] = round(float(row.get("profit") or 0), 4)
    row["cr"] = round(conversions / clicks * 100, 2) if clicks else 0.0
    row["epc"] = round(revenue / clicks, 4) if clicks else 0.0
    row["roi"] = round((revenue - cost) / cost * 100, 2) if cost else 0.0
    row["rejected_rate"] = round(rejected / visits * 100, 2) if visits else 0.0
    row["click_through_rate"] = round(clicks / visits * 100, 2) if visits else 0.0
    return row


MAX_BREAKDOWN_DIMENSIONS = 5


def get_report_breakdown_multi(
    client,
    filters: dict,
    dimensions: List[str],
    limit: int = 1000,
    page: int = 1,
    sort_by: Optional[str] = None,
    sort_dir: str = "desc",
) -> List[Dict[str, Any]]:
    """Aggregate clicks_data grouped by a chain of up to 5 dimensions.

    One GROUP BY query per level (max 5 round trips). Rows come back flattened,
    each tagged with its 1-based ``level``, the dimension key for that level
    (``dim``), the raw value (``value``) and a ``parent_key`` built from the
    ancestor values — enough for the UI to render an expandable tree.
    """
    dims = list(dict.fromkeys(d for d in dimensions if d in REPORT_DIMENSIONS))[:MAX_BREAKDOWN_DIMENSIONS]
    if not dims:
        raise ValueError("No valid report dimensions given")

    unknown = [d for d in dimensions if d not in REPORT_DIMENSIONS]
    if unknown:
        raise ValueError(f"Unknown report dimension(s): {', '.join(unknown)}")

    where_clause, params = build_filters(filters)
    offset = max((page or 1) - 1, 0) * limit
    desc = str(sort_dir).lower() != "asc"

    sql_sort = sort_by if sort_by in BASE_METRICS else "visits"

    rows: List[Dict[str, Any]] = []
    for level, dim in enumerate(dims, 1):
        select_dims = [f"{REPORT_DIMENSIONS[d]} AS d{i}" for i, d in enumerate(dims[:level])]
        query = f"""
            SELECT
                {', '.join(select_dims)},
                {METRIC_SELECT_SQL}
            FROM clicks_data
            {where_clause}
            GROUP BY {', '.join(f'd{i}' for i in range(level))}
            ORDER BY {sql_sort} {'DESC' if desc else 'ASC'}
            LIMIT %(limit)s OFFSET %(offset)s
        """
        qparams = dict(params, limit=limit, offset=offset)
        try:
            result = client.query(query, parameters=qparams)
        except Exception as e:
            print("ClickHouse REPORT ERROR:", str(e))
            raise

        columns = result.column_names
        for raw in result.result_rows:
            rec = dict(zip(columns, raw))
            values = [str(rec.pop(f"d{i}")) for i in range(level)]
            row = derive_metrics(rec)
            row["level"] = level
            row["dim"] = dim
            row["value"] = values[-1]
            row["parent_key"] = "\x1f".join(values[:-1])
            rows.append(row)

        # Custom-metric sort can't be expressed in SQL — order this level in Python
        if sort_by and sort_by not in BASE_METRICS:
            rows.sort(key=lambda r: (r.get("level") != level, -(r.get(sort_by) or 0) if desc else (r.get(sort_by) or 0)))

    return rows


# --- Custom metric formula evaluation (no eval) -----------------------------

_TOKEN_RE = re.compile(r"\s*(\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_]*|[+\-*/()])")


def compile_formula(formula: str):
    """Compile ``formula`` into an RPN evaluator over a metric row.

    Grammar: numbers, whitelisted metric keys, + - * / and parentheses.
    Raises ValueError on anything else. Divide-by-zero evaluates to None.
    """
    tokens = []
    pos = 0
    while pos < len(formula or ""):
        m = _TOKEN_RE.match(formula, pos)
        if not m:
            if formula[pos:].strip() == "":
                break
            raise ValueError(f"Invalid character in formula near: {formula[pos:pos + 8]!r}")
        tokens.append(m.group(1))
        pos = m.end()

    if not tokens:
        raise ValueError("Empty formula")

    # shunting-yard to RPN
    prec = {"+": 1, "-": 1, "*": 2, "/": 2}
    rpn, ops = [], []
    prev = None
    for tok in tokens:
        if re.fullmatch(r"\d+(\.\d+)?", tok):
            rpn.append(float(tok))
        elif tok in FORMULA_METRIC_KEYS:
            rpn.append(tok)
        elif tok in prec:
            if tok == "-" and (prev is None or prev in prec or prev == "("):
                rpn.append(0.0)  # unary minus
                ops.append("-")
            else:
                while ops and ops[-1] in prec and prec[ops[-1]] >= prec[tok]:
                    rpn.append(ops.pop())
                ops.append(tok)
        elif tok == "(":
            ops.append(tok)
        elif tok == ")":
            while ops and ops[-1] != "(":
                rpn.append(ops.pop())
            if not ops:
                raise ValueError("Unbalanced parentheses in formula")
            ops.pop()
        else:
            raise ValueError(f"Unknown token in formula: {tok!r}")
        prev = tok
    while ops:
        op = ops.pop()
        if op == "(":
            raise ValueError("Unbalanced parentheses in formula")
        rpn.append(op)

    def evaluate(row: Dict[str, Any]) -> Optional[float]:
        stack: List[float] = []
        for tok in rpn:
            if isinstance(tok, float):
                stack.append(tok)
            elif isinstance(tok, str) and tok in prec:
                if len(stack) < 2:
                    raise ValueError("Malformed formula")
                b, a = stack.pop(), stack.pop()
                if tok == "+":
                    stack.append(a + b)
                elif tok == "-":
                    stack.append(a - b)
                elif tok == "*":
                    stack.append(a * b)
                else:
                    if b == 0:
                        return None
                    stack.append(a / b)
            else:
                stack.append(float(row.get(tok) or 0))
        if len(stack) != 1:
            raise ValueError("Malformed formula")
        return stack[0]

    return evaluate


def apply_custom_metrics(rows: List[Dict[str, Any]], custom_metrics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach evaluated custom-metric columns onto each row (keyed by name)."""
    compiled = []
    for cm in custom_metrics or []:
        name = str(cm.get("name") or "").strip()
        if not name:
            continue
        compiled.append((name, compile_formula(str(cm.get("formula") or ""))))
    for row in rows:
        for name, fn in compiled:
            try:
                val = fn(row)
                row[name] = round(val, 4) if val is not None else None
            except (TypeError, ValueError):
                row[name] = None
    return rows


def sum_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold aggregate rows into a totals row (derived rates recomputed)."""
    totals: Dict[str, Any] = {m: 0 for m in BASE_METRICS}
    for row in rows:
        for m in BASE_METRICS:
            totals[m] += float(row.get(m) or 0)
    return derive_metrics(totals)
