"""Request-scoped tenant context.

The current tenant is stored in a ``contextvars.ContextVar`` rather than being
threaded through every function signature:

* Starlette/anyio propagate the context down into the threadpool that runs the
  sync endpoints and into the tasks a middleware spawns, so a value set once by
  ``TenantContextMiddleware`` (backend/app.py) is visible everywhere in the
  request.
* The default is tenant 1 — this install. Anything that runs *outside* a
  request must set the context explicitly: the background loops enumerate the
  tenants that need work and run each tenant's pass through
  ``tenant_settings.for_each_tenant`` (which also reads/clears this value).

A request carrying a Bearer API token resolves that token to the tenant that
owns it (``_api_token_tenant`` below) and runs with *that* tenant as the
current one — the token can never name another workspace.

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

# The tenant a Bearer API token in this request resolved to (None when the
# request carries no valid token). Set by TenantContextMiddleware before the
# per-tenant context is written, so auth can keep the token inside its own
# workspace and refuse the platform-only escape hatches.
_api_token_tenant: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "aaa_api_token_tenant", default=None)


def set_api_token_tenant(tenant_id: Optional[int]):
    return _api_token_tenant.set(int(tenant_id) if tenant_id else None)


def reset_api_token_tenant(token) -> None:
    _api_token_tenant.reset(token)


def api_token_tenant() -> Optional[int]:
    """The workspace a valid Bearer API token acts in, else None."""
    return _api_token_tenant.get()


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
