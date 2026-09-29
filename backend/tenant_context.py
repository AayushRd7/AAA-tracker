"""Request-scoped tenant context.

The current tenant is stored in a ``contextvars.ContextVar`` rather than being
threaded through every function signature:

* Starlette/anyio propagate the context down into the threadpool that runs the
  sync endpoints and into the tasks a middleware spawns, so a value set once by
  ``TenantContextMiddleware`` (backend/app.py) is visible everywhere in the
  request.
* The default is tenant 1 — this install. Anything that runs *outside* a
  request (the monitor / auto-rules / optimizer / email-report background
  loops) therefore behaves exactly as it did before multi-tenancy: it sees and
  writes tenant 1's rows only.

``tenant_scope.py`` consumes this value to scope every ORM statement; see the
comment there for why raw SQL is NOT covered by that mechanism.
"""
import contextvars
from typing import Optional

DEFAULT_TENANT_ID = 1

_current_tenant: contextvars.ContextVar[int] = contextvars.ContextVar(
    "aaa_current_tenant", default=DEFAULT_TENANT_ID)

# True when the authenticated caller exists but holds no tenant_memberships row.
# The middleware sets it; auth.require_api_auth turns it into a 403.
_tenant_missing: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "aaa_tenant_missing", default=False)


def set_current_tenant(tenant_id: Optional[int]):
    """Set the tenant for this request/context; returns a reset token."""
    _tenant_missing.set(False)
    return _current_tenant.set(int(tenant_id) if tenant_id else DEFAULT_TENANT_ID)


def reset_current_tenant(token) -> None:
    _current_tenant.reset(token)


def set_tenant_missing(missing: bool) -> None:
    _tenant_missing.set(bool(missing))


def tenant_missing() -> bool:
    return _tenant_missing.get()


def current_tenant() -> int:
    return _current_tenant.get()
