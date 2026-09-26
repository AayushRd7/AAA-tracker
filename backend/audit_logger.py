"""Audit log helper (G65).

Central write helper for the audit_log table. Instrumented: auth events
(login success/fail, logout, TOTP setup/enable/disable/verify/reset), user CRUD
and permission changes, archive/restore, and entity CRUD across campaigns,
offers, sources, affiliate networks and domains (D1d).
"""
import json

from sqlalchemy import text

from db import SessionLocal

_table_ready = False


def ensure_audit_table():
    global _table_ready
    if _table_ready:
        return
    db = SessionLocal()
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id BIGSERIAL PRIMARY KEY,
            at TIMESTAMP NOT NULL DEFAULT now(),
            username VARCHAR(255) NOT NULL,
            action VARCHAR(64) NOT NULL,
            entity VARCHAR(64) NOT NULL DEFAULT '',
            entity_id VARCHAR(64) NOT NULL DEFAULT '',
            detail JSONB,
            ip VARCHAR(64) NOT NULL DEFAULT ''
        )
    """))
    db.execute(text("CREATE INDEX IF NOT EXISTS audit_log_at_idx ON audit_log (at DESC)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS audit_log_username_idx ON audit_log (username)"))
    db.commit()
    db.close()
    _table_ready = True


def audit_event(username: str, action: str, entity: str = "",
                entity_id: str = "", detail: dict = None, ip: str = ""):
    """Append one row to audit_log. Never raises — auditing must not break requests."""
    try:
        ensure_audit_table()
        db = SessionLocal()
        db.execute(text(
            "INSERT INTO audit_log (username, action, entity, entity_id, detail, ip) "
            "VALUES (:u, :a, :e, :eid, CAST(:d AS JSONB), :ip)"),
            {"u": (username or "")[:255], "a": action[:64], "e": (entity or "")[:64],
             "eid": str(entity_id or "")[:64],
             "d": json.dumps(detail) if detail else None, "ip": (ip or "")[:64]})
        db.commit()
        db.close()
    except Exception:
        pass
