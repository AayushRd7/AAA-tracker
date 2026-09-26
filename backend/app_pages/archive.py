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
            text(f"UPDATE {table} SET archived = :a WHERE id = :i"),
            {"a": archived, "i": row_id})
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
            row = db.execute(text("SELECT owner_id FROM campaigns WHERE id = :i"),
                             {"i": row_id}).fetchone()
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
        rows = db.execute(text(f"SELECT id FROM {table} WHERE archived = true")).fetchall()
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


@audit_router.get("/")
def read_audit(user: str = "", action: str = "", entity: str = "",
               page: int = 1, page_size: int = 50, db: Session = Depends(get_db)):
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    where, params = [], {}
    if user:
        where.append("username = :u")
        params["u"] = user
    if action:
        where.append("action = :a")
        params["a"] = action
    if entity:
        where.append("entity = :e")
        params["e"] = entity
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.execute(text(f"SELECT count(*) FROM audit_log{clause}"), params).scalar()
    rows = db.execute(
        text(f"SELECT id, at, username, action, entity, entity_id, detail, ip "
             f"FROM audit_log{clause} ORDER BY id DESC LIMIT :lim OFFSET :off"),
        dict(params, lim=page_size, off=(page - 1) * page_size)).fetchall()
    return {"total": total, "page": page, "page_size": page_size,
            "entries": [{"id": r[0], "at": r[1].isoformat() if r[1] else None,
                         "username": r[2], "action": r[3], "entity": r[4],
                         "entity_id": r[5], "detail": r[6], "ip": r[7]} for r in rows]}
