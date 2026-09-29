from sqlalchemy import Column, Integer, String, UniqueConstraint
from models.base import Base, TenantMixin

class SettingsORM(TenantMixin, Base):
    """Per-tenant settings rows (phase 1: one 'settings'/'subIdMapping' row for
    tenant 1, a second tenant starts with its own copies)."""
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("tenant_id", "name",
                                       name="settings_tenant_name_key"),)

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    value = Column(String, nullable=False)
