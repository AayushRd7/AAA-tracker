"""Read-only view of the workspace display settings.

Number formatting, conditional row colouring and default columns live inside
the shared ``settings`` document (written through the normal settings save path
from Settings → General). This router only exposes that block to the pages that
render data tables, so non-admin users honour the same display preferences
without holding the settings section permission.
"""
import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from db import get_db
from models.settings import SettingsORM

router = APIRouter()

WORKSPACE_DEFAULTS = {
    "decimals": 2,
    "divider": "comma",
    "row_coloring": [],
    "default_columns": {},
}


@router.get("/")
def get_workspace(db: Session = Depends(get_db)):
    row = db.query(SettingsORM).filter_by(name="settings").first()
    cfg = {}
    if row and row.value:
        try:
            cfg = json.loads(row.value) or {}
        except Exception:
            cfg = {}
    workspace = cfg.get("workspace") if isinstance(cfg.get("workspace"), dict) else {}
    return {"workspace": {**WORKSPACE_DEFAULTS, **(workspace or {})}}
