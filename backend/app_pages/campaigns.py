from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from db import get_db
from models.campaigns import CampaignORM
from models.user import UserORM
from typing import List

from pydantic import BaseModel
from typing import Optional, Literal
from datetime import datetime, date, timedelta
from sqlalchemy import text

from clickHouse import get_clickhouse_client, get_report_breakdown

router = APIRouter()

class CampaignIn(BaseModel):
    name: str
    alias: str
    type: Literal['campaign', 'tracking_only'] = 'campaign'
    status: Literal['active', 'paused', 'archived'] = 'active'
    redirect_mode: Literal['position', 'weight'] = 'position'
    traffic_source_id: Optional[int] = None
    domain_id: Optional[int] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    config: Optional[dict] = None


class BulkTagsIn(BaseModel):
    ids: List[int]
    tags: List[str]
    mode: Literal['add', 'replace'] = 'add'

class BulkIn(BaseModel):
    ids: List[int]
    action: Literal['tags_add', 'tags_remove', 'archive', 'delete']
    tags: Optional[List[str]] = None

class CampaignImportIn(BaseModel):
    lines: str

class CampaignOut(CampaignIn):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


def _client_ip(request: Request) -> str:
    return request.client.host if request and request.client else ""


def _owner_scope(request: Request, db: Session, query):
    """G63/D1c: a non-admin whose permissions carry campaigns:'own' only sees
    campaigns they own; everyone else (admins, default users) sees all."""
    from auth import get_caller
    username, is_admin = get_caller(request)
    if is_admin or not username:
        return query
    user = db.query(UserORM).filter(UserORM.username == username).first()
    raw = (user.permissions or {}) if user else {}
    if raw.get("campaigns") == "own":
        query = query.filter(CampaignORM.owner_id == user.id)
    return query


def _require_mutation_access(request: Request, db: Session, campaigns) -> None:
    """G63/D1c: campaigns:'own' users may only mutate campaigns they own —
    enforced on every mutation path (PUT/DELETE/clone/tags/bulk/import),
    not just list/export/metrics. Admins and regular users pass."""
    from auth import get_caller
    username, is_admin = get_caller(request)
    if is_admin or not username:
        return
    user = db.query(UserORM).filter(UserORM.username == username).first()
    raw = (user.permissions or {}) if user else {}
    if raw.get("campaigns") != "own":
        return
    for campaign in campaigns:
        if campaign is not None and campaign.owner_id != user.id:
            raise HTTPException(status_code=403,
                                detail="You can only modify your own campaigns")


def _purge_campaign(db: Session, ch, campaign_id: int) -> None:
    """Purge the tracking history — otherwise orphaned clicks keep feeding
    dashboard/report numbers for a campaign that no longer exists."""
    try:
        ch.command(
            "ALTER TABLE clicks_data DELETE WHERE campaign_id = %(campaign_id)s",
            {"campaign_id": int(campaign_id)})
    except Exception as e:
        print(f"campaign delete: ClickHouse purge failed for {campaign_id}:", e)

    db.execute(text("DELETE FROM conversions_data WHERE campaign_id = :cid"),
               {"cid": campaign_id})


@router.get("/", response_model=List[CampaignOut])
def get_campaigns(request: Request, db: Session = Depends(get_db)):
    query = db.query(CampaignORM).order_by(CampaignORM.id.asc())
    return _owner_scope(request, db, query).all()


@router.get("/metrics")
def get_campaign_metrics(
    request: Request,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Per-campaign live metric columns from ClickHouse.

    Returns {campaign_id: {clicks, conversions, cost, revenue, profit, cr, epc, roi, ...}}.
    """
    filters = {"campaigns": [], "date_from": date_from, "date_to": date_to}
    try:
        rows = get_report_breakdown(request.state.ch, filters, "campaign_id")
    except Exception:
        return {}
    visible = {c.id for c in _owner_scope(request, db, db.query(CampaignORM)).all()}
    return {row["dimension"]: row for row in rows if int(row["dimension"]) in visible}


@router.post("/{campaign_id}/clone", response_model=dict)
def clone_campaign(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    """Duplicate a campaign — new alias derived from the original."""
    from audit_logger import audit_event
    campaign = db.query(CampaignORM).filter(CampaignORM.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    _require_mutation_access(request, db, [campaign])

    base_alias = f"{campaign.alias}-copy"
    alias = base_alias
    n = 2
    while db.query(CampaignORM).filter(CampaignORM.alias == alias).first():
        alias = f"{base_alias}-{n}"
        n += 1

    clone = CampaignORM(
        name=f"{campaign.name} (copy)",
        alias=alias,
        type=campaign.type,
        status='paused',
        redirect_mode=campaign.redirect_mode,
        traffic_source_id=campaign.traffic_source_id,
        domain_id=campaign.domain_id,
        notes=campaign.notes,
        tags=campaign.tags,
        config=campaign.config,
        owner_id=campaign.owner_id,
    )
    db.add(clone)
    db.commit()
    db.refresh(clone)
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "create", "campaigns", str(clone.id),
                {"cloned_from": campaign_id}, _client_ip(request))
    return {"message": "Campaign cloned", "id": clone.id, "alias": clone.alias}

@router.post("/", response_model=dict)
def create_campaign(data: CampaignIn, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)
    owner_id = None
    if caller:
        user = db.query(UserORM).filter(UserORM.username == caller).first()
        owner_id = user.id if user else None
    campaign = CampaignORM(**data.dict(), owner_id=owner_id)
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    audit_event(caller or "api_token", "create", "campaigns", str(campaign.id),
                {"name": campaign.name}, _client_ip(request))
    return {"message": "Campaign created", "id": campaign.id}

@router.patch("/{campaign_id}/tags", response_model=CampaignOut)
def set_campaign_tags(campaign_id: int, data: dict, request: Request, db: Session = Depends(get_db)):
    """Replace one campaign's tag set."""
    from audit_logger import audit_event
    campaign = db.query(CampaignORM).filter(CampaignORM.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    _require_mutation_access(request, db, [campaign])

    raw = data.get("tags") or []
    campaign.tags = sorted({str(t).strip() for t in raw if str(t).strip()})
    campaign.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(campaign)
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "campaigns", str(campaign.id),
                {"fields": ["tags"]}, _client_ip(request))
    return campaign


@router.post("/bulk/tags", response_model=dict)
def bulk_set_tags(data: BulkTagsIn, request: Request, db: Session = Depends(get_db)):
    """Apply a tag set to many campaigns at once — add merges, replace overwrites."""
    from audit_logger import audit_event
    clean = sorted({str(t).strip() for t in data.tags if str(t).strip()})
    campaigns = db.query(CampaignORM).filter(CampaignORM.id.in_(data.ids)).all()
    _require_mutation_access(request, db, campaigns)
    for campaign in campaigns:
        if data.mode == 'replace':
            campaign.tags = list(clean)
        else:
            campaign.tags = sorted(set(campaign.tags or []) | set(clean))
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "campaigns", ",".join(map(str, data.ids)),
                {"bulk": "tags", "mode": data.mode, "tags": clean}, _client_ip(request))
    return {"message": f"Tags applied to {len(campaigns)} campaigns", "updated": len(campaigns)}


@router.post("/bulk", response_model=dict)
def bulk_campaigns(data: BulkIn, request: Request, db: Session = Depends(get_db)):
    """G67 bulk actions: tags_add / tags_remove / archive / delete over a set of ids."""
    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)
    campaigns = db.query(CampaignORM).filter(CampaignORM.id.in_(data.ids)).all()
    _require_mutation_access(request, db, campaigns)
    clean = sorted({str(t).strip() for t in (data.tags or []) if str(t).strip()})

    if data.action == 'tags_add':
        for c in campaigns:
            c.tags = sorted(set(c.tags or []) | set(clean))
        db.commit()
        detail = {"bulk": "tags_add", "tags": clean, "count": len(campaigns)}
    elif data.action == 'tags_remove':
        for c in campaigns:
            c.tags = sorted(set(c.tags or []) - set(clean))
        db.commit()
        detail = {"bulk": "tags_remove", "tags": clean, "count": len(campaigns)}
    elif data.action == 'archive':
        for c in campaigns:
            c.archived = True
        db.commit()
        detail = {"bulk": "archive", "count": len(campaigns)}
    elif data.action == 'delete':
        ch = request.state.ch
        for c in campaigns:
            _purge_campaign(db, ch, c.id)
            db.delete(c)
        db.commit()
        detail = {"bulk": "delete", "count": len(campaigns)}
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action '{data.action}'")

    audit_event(caller or "api_token", "update" if data.action != "delete" else "delete",
                "campaigns", ",".join(map(str, data.ids)), detail, _client_ip(request))
    return {"message": f"Bulk {data.action} applied to {len(campaigns)} campaigns",
            "updated": len(campaigns)}


CAMPAIGN_EXPORT_FIELDS = ["id", "name", "alias", "type", "status", "redirect_mode",
                          "domain_id", "traffic_source_id", "tags", "notes"]


def _csv_safe(value):
    """Prefix cells that would start a spreadsheet formula (=,+,-,@) with an
    apostrophe so exported CSVs can't smuggle live formulas into Excel/Sheets.
    str-subclass values (e.g. CampaignType) are used as-is — str() on a
    (str, Enum) member yields 'CampaignType.campaign', breaking round-trips."""
    s = "" if value is None else (value if isinstance(value, str) else str(value))
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


@router.get("/export")
def export_campaigns(request: Request, db: Session = Depends(get_db)):
    """G67 CSV export of every visible campaign column (tags ;-separated)."""
    import csv as _csv
    import io as _io
    from fastapi.responses import Response
    rows = _owner_scope(request, db, db.query(CampaignORM)).order_by(CampaignORM.id.asc()).all()
    buf = _io.StringIO()
    writer = _csv.writer(buf, lineterminator="\n")
    writer.writerow(CAMPAIGN_EXPORT_FIELDS)
    for c in rows:
        def _plain(v):
            return v.value if hasattr(v, "value") else v
        writer.writerow([
            _csv_safe(v) for v in (
                c.id, c.name, c.alias, _plain(c.type), _plain(c.status),
                _plain(c.redirect_mode),
                c.domain_id or "", c.traffic_source_id or "",
                ";".join(c.tags or []), c.notes or "",
            )
        ])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=campaigns.csv"})


@router.post("/import", response_model=dict)
def import_campaigns(data: CampaignImportIn, request: Request, db: Session = Depends(get_db)):
    """G67 CSV import — one campaign per line, matched to an existing row by id
    (preferred) or name; everything else is created. Never fails the batch:
    every line reports its own result."""
    import csv as _csv
    import io as _io
    from audit_logger import audit_event
    from auth import get_caller
    caller, _ = get_caller(request)

    results = []
    rows = [r for r in _csv.reader(_io.StringIO(data.lines or ""))
            if r and any(str(c).strip() for c in r)]
    if not rows:
        return {"results": [], "imported": 0, "failed": 0}

    first = [str(h).strip().lower() for h in rows[0]]
    if "name" in first:
        header = first
        body = [(i + 2, r) for i, r in enumerate(rows[1:])]  # real file line numbers
    else:
        # no header row — assume the export column order
        header = CAMPAIGN_EXPORT_FIELDS
        body = [(i + 1, r) for i, r in enumerate(rows)]

    def cell(row, key):
        try:
            return str(row[header.index(key)]).strip()
        except (ValueError, IndexError):
            return ""

    def cell_plain(row, key):
        v = cell(row, key)
        if "." in v:
            v = v.rsplit(".", 1)[-1]
        return v

    def parse_tags(raw):
        return sorted({t.strip() for t in raw.replace(",", ";").split(";") if t.strip()})

    created = 0
    for line_no, row in body:
        name = cell(row, "name")
        if not name:
            results.append({"line": line_no, "ok": False, "detail": "missing name"})
            continue
        camp_id = cell(row, "id")
        campaign = None
        if camp_id and camp_id.isdigit():
            campaign = db.query(CampaignORM).filter(CampaignORM.id == int(camp_id)).first()
        if campaign is None:
            campaign = db.query(CampaignORM).filter(CampaignORM.name == name).first()

        fields = {
            "name": name,
            "type": cell_plain(row, "type") or "campaign",
            "status": cell_plain(row, "status") or "active",
            "redirect_mode": cell_plain(row, "redirect_mode") or "position",
            "domain_id": int(cell(row, "domain_id")) if cell(row, "domain_id").isdigit() else None,
            "traffic_source_id": int(cell(row, "traffic_source_id")) if cell(row, "traffic_source_id").isdigit() else None,
            "notes": cell(row, "notes") or None,
            "tags": parse_tags(cell(row, "tags")),
        }
        if fields["type"] not in ("campaign", "tracking_only"):
            results.append({"line": line_no, "ok": False,
                            "detail": f"invalid type '{fields['type']}'"})
            continue
        if fields["status"] not in ("active", "paused"):
            fields["status"] = "active"
        if fields["redirect_mode"] not in ("position", "weight"):
            fields["redirect_mode"] = "position"

        if campaign is not None:
            _require_mutation_access(request, db, [campaign])
            for key, value in fields.items():
                setattr(campaign, key, value)
            campaign.updated_at = datetime.utcnow()
            db.commit()
            results.append({"line": line_no, "ok": True,
                            "detail": f"updated campaign {campaign.id}"})
        else:
            alias = cell(row, "alias")
            if not alias or db.query(CampaignORM).filter(CampaignORM.alias == alias).first():
                alias = f"imp-{int(datetime.utcnow().timestamp() * 1000) % 10**10:x}-{line_no}"
            owner = db.query(UserORM).filter(UserORM.username == caller).first() if caller else None
            campaign = CampaignORM(
                name=name, alias=alias,
                config={"flows": [], "postbacks": [], "hide_referrer": False,
                        "fallback_url": ""},
                owner_id=owner.id if owner else None,
                **{k: v for k, v in fields.items() if k != "name"})
            db.add(campaign)
            try:
                db.commit()
                created += 1
                results.append({"line": line_no, "ok": True,
                                "detail": f"created campaign {campaign.id}"})
            except Exception as e:
                db.rollback()
                results.append({"line": line_no, "ok": False, "detail": f"create failed: {e}"})

    audit_event(caller or "api_token", "update", "campaigns", "import",
                {"created": created, "lines": len(results)}, _client_ip(request))
    ok_count = sum(1 for r in results if r["ok"])
    return {"results": results, "imported": ok_count, "failed": len(results) - ok_count}


@router.put("/{campaign_id}", response_model=CampaignOut)
def update_campaign(campaign_id: int, data: CampaignIn, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    campaign = db.query(CampaignORM).filter(CampaignORM.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    _require_mutation_access(request, db, [campaign])

    # Only touch fields the caller actually sent — omitted fields (e.g. tags
    # edited from elsewhere) keep their current value.
    changed = []
    for key, value in data.dict(exclude_unset=True).items():
        if getattr(campaign, key, None) != value:
            changed.append(key)
        setattr(campaign, key, value)
    campaign.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(campaign)
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "campaigns", str(campaign.id),
                {"fields": changed}, _client_ip(request))
    return campaign

@router.delete("/{campaign_id}")
def delete_campaign(campaign_id: int, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    campaign = db.query(CampaignORM).filter_by(id=campaign_id).first()
    if not campaign:
        raise HTTPException(404, detail="Campaign not found")
    _require_mutation_access(request, db, [campaign])

    _purge_campaign(db, request.state.ch, campaign_id)

    db.delete(campaign)
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "delete", "campaigns", str(campaign_id),
                {"name": campaign.name}, _client_ip(request))
    return {"message": "Campaign deleted"}
