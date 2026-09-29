"""Central tenant scoping for the SQLAlchemy ORM.

Two session-level hooks implement "a caller cannot forget the tenant":

``do_orm_execute``
    Every ORM SELECT / UPDATE / DELETE gets
    ``with_loader_criteria(TenantMixin, cls.tenant_id == current_tenant())``
    appended. A row of another tenant is therefore invisible even to
    ``.filter(Model.id == <id of another tenant's row>)`` — it resolves to no
    row, which is what makes cross-tenant "fetch by id" 404 instead of leak.

``before_flush``
    New objects that inherit ``TenantMixin`` are stamped with the current
    tenant, *overriding* whatever the caller put in ``tenant_id``. A crafted
    payload cannot plant a row in another tenant.

LIMITS — read before extending this module
------------------------------------------
* Raw SQL is **NOT** covered. `session.execute(text("SELECT ... FROM
  campaigns"))` and `conn.query(...)` bypass the ORM entirely, so every raw
  statement that touches a tenant-owned table must carry its own
  ``tenant_id = :tenant_id`` predicate / ``tenant_id`` column. That is why the
  raw sites in `app_pages/` were audited by hand; grep for ``text(`` and for
  ``tenant_id`` when adding new ones. Same for the asyncpg SQL in
  ``frontend/app.py`` (the tracking plane) — it never imports this module.
* ``session.bulk_insert_mappings`` / ``bulk_save_objects`` bypass
  ``before_flush``; such rows fall back to the column's ``server_default``
  (tenant 1). No caller uses the bulk APIs on a tenant-owned table today.
* Statements issued with ``execution_options(synchronize_session=False)`` on a
  Core ``update()``/``delete()`` built from a *text* clause are still raw SQL.
* ``with_loader_criteria`` cannot help with a JOIN to a table whose model does
  not inherit ``TenantMixin`` (users, auth_sessions, tenants,
  tenant_memberships, oauth_states, totp_token_used — all deliberately
  install-global in phase 1). Those joins must be filtered on the tenanted
  side.
"""
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from models.base import TenantMixin
from tenant_context import current_tenant


@event.listens_for(Session, "do_orm_execute")
def _scope_statement_to_tenant(execute_state):
    if not (execute_state.is_select or execute_state.is_update or execute_state.is_delete):
        return
    # Column / relationship loads re-issue a statement for the whole entity
    # set of an already-scoped parent; re-applying the criteria there is both
    # unnecessary and (for a lazy load of a scalar) invalid.
    if execute_state.is_column_load or execute_state.is_relationship_load:
        return
    tenant_id = current_tenant()
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantMixin,
            lambda cls: cls.tenant_id == tenant_id,
            include_aliases=True,
        )
    )


@event.listens_for(Session, "before_flush")
def _stamp_new_rows_with_tenant(session, flush_context, instances):
    tenant_id = current_tenant()
    for obj in session.new:
        if isinstance(obj, TenantMixin):
            # Deliberately overrides a caller-supplied value: an INSERT can
            # never target another tenant.
            obj.tenant_id = tenant_id
