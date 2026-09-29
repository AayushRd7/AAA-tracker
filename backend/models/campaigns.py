from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Boolean,
    ForeignKey, Enum as SQLAEnum, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime

from models.base import Base, TenantMixin
import enum

# === ENUM TYPES ===
class CampaignType(str, enum.Enum):
    campaign = 'campaign'
    tracking_only = 'tracking_only'

class CampaignStatus(str, enum.Enum):
    active = 'active'
    paused = 'paused'

class RedirectMode(str, enum.Enum):
    position = 'position'
    weight = 'weight'

# === MODEL ===
class CampaignORM(TenantMixin, Base):
    __tablename__ = "campaigns"
    # name/alias are unique per tenant, not globally (a second tenant may reuse
    # an alias). The composite constraints are created by init.sql / the
    # startup migration; declared here so the model matches the schema.
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="campaigns_tenant_name_key"),
        UniqueConstraint("tenant_id", "alias", name="campaigns_tenant_alias_key"),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    alias = Column(String(255), nullable=False)
    type = Column(SQLAEnum(CampaignType), nullable=False, default=CampaignType.campaign)
    status = Column(SQLAEnum(CampaignStatus), nullable=False, default=CampaignStatus.active)
    redirect_mode = Column(SQLAEnum(RedirectMode), nullable=False, default=RedirectMode.position)
    domain_id = Column(Integer, ForeignKey("domains.id"), nullable=True)
    traffic_source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    # D1c — owning user for the campaigns:'own' permission scope
    owner_id = Column(Integer, nullable=True)
    # Meta Ads cost auto-sync — the platform's campaign id (primary match key)
    ad_platform_campaign_id = Column(String(64), nullable=True)
    # G66 — soft-delete flag (archive without purging)
    archived = Column(Boolean, nullable=False, default=False)
    notes = Column(Text, nullable=True)
    tags = Column(JSONB, nullable=True, default=list)
    config = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
