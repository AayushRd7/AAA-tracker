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

    ``server_default="1"`` keeps direct SQL inserts (the tracking plane, the
    background loops) landing in tenant 1 instead of failing the NOT NULL.
    """
    tenant_id = Column(Integer, nullable=False, server_default="1", index=True)
