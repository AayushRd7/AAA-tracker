"""G66 — Archive & restore (soft-delete) for campaigns and offers, plus the
G65 audit-log read API.

The entity CRUD lives in frozen files, so archiving is implemented here as a
separate mini-router that only flips an `archived` flag on the row. The frozen
list endpoints still return archived rows (their response models don't include
the flag); clients fetch the archived id set from GET /api/archive and filter
client-side. Real deletes remain available on the entity routers.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db, engine, SessionLocal
from audit_logger import audit_event
from tenant_context import current_tenant

import json

router = APIRouter()
audit_router = APIRouter()

ARCHIVABLE = {"campaigns": "campaigns", "offers": "offers"}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _caller(request: Request):
    from auth import get_caller
    return get_caller(request)


def _set_archived(entity: str, row_id: int, archived: bool) -> bool:
    table = ARCHIVABLE[entity]
    with engine.connect() as conn:
        res = conn.execute(
            text(f"UPDATE {table} SET archived = :a WHERE id = :i AND tenant_id = :tid"),
            {"a": archived, "i": row_id, "tid": current_tenant()})
        conn.commit()
        return res.rowcount > 0


def _check_ownership(entity: str, row_id: int, request: Request):
    """Non-admins may only archive/restore campaigns they own (offers have no
    owner — those are admin-only). Raises 403 on violation."""
    from auth import get_caller
    caller, is_admin = _caller(request)
    if is_admin or not caller:
        return
    db = SessionLocal()
    try:
        user = db.execute(text("SELECT id FROM users WHERE username = :u"),
                          {"u": caller}).fetchone()
        if entity == "campaigns":
            if not user:
                raise HTTPException(status_code=403, detail="Not allowed")
            row = db.execute(text("SELECT owner_id FROM campaigns "
                                  "WHERE id = :i AND tenant_id = :tid"),
                             {"i": row_id, "tid": current_tenant()}).fetchone()
            if not row or row[0] != user[0]:
                raise HTTPException(status_code=403,
                                    detail="You can only archive your own campaigns")
        else:
            raise HTTPException(status_code=403, detail="Admin only")
    finally:
        db.close()


@router.get("/")
def list_archived(db: Session = Depends(get_db)):
    out = {}
    for entity, table in ARCHIVABLE.items():
        rows = db.execute(text(f"SELECT id FROM {table} "
                               "WHERE archived = true AND tenant_id = :tid"),
                          {"tid": current_tenant()}).fetchall()
        out[entity] = [r[0] for r in rows]
    return out


@router.post("/{entity}/{row_id}")
def archive(entity: str, row_id: int, request: Request):
    if entity not in ARCHIVABLE:
        raise HTTPException(status_code=404, detail="Unknown entity")
    _check_ownership(entity, row_id, request)
    if not _set_archived(entity, row_id, True):
        raise HTTPException(status_code=404, detail=f"{entity[:-1]} not found")
    caller, _ = _caller(request)
    audit_event(caller or "unknown", "archive", entity, str(row_id), ip=_client_ip(request))
    return {"message": "Archived", "entity": entity, "id": row_id}


@router.post("/{entity}/{row_id}/restore")
def restore(entity: str, row_id: int, request: Request):
    if entity not in ARCHIVABLE:
        raise HTTPException(status_code=404, detail="Unknown entity")
    _check_ownership(entity, row_id, request)
    if not _set_archived(entity, row_id, False):
        raise HTTPException(status_code=404, detail=f"{entity[:-1]} not found")
    caller, _ = _caller(request)
    audit_event(caller or "unknown", "restore", entity, str(row_id), ip=_client_ip(request))
    return {"message": "Restored", "entity": entity, "id": row_id}


def _audit_filters(user, action, entity, q, date_from, date_to):
    """Shared WHERE builder for the audit list and its CSV export."""
    where, params = ["tenant_id = :tid"], {"tid": current_tenant()}
    if user:
        where.append("username = :u")
        params["u"] = user
    if action:
        where.append("action = :a")
        params["a"] = action
    if entity:
        where.append("entity = :e")
        params["e"] = entity
    if q:
        # substring over the entity id and the free-form detail payload
        where.append("(entity_id ILIKE :q OR detail::text ILIKE :q)")
        params["q"] = f"%{q}%"
    if date_from:
        where.append("at >= CAST(:df AS date)")
        params["df"] = date_from
    if date_to:
        # inclusive of the whole end day
        where.append("at < CAST(:dt AS date) + interval '1 day'")
        params["dt"] = date_to
    return where, params


_AUDIT_ORDER_COLS = ("id", "at", "username", "action", "entity", "entity_id", "ip")


@audit_router.get("/")
def read_audit(user: str = "", action: str = "", entity: str = "",
               q: str = "", date_from: str = "", date_to: str = "",
               page: int = 1, page_size: int = 50,
               sort_by: str = "id", sort_desc: bool = True,
               db: Session = Depends(get_db)):
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    where, params = _audit_filters(user, action, entity, q, date_from, date_to)
    # Whitelisted columns only — anything else falls back to the default
    # newest-first ordering so header-sort clicks can never inject SQL.
    order_col = sort_by if sort_by in _AUDIT_ORDER_COLS else "id"
    direction = "DESC" if sort_desc else "ASC"
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(text(f"SELECT count(*) FROM audit_log{clause}"), params).scalar()
    rows = db.execute(
        text(f"SELECT id, at, username, action, entity, entity_id, detail, ip "
             f"FROM audit_log{clause} ORDER BY {order_col} {direction} LIMIT :lim OFFSET :off"),
        dict(params, lim=page_size, off=(page - 1) * page_size)).fetchall()
    return {"total": total, "page": page, "page_size": page_size,
            "entries": [{"id": r[0], "at": r[1].isoformat() if r[1] else None,
                         "username": r[2], "action": r[3], "entity": r[4],
                         "entity_id": r[5], "detail": r[6], "ip": r[7]} for r in rows]}


@audit_router.get("/facets")
def audit_facets(db: Session = Depends(get_db)):
    """Distinct filter values actually present in the log (object types, actions,
    users) so the UI dropdowns stay in sync with real data."""
    tid = {"tid": current_tenant()}
    entities = [r[0] for r in db.execute(text(
        "SELECT DISTINCT entity FROM audit_log WHERE tenant_id = :tid "
        "AND entity <> '' ORDER BY entity"), tid).fetchall()]
    actions = [r[0] for r in db.execute(text(
        "SELECT DISTINCT action FROM audit_log WHERE tenant_id = :tid "
        "AND action <> '' ORDER BY action"), tid).fetchall()]
    users = [r[0] for r in db.execute(text(
        "SELECT DISTINCT username FROM audit_log WHERE tenant_id = :tid "
        "AND username <> '' ORDER BY username LIMIT 200"), tid).fetchall()]
    return {"entities": entities, "actions": actions, "users": users}


def _csv_safe(value):
    """Prefix cells that would start a spreadsheet formula (=,+,-,@) with an
    apostrophe so exported CSVs can't smuggle live formulas into Excel/Sheets."""
    s = "" if value is None else (value if isinstance(value, str) else str(value))
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


@audit_router.get("/export")
def export_audit(user: str = "", action: str = "", entity: str = "",
                 q: str = "", date_from: str = "", date_to: str = "",
                 sort_by: str = "id", sort_desc: bool = True,
                 db: Session = Depends(get_db)):
    """CSV export of the filtered audit set (UTF-8 BOM, formula-guarded)."""
    import csv as _csv
    import io as _io
    from fastapi.responses import Response
    where, params = _audit_filters(user, action, entity, q, date_from, date_to)
    order_col = sort_by if sort_by in _AUDIT_ORDER_COLS else "id"
    direction = "DESC" if sort_desc else "ASC"
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = db.execute(text(
        f"SELECT id, at, username, action, entity, entity_id, detail, ip "
        f"FROM audit_log{clause} ORDER BY {order_col} {direction} LIMIT 10000"),
        params).fetchall()
    buf = _io.StringIO()
    writer = _csv.writer(buf, lineterminator="\n")
    writer.writerow(["id", "at", "username", "action", "entity", "entity_id",
                     "ip", "detail"])
    for r in rows:
        writer.writerow([_csv_safe(v) for v in (
            r[0], r[1].isoformat() if r[1] else "", r[2], r[3], r[4], r[5],
            r[7], json.dumps(r[6]) if r[6] is not None else "")])
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=audit_log.csv"})
