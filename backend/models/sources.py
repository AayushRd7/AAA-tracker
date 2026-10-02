# models/sources.py

from sqlalchemy import Column, Integer, String, Float, Text, JSON, TIMESTAMP, func, UniqueConstraint
from models.base import Base, TenantMixin

class SourceORM(TenantMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("tenant_id", "name",
                                       name="sources_tenant_name_key"),)

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    traffic_loss = Column(Float, default=0)
    s2s_postback = Column(String(1024), nullable=True)
    s2s_postback_statuses = Column(JSON, default={})

    settings = Column(JSON, default=[])
    additional_settings = Column(JSON, default={})

    # Owner scope parity with campaigns.owner_id: the sources:'own' permission
    # limits a caller to traffic sources they own.
    owner_id = Column(Integer, nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
