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
from app_pages.settings import _settings_rev

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
    raw = (row.value or "") if row else ""
    cfg = {}
    if raw:
        try:
            cfg = json.loads(raw) or {}
        except Exception:
            cfg = {}
    workspace = cfg.get("workspace") if isinstance(cfg.get("workspace"), dict) else {}
    # Same optimistic-concurrency token the Settings page uses, hashed from the
    # same raw stored value so the two endpoints agree: the pages that write the
    # workspace block back (Logs / Reports default columns) echo it, so a page
    # whose workspace never loaded cannot overwrite the stored one.
    return {"workspace": {**WORKSPACE_DEFAULTS, **(workspace or {})},
            "settings_rev": _settings_rev(raw)}
