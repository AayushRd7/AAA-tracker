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
            ip VARCHAR(64) NOT NULL DEFAULT '',
            tenant_id INTEGER NOT NULL DEFAULT 1
        )
    """))
    db.execute(text("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS "
                    "tenant_id INTEGER"))
    db.execute(text("UPDATE audit_log SET tenant_id = 1 WHERE tenant_id IS NULL"))
    db.execute(text("ALTER TABLE audit_log ALTER COLUMN tenant_id SET DEFAULT 1"))
    db.execute(text("ALTER TABLE audit_log ALTER COLUMN tenant_id SET NOT NULL"))
    db.execute(text("CREATE INDEX IF NOT EXISTS audit_log_at_idx ON audit_log (at DESC)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS audit_log_username_idx ON audit_log (username)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS audit_log_tenant_id_idx ON audit_log (tenant_id)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS audit_log_tenant_at_idx ON audit_log (tenant_id, at)"))
    db.commit()
    db.close()
    _table_ready = True


def audit_event(username: str, action: str, entity: str = "",
                entity_id: str = "", detail: dict = None, ip: str = ""):
    """Append one row to audit_log. Never raises — auditing must not break requests.

    Raw SQL, so the ORM tenant scoping does not apply here: tenant_id is taken
    from the request context explicitly (tenant_context), defaulting to tenant 1
    for background work.
    """
    try:
        from tenant_context import current_tenant
        ensure_audit_table()
        db = SessionLocal()
        db.execute(text(
            "INSERT INTO audit_log (username, action, entity, entity_id, detail, ip, tenant_id) "
            "VALUES (:u, :a, :e, :eid, CAST(:d AS JSONB), :ip, :t)"),
            {"u": (username or "")[:255], "a": action[:64], "e": (entity or "")[:64],
             "eid": str(entity_id or "")[:64],
             "d": json.dumps(detail) if detail else None, "ip": (ip or "")[:64],
             "t": current_tenant()})
        db.commit()
        db.close()
    except Exception:
        pass
