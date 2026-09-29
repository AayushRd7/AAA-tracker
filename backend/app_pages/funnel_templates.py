"""Funnel templates — reusable multi-step funnel configurations.

A template stores a campaign editor's Funnel tab step list verbatim
(``{name, landing, offers, schema}`` objects). Applying one copies the steps
into the current campaign form; the campaign itself is not saved.
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from db import get_db

router = APIRouter()


class FunnelTemplateIn(BaseModel):
    name: str
    steps: list = []


def _template_public(r) -> dict:
    steps = r[2]
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except Exception:
            steps = []
    return {"id": r[0], "name": r[1], "steps": steps or [],
            "created_at": r[3].isoformat() if r[3] else None}


def _get_template(db: Session, template_id: int):
    r = db.execute(text("SELECT * FROM funnel_templates WHERE id = :i"),
                   {"i": template_id}).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Funnel template not found")
    return r


@router.get("/")
def list_templates(db: Session = Depends(get_db)):
    rows = db.execute(text("SELECT * FROM funnel_templates ORDER BY name ASC, id ASC")).fetchall()
    return {"templates": [_template_public(r) for r in rows]}


@router.post("/")
def create_template(payload: FunnelTemplateIn, request: Request,
                    db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    name = str(payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Template name is required")
    steps = payload.steps if isinstance(payload.steps, list) else []
    caller, _ = get_caller(request)
    r = db.execute(text("""
        INSERT INTO funnel_templates (name, steps)
        VALUES (:n, CAST(:s AS JSONB)) RETURNING id"""),
        {"n": name, "s": json.dumps(steps)}).fetchone()
    db.commit()
    audit_event(caller or "api_token", "create", "funnel_templates", str(r[0]),
                {"name": name, "steps": len(steps)},
                request.client.host if request.client else "")
    return {"template": _template_public(_get_template(db, r[0]))}


@router.delete("/{template_id}")
def delete_template(template_id: int, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    from auth import get_caller
    _get_template(db, template_id)
    db.execute(text("DELETE FROM funnel_templates WHERE id = :i"), {"i": template_id})
    db.commit()
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "delete", "funnel_templates", str(template_id),
                ip=request.client.host if request.client else "")
    return {"message": "Funnel template deleted"}
