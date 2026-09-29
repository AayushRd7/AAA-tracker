"""Saved filter presets — named filter sets per view.

Each preset carries a ``scope`` (e.g. ``logs-postbacks``, ``logs-clicks``,
``reports``) so a view only ever lists the presets that belong to it. The
``filters`` JSONB stores the view's filter state verbatim.
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db

router = APIRouter()


class PresetIn(BaseModel):
    name: str
    scope: str
    filters: Optional[dict] = None


def _preset_public(r) -> dict:
    filters = r[3]
    if isinstance(filters, str):
        try:
            filters = json.loads(filters)
        except Exception:
            filters = {}
    return {"id": r[0], "name": r[1], "scope": r[2], "filters": filters or {},
            "created_by": r[4],
            "created_at": r[5].isoformat() if r[5] else None}


def _get_preset(db: Session, preset_id: int):
    r = db.execute(text("SELECT * FROM filter_presets WHERE id = :i"),
                   {"i": preset_id}).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Preset not found")
    return r


@router.get("/")
def list_presets(scope: Optional[str] = None, db: Session = Depends(get_db)):
    query = "SELECT * FROM filter_presets"
    params = {}
    if scope:
        query += " WHERE scope = :s"
        params["s"] = scope
    query += " ORDER BY name ASC, id ASC"
    rows = db.execute(text(query), params).fetchall()
    return {"presets": [_preset_public(r) for r in rows]}


@router.post("/")
def create_preset(payload: PresetIn, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    name = str(payload.name or "").strip()
    scope = str(payload.scope or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Preset name is required")
    if not scope:
        raise HTTPException(status_code=400, detail="Preset scope is required")
    caller, _ = get_caller(request)
    r = db.execute(text("""
        INSERT INTO filter_presets (name, scope, filters, created_by)
        VALUES (:n, :s, CAST(:f AS JSONB), :b) RETURNING id"""),
        {"n": name, "s": scope, "f": json.dumps(payload.filters or {}),
         "b": caller or "api_token"}).fetchone()
    db.commit()
    audit_event(caller or "api_token", "create", "filter_presets", str(r[0]),
                {"name": name, "scope": scope},
                request.client.host if request.client else "")
    return {"preset": _preset_public(_get_preset(db, r[0]))}


@router.put("/{preset_id}")
def update_preset(preset_id: int, payload: dict, request: Request,
                  db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    r = _get_preset(db, preset_id)
    merged = _preset_public(r)
    merged.update({k: v for k, v in payload.items() if k in ("name", "scope", "filters")})
    name = str(merged.get("name") or "").strip()
    scope = str(merged.get("scope") or "").strip()
    if not name or not scope:
        raise HTTPException(status_code=400, detail="Preset name and scope are required")
    db.execute(text("""
        UPDATE filter_presets SET name = :n, scope = :s, filters = CAST(:f AS JSONB)
        WHERE id = :i"""),
        {"n": name, "s": scope, "f": json.dumps(merged.get("filters") or {}), "i": preset_id})
    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "filter_presets", str(preset_id),
                {"fields": list(payload.keys())},
                request.client.host if request.client else "")
    return {"preset": _preset_public(_get_preset(db, preset_id))}


@router.delete("/{preset_id}")
def delete_preset(preset_id: int, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    _get_preset(db, preset_id)
    db.execute(text("DELETE FROM filter_presets WHERE id = :i"), {"i": preset_id})
    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "delete", "filter_presets", str(preset_id),
                ip=request.client.host if request.client else "")
    return {"message": "Preset deleted"}
