from sqlalchemy import Column, Integer
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class TenantMixin:
    """Marks a mapped class as tenant-owned.

    Every table that holds a tenant's own data carries a ``tenant_id`` column
    and inherits from this mixin. The mixin is both a schema declaration (the
    Column is copied onto each subclass) and the handle ``tenant_scope.py``
    uses to attach a ``with_loader_criteria`` filter to every ORM SELECT,
    UPDATE and DELETE, plus a ``before_flush`` hook that stamps ``tenant_id``
    on new rows.

    The column deliberately carries **no** SQL default: an INSERT that omits
    ``tenant_id`` must fail the NOT NULL constraint rather than silently land in
    tenant 1. Raw SQL callers must pass ``tenant_id`` explicitly (the audited
    sites all do); the ORM path is stamped by the ``before_flush`` hook above.
    """
    tenant_id = Column(Integer, nullable=False, index=True)
