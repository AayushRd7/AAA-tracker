from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Boolean,
    ForeignKey, Enum as SQLAEnum
)
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime

from models.base import Base
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
class CampaignORM(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, unique=True)
    alias = Column(String(255), nullable=False, unique=True)
    type = Column(SQLAEnum(CampaignType), nullable=False, default=CampaignType.campaign)
    status = Column(SQLAEnum(CampaignStatus), nullable=False, default=CampaignStatus.active)
    redirect_mode = Column(SQLAEnum(RedirectMode), nullable=False, default=RedirectMode.position)
    domain_id = Column(Integer, ForeignKey("domains.id"), nullable=True)
    traffic_source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    # D1c — owning user for the campaigns:'own' permission scope
    owner_id = Column(Integer, nullable=True)
    # G66 — soft-delete flag (archive without purging)
    archived = Column(Boolean, nullable=False, default=False)
    notes = Column(Text, nullable=True)
    tags = Column(JSONB, nullable=True, default=list)
    config = Column(JSONB, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
