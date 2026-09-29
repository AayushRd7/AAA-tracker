"""Script library — titled tracking/pixel/custom-JS snippets.

Snippets are stored and copied; injection into landers is explicitly out of
scope. The table is created at backend startup (see ``_ensure_wave18_tables``
in app.py); the tracking plane never touches it.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db
from tenant_context import current_tenant

router = APIRouter()


class ScriptIn(BaseModel):
    title: str
    code: str = ""
    description: Optional[str] = None


def _script_public(r) -> dict:
    return {"id": r[0], "title": r[1], "code": r[2], "description": r[3],
            "created_at": r[4].isoformat() if r[4] else None}


def _get_script(db: Session, script_id: int):
    r = db.execute(text("SELECT * FROM scripts WHERE id = :i AND tenant_id = :tid"),
                   {"i": script_id, "tid": current_tenant()}).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Script not found")
    return r


@router.get("/")
def list_scripts(db: Session = Depends(get_db)):
    rows = db.execute(text("SELECT * FROM scripts WHERE tenant_id = :tid "
                           "ORDER BY id ASC"),
                      {"tid": current_tenant()}).fetchall()
    return {"scripts": [_script_public(r) for r in rows]}


@router.post("/")
def create_script(payload: ScriptIn, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    title = str(payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Script title is required")
    r = db.execute(text("""
        INSERT INTO scripts (title, code, description, tenant_id)
        VALUES (:t, :c, :d, :tid) RETURNING id"""),
        {"t": title, "c": payload.code or "", "d": payload.description,
         "tid": current_tenant()}).fetchone()
    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "create", "scripts", str(r[0]),
                {"title": title}, request.client.host if request.client else "")
    return {"script": _script_public(_get_script(db, r[0]))}


@router.put("/{script_id}")
def update_script(script_id: int, payload: dict, request: Request,
                  db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    r = _get_script(db, script_id)
    merged = _script_public(r)
    merged.update({k: v for k, v in payload.items() if k in ("title", "code", "description")})
    title = str(merged.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Script title is required")
    db.execute(text("UPDATE scripts SET title = :t, code = :c, description = :d "
                    "WHERE id = :i AND tenant_id = :tid"),
               {"t": title, "c": merged.get("code") or "",
                "d": merged.get("description"), "i": script_id,
                "tid": current_tenant()})
    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "scripts", str(script_id),
                {"fields": list(payload.keys())},
                request.client.host if request.client else "")
    return {"script": _script_public(_get_script(db, script_id))}


@router.delete("/{script_id}")
def delete_script(script_id: int, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    _get_script(db, script_id)
    db.execute(text("DELETE FROM scripts WHERE id = :i AND tenant_id = :tid"),
               {"i": script_id, "tid": current_tenant()})
    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "delete", "scripts", str(script_id),
                ip=request.client.host if request.client else "")
    return {"message": "Script deleted"}
