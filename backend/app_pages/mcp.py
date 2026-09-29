"""G76 — MCP / AI-agent access.

Minimal Model Context Protocol server: JSON-RPC 2.0 over a single HTTP POST
endpoint (`POST /api/mcp`), so external AI agents (Claude Desktop, Cursor,
custom assistants) can read tracker state and perform the safe day-to-day
operations (pause/activate a campaign) with the workspace's Settings API token:

    Authorization: Bearer <apiToken>

The token is workspace-scoped (multi-tenancy phase 2B): it resolves to the
tenant whose settings document holds it, and every tool below therefore sees
(and can only touch) that workspace's data.

Implemented methods: initialize, ping, tools/list, tools/call. JSON-RPC
notifications (requests without an `id`) are accepted and acknowledged with
202, per the spec. Protocol errors use standard JSON-RPC codes; business
errors (unknown campaign, invalid enum value, …) come back as a tools/call
result with isError=true so the agent can read and recover from the message.

Tools:
  campaigns.list        id/name/alias/type/status/tags for every campaign
  campaigns.get         one campaign with its flow summary
  campaigns.metrics     ClickHouse-backed per-campaign metrics for a period
  campaigns.set_status  pause / activate a campaign (audit-logged)
  offers.list           all offers (id/name/url/network)
  sources.list          all traffic sources (id/name/postback template)
  reports.summary       account-wide totals for a period (today/7d/30d/all)
  conversions.recent    latest conversions from Postgres
  insights.latest       cached anomaly-insights findings (see G56)

All tools are read-only except campaigns.set_status.
"""
import json
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from clickHouse import get_report_breakdown
from db import get_db
from models.campaigns import CampaignORM
from models.offers import OfferORM
from models.sources import SourceORM
from version import __version__

router = APIRouter()

PROTOCOL_VERSION = "2024-11-05"
PERIODS = ("today", "7d", "30d", "all")
STATUSES = ("active", "paused")


class _ToolError(Exception):
    """Business error surfaced to the agent as an isError tools/call result."""


# ---------------------------------------------------------------------------
# Argument schemas (validated before dispatch)
# ---------------------------------------------------------------------------

class _CampaignsListIn(BaseModel):
    include_archived: bool = False


class _CampaignsGetIn(BaseModel):
    campaign_id: int


class _SetStatusIn(BaseModel):
    campaign_id: int
    status: str


class _MetricsIn(BaseModel):
    period: str = "7d"
    campaign_id: Optional[int] = None


class _SummaryIn(BaseModel):
    period: str = "7d"


class _ConversionsIn(BaseModel):
    limit: int = 20
    campaign_id: Optional[int] = None


class _NoArgs(BaseModel):
    pass


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _period_bounds(period: str) -> tuple:
    """('today'|'7d'|'30d'|'all') -> (date_from, date_to) as 'YYYY-MM-DD' or
    (None, None) for 'all'. 'Today' is derived from UTC (smoke-suite tz fix)."""
    if period == "all":
        return None, None
    if period not in PERIODS:
        raise _ToolError(f"Unknown period '{period}' — use one of: {', '.join(PERIODS)}")
    today = datetime.utcnow().date()
    days = {"today": 0, "7d": 6, "30d": 29}[period]
    return (today - timedelta(days=days)).isoformat(), today.isoformat()


def _campaign_brief(c) -> dict:
    return {"id": c.id, "name": c.name, "alias": c.alias, "type": c.type.value,
            "status": c.status.value, "archived": bool(c.archived),
            "tags": c.tags or [], "updated_at": str(c.updated_at)}


def _tool_campaigns_list(args: _CampaignsListIn, request: Request, db: Session) -> Any:
    q = db.query(CampaignORM).order_by(CampaignORM.id.asc())
    if not args.include_archived:
        q = q.filter(CampaignORM.archived == False)  # noqa: E712
    return [_campaign_brief(c) for c in q.all()]


def _tool_campaigns_get(args: _CampaignsGetIn, request: Request, db: Session) -> Any:
    c = db.query(CampaignORM).filter_by(id=args.campaign_id).first()
    if not c:
        raise _ToolError(f"Campaign {args.campaign_id} not found")
    flows = ((c.config or {}).get("flows") or [])
    return {**_campaign_brief(c),
            "notes": c.notes, "traffic_source_id": c.traffic_source_id,
            "domain_id": c.domain_id,
            "flows": [{"position": f.get("position"),
                       "schema": f.get("schema"),
                       "enabled": f.get("enabled"),
                       "weight": f.get("weight")}
                      for f in flows if isinstance(f, dict)],
            "flow_count": len(flows)}


def _breakdown(request: Request, period: str, campaign_id: Optional[int]) -> list:
    date_from, date_to = _period_bounds(period)
    filters = {"campaigns": [campaign_id] if campaign_id else [],
               "date_from": date_from, "date_to": date_to}
    return get_report_breakdown(request.state.ch, filters, "campaign_id")


_KEEP_METRICS = ("visits", "clicks", "conversions", "cost", "revenue", "profit",
                 "cr", "epc", "roi", "bot_clicks", "bot_cost")


def _tool_campaigns_metrics(args: _MetricsIn, request: Request, db: Session) -> Any:
    rows = _breakdown(request, args.period, args.campaign_id)
    out = []
    for row in rows:
        m = {k: row.get(k) for k in _KEEP_METRICS}
        out.append({"campaign_id": int(row["dimension"]), **m})
    return out


def _tool_reports_summary(args: _SummaryIn, request: Request, db: Session) -> Any:
    rows = _breakdown(request, args.period, None)
    totals = {"period": args.period, "campaigns": len(rows),
              "visits": 0, "clicks": 0, "conversions": 0, "bot_clicks": 0,
              "cost": 0.0, "revenue": 0.0, "profit": 0.0}
    for row in rows:
        for k in ("visits", "clicks", "conversions", "bot_clicks"):
            totals[k] += int(row.get(k) or 0)
        for k in ("cost", "revenue", "profit"):
            totals[k] += float(row.get(k) or 0)
    totals["cost"], totals["revenue"], totals["profit"] = (
        round(totals[k], 4) for k in ("cost", "revenue", "profit"))
    totals["cr"] = round(totals["conversions"] / totals["clicks"] * 100, 2) \
        if totals["clicks"] else 0.0
    totals["epc"] = round(totals["revenue"] / totals["clicks"], 4) \
        if totals["clicks"] else 0.0
    totals["roi"] = round((totals["revenue"] - totals["cost"]) / totals["cost"] * 100, 2) \
        if totals["cost"] else 0.0
    return totals


def _tool_campaigns_set_status(args: _SetStatusIn, request: Request, db: Session) -> Any:
    if args.status not in STATUSES:
        raise _ToolError(f"Invalid status '{args.status}' — use one of: "
                         + ", ".join(STATUSES))
    c = db.query(CampaignORM).filter_by(id=args.campaign_id).first()
    if not c:
        raise _ToolError(f"Campaign {args.campaign_id} not found")
    c.status = args.status
    db.commit()
    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "campaigns", str(c.id),
                {"mcp": "campaigns.set_status", "status": args.status},
                request.client.host if request.client else "")
    return _campaign_brief(c)


def _tool_offers_list(args: BaseModel, request: Request, db: Session) -> Any:
    return [{"id": o.id, "name": o.name, "url": o.url,
             "affiliate_network_id": o.affiliate_network_id,
             "payout": o.payout} for o in
            db.query(OfferORM).order_by(OfferORM.id.asc()).all()]


def _tool_sources_list(args: BaseModel, request: Request, db: Session) -> Any:
    return [{"id": s.id, "name": s.name,
             "postback_url": (s.additional_settings or {}).get("postback_url", ""),
             "params": (s.additional_settings or {}).get("paramsIdMapping", {})}
            for s in db.query(SourceORM).order_by(SourceORM.id.asc()).all()]


def _tool_conversions_recent(args: _ConversionsIn, request: Request, db: Session) -> Any:
    limit = max(1, min(int(args.limit), 100))
    # Raw SQL is not covered by the ORM's tenant scoping, so the tenant predicate has to be
    # written here explicitly — without it this tool would return every tenant's conversions.
    from tenant_context import current_tenant
    sql = ("SELECT received_at, click_id, campaign_id, offer_id, status, "
           "payout, revenue, external_id FROM conversions_data WHERE tenant_id = :tid")
    params = {"lim": limit, "tid": current_tenant()}
    if args.campaign_id is not None:
        sql += " AND campaign_id = :cid"
        params["cid"] = int(args.campaign_id)
    sql += " ORDER BY received_at DESC LIMIT :lim"
    rows = db.execute(text(sql), params).fetchall()
    return [{"received_at": str(r[0]), "click_id": r[1], "campaign_id": r[2],
             "offer_id": r[3], "status": r[4], "payout": float(r[5] or 0),
             "revenue": float(r[6] or 0), "external_id": r[7]} for r in rows]


def _tool_insights_latest(args: BaseModel, request: Request, db: Session) -> Any:
    from app_pages.insights import _load_main_settings, SETTINGS_BLOCK, run_analysis
    block = (_load_main_settings(db).get(SETTINGS_BLOCK) or {})
    if not block.get("run_at"):
        block = run_analysis("mcp")
    block = dict(block)
    block.pop("critical_ids", None)
    return block


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def _obj(**props) -> dict:
    return {"type": "object", "properties": props,
            "additionalProperties": False}


TOOLS = [
    {"name": "campaigns.list",
     "description": "List all campaigns (id, name, alias, type, status, tags). "
                    "Pass include_archived=true to also list archived ones.",
     "inputSchema": _obj(include_archived={"type": "boolean", "default": False}),
     "handler": _tool_campaigns_list, "args": _CampaignsListIn},
    {"name": "campaigns.get",
     "description": "Get one campaign by id, including a flow summary.",
     "inputSchema": _obj(campaign_id={"type": "integer"}),
     "handler": _tool_campaigns_get, "args": _CampaignsGetIn},
    {"name": "campaigns.metrics",
     "description": "Per-campaign performance metrics (visits, clicks, "
                    "conversions, cost, revenue, profit, CR, EPC, ROI, bot "
                    "share) for a period: today, 7d, 30d or all.",
     "inputSchema": _obj(period={"type": "string", "enum": list(PERIODS),
                                 "default": "7d"},
                         campaign_id={"type": "integer"}),
     "handler": _tool_campaigns_metrics, "args": _MetricsIn},
    {"name": "campaigns.set_status",
     "description": "Pause or activate a campaign (status: 'active' or "
                    "'paused'). The only mutating MCP tool; audit-logged.",
     "inputSchema": _obj(campaign_id={"type": "integer"},
                         status={"type": "string", "enum": list(STATUSES)}),
     "handler": _tool_campaigns_set_status, "args": _SetStatusIn},
    {"name": "offers.list",
     "description": "List all offers (id, name, url, affiliate network, payout).",
     "inputSchema": _obj(),
     "handler": _tool_offers_list, "args": _NoArgs},
    {"name": "sources.list",
     "description": "List all traffic sources with their postback URL template "
                    "and token (paramsIdMapping) configuration.",
     "inputSchema": _obj(),
     "handler": _tool_sources_list, "args": _NoArgs},
    {"name": "reports.summary",
     "description": "Account-wide totals (visits, clicks, conversions, cost, "
                    "revenue, profit, CR, EPC, ROI) for a period: today, 7d, "
                    "30d or all.",
     "inputSchema": _obj(period={"type": "string", "enum": list(PERIODS),
                                 "default": "7d"}),
     "handler": _tool_reports_summary, "args": _SummaryIn},
    {"name": "conversions.recent",
     "description": "Latest conversions (time, click id, campaign, offer, "
                    "status, payout, revenue, external id), newest first.",
     "inputSchema": _obj(limit={"type": "integer", "default": 20, "maximum": 100},
                         campaign_id={"type": "integer"}),
     "handler": _tool_conversions_recent, "args": _ConversionsIn},
    {"name": "insights.latest",
     "description": "Latest anomaly-insights run: per-campaign anomaly findings "
                    "(ctr_drop, cost_spike, click_drop, bot_surge, revenue_stop, "
                    "zero_conversion_spend) with severity and magnitude.",
     "inputSchema": _obj(),
     "handler": _tool_insights_latest, "args": _NoArgs},
]
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------

def _rpc_result(rpc_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def _rpc_error(rpc_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                         "error": {"code": code, "message": message}})


def _tool_result(payload: Any, is_error: bool = False) -> dict:
    text_out = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return {"content": [{"type": "text", "text": text_out}], "isError": is_error}


def _dispatch_tool(name: str, arguments: dict, request: Request,
                   db: Session) -> dict:
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise _RpcMethodError(-32602, f"Unknown tool '{name}'")
    if not isinstance(arguments, dict):
        raise _RpcMethodError(-32602, "Invalid params: 'arguments' must be an object")
    try:
        args = tool["args"](**(arguments or {}))
    except ValidationError as e:
        raise _RpcMethodError(-32602, f"Invalid arguments: {e.errors()[:3]}")
    try:
        payload = tool["handler"](args, request, db)
        return _tool_result(payload)
    except _ToolError as e:
        return _tool_result(str(e), is_error=True)


class _RpcMethodError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@router.post("/")
@router.post("")
async def mcp_endpoint(request: Request, db: Session = Depends(get_db)):
    try:
        envelope = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error: body is not valid JSON")

    # JSON-RPC notification: no id -> no response body per the spec.
    if isinstance(envelope, dict) and "id" not in envelope and envelope.get("method"):
        return JSONResponse(status_code=202, content=None)

    if not isinstance(envelope, dict) or not envelope.get("method"):
        return _rpc_error(envelope.get("id") if isinstance(envelope, dict) else None,
                          -32600, "Invalid Request: missing method")
    rpc_id, method, params = envelope.get("id"), envelope["method"], \
        envelope.get("params") or {}

    if method == "initialize":
        return _rpc_result(rpc_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "aaa-tracker", "version": __version__},
            "instructions": "AAA Tracker MCP. Use the tools to inspect campaigns, "
                            "metrics, reports, conversions and insights. Only "
                            "campaigns.set_status can mutate state.",
        })
    if method == "ping":
        return _rpc_result(rpc_id, {})
    if method == "tools/list":
        return _rpc_result(rpc_id, {"tools": [
            {"name": t["name"], "description": t["description"],
             "inputSchema": t["inputSchema"]} for t in TOOLS]})
    if method == "tools/call":
        if not isinstance(params, dict) or not params.get("name"):
            return _rpc_error(rpc_id, -32602, "Invalid params: tools/call "
                                              "requires {name, arguments}")
        try:
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return _rpc_error(rpc_id, -32602,
                                  "Invalid params: 'arguments' must be an object")
            result = _dispatch_tool(params["name"], arguments, request, db)
        except _RpcMethodError as e:
            return _rpc_error(rpc_id, e.code, e.message)
        return _rpc_result(rpc_id, result)

    return _rpc_error(rpc_id, -32601, f"Method not found: {method}")
