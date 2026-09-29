"""Per-tenant settings defaults, seeding and the tenant registry helpers.

Phase 1 left every tenant without a settings document: a new workspace's reads
fell back to whatever the reader does with a missing row, and the API token
stayed tenant 1's. This module owns the three pieces phase 2B adds:

``DEFAULT_SETTINGS`` / ``seed_tenant_settings``
    The documented default document (``install/sql/init.sql`` seeds exactly
    these keys for tenant 1). A newly created tenant gets its own copy — its
    own fresh ``apiToken``, never tenant 1's values.

``list_tenant_ids`` / ``for_each_tenant``
    The background loops (monitor, auto rules, optimizer, insights, email
    reports, Meta cost sync) run for every active tenant inside
    ``set_current_tenant`` so the ORM and ClickHouse scoping do the rest. One
    tenant's failure is logged and never aborts the sweep.

``tenant_features`` / ``tenant_feature``
    ``tenants.features`` is a small JSONB flag map. A flag is only ever
    *explicitly* false to disable a capability; absent means enabled (every
    existing workspace carries ``{}`` and must behave as before).

Raw SQL throughout: ``tenants`` is deliberately install-global (no
``TenantMixin``), so the ORM's tenant filter must not apply here.
"""
import json
import secrets

from sqlalchemy import text

from db import SessionLocal
from tenant_context import reset_current_tenant, set_current_tenant

# The documented defaults — the exact keys install/sql/init.sql writes for
# tenant 1's settings row, minus the shared token (each tenant mints its own).
DEFAULT_SETTINGS = {
    "domain": "",
    "currency": "USD",
    "timezone": "UTC",
    "autoUpdateReports": True,
    "enableLogging": False,
}


def new_api_token() -> str:
    """A fresh workspace API token (the credential on the Settings page)."""
    return secrets.token_urlsafe(24)


def seed_tenant_settings(db, tenant_id: int) -> bool:
    """Seed a tenant's settings document when it does not exist yet.

    Idempotent: an existing row is left untouched. Written with an explicit
    ``tenant_id`` through raw SQL because ``before_flush`` stamps new ORM rows
    with the *current* tenant — the caller creating workspace N is still inside
    workspace 1. Returns True when this call created the row.
    """
    tid = int(tenant_id)
    exists = db.execute(text("SELECT 1 FROM settings WHERE name = 'settings' "
                             "AND tenant_id = :t"), {"t": tid}).fetchone()
    if exists:
        return False
    doc = dict(DEFAULT_SETTINGS)
    doc["apiToken"] = new_api_token()
    db.execute(text(
        "INSERT INTO settings (name, value, tenant_id) "
        "VALUES ('settings', :v, :t) "
        "ON CONFLICT (tenant_id, name) DO NOTHING"),
        {"v": json.dumps(doc), "t": tid})
    return True


def list_tenant_ids(active_only: bool = True) -> list:
    """Every tenant id, ascending. ``active_only`` skips suspended workspaces."""
    db = SessionLocal()
    try:
        sql = "SELECT id FROM tenants"
        if active_only:
            sql += " WHERE status = 'active'"
        sql += " ORDER BY id ASC"
        return [int(r[0]) for r in db.execute(text(sql)).fetchall()]
    except Exception:
        return []
    finally:
        db.close()


def tenant_features(tenant_id: int) -> dict:
    """The tenant's ``features`` JSONB map (``{}`` when absent/unreadable)."""
    db = SessionLocal()
    try:
        row = db.execute(text("SELECT features FROM tenants WHERE id = :t"),
                         {"t": int(tenant_id)}).fetchone()
        features = row[0] if row else None
        if isinstance(features, str):
            features = json.loads(features)
        return features if isinstance(features, dict) else {}
    except Exception:
        return {}
    finally:
        db.close()


def tenant_feature(db, tenant_id: int, name: str, default: bool = True) -> bool:
    """Whether a tenant has a feature flag enabled.

    Absent flag = enabled: every workspace created before feature flags carries
    ``{}`` and must keep today's behaviour. Only an explicit ``false`` denies.
    """
    tid = int(tenant_id)
    try:
        row = db.execute(text("SELECT features FROM tenants WHERE id = :t"),
                         {"t": tid}).fetchone()
    except Exception:
        return default
    features = row[0] if row else None
    if isinstance(features, str):
        try:
            features = json.loads(features)
        except Exception:
            return default
    if not isinstance(features, dict) or name not in features:
        return default
    return bool(features[name])


async def for_each_tenant(work) -> None:
    """Run ``await work(tenant_id)`` for every active tenant, tenant-scoped.

    The contextvar is set before the per-tenant work and reset after it, so the
    ORM/ClickHouse scoping (and any ``asyncio.to_thread`` hop) sees the right
    workspace. Exceptions are logged and swallowed: one broken workspace must
    not stop the others in the same sweep.
    """
    for tenant_id in list_tenant_ids():
        token = set_current_tenant(tenant_id)
        try:
            await work(tenant_id)
        except Exception as e:  # noqa: BLE001 — a sweep must survive one tenant
            print(f"tenant sweep error (tenant {tenant_id}):", e)
        finally:
            reset_current_tenant(token)
