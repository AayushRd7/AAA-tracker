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
    # User-defined report columns: [{name, formula}] over the metric whitelist.
    # They live in the workspace block (tenant-scoped with the settings row) and
    # are evaluated by the report builder's formula engine.
    "custom_columns": [],
    # When true the report breakdown renders as a grouped view (one subtotal row
    # per first-level group) instead of the flat expandable tree.
    "grouping_view": False,
}


def load_custom_columns(db) -> list:
    """The workspace's user-defined report columns, read straight from the
    settings document.

    Read-only and lenient: the report builder evaluates these on every breakdown,
    including for a reader who does not hold the settings permission, and a
    malformed stored entry is skipped rather than failing the report (the save
    path already rejects bad input).
    """
    row = db.query(SettingsORM).filter_by(name="settings").first()
    if not row or not row.value:
        return []
    try:
        cfg = json.loads(row.value) or {}
    except Exception:
        return []
    workspace = cfg.get("workspace") if isinstance(cfg.get("workspace"), dict) else {}
    out = []
    for item in (workspace.get("custom_columns") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        formula = str(item.get("formula") or "").strip()
        if name and formula:
            out.append({"name": name, "formula": formula})
    return out


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
