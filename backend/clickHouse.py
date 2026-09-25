from datetime import datetime, timedelta, date
import os

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

    # Substring search over text fields (searching in ILIKE fashion)
    search = filters.get("search")
    if search:
        conditions.append("(url ILIKE %(search)s OR referrer ILIKE %(search)s OR ip ILIKE %(search)s OR visitor_id ILIKE %(search)s OR keyword ILIKE %(search)s)")
        params["search"] = f"%{search}%"

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

    print(filters)
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

    print("QUERY:", query)

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
    "utm_campaign": "utm_campaign",
    "utm_creative": "utm_creative",
    "keyword": "keyword",
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
        cost = float(row.get("cost") or 0)
        revenue = float(row.get("revenue") or 0)
        clicks = int(row.get("clicks") or 0)
        conversions = int(row.get("conversions") or 0)
        row["cost"] = round(cost, 4)
        row["revenue"] = round(revenue, 4)
        row["profit"] = round(float(row.get("profit") or 0), 4)
        row["cr"] = round(conversions / clicks * 100, 2) if clicks else 0.0
        row["epc"] = round(revenue / clicks, 4) if clicks else 0.0
        row["roi"] = round((revenue - cost) / cost * 100, 2) if cost else 0.0

    return rows
