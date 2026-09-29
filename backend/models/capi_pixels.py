"""CAPI integration records.

A pixel is a first-class record (not a single global config): one row per
Platform + Pixel/Dataset. Per-conversion-type event mapping and payout
customisation ride along as JSONB lists. Bindings attach a pixel to a traffic
channel or an offer; the tracking plane resolves the applicable pixels per
conversion and sends each pixel once.
"""
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, ForeignKey, DateTime, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from models.base import Base, TenantMixin


class CapiPixelORM(TenantMixin, Base):
    __tablename__ = "capi_pixels"

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    platform = Column(String(32), nullable=False, default="meta")
    pixel_id = Column(String(255), nullable=False, default="")
    access_token = Column(Text)
    default_event_name = Column(String(100), nullable=False, default="Purchase")
    event_url = Column(Text)
    action_source = Column(String(64), nullable=False, default="website")
    data_quality_token = Column(Text)
    custom_matching = Column(Boolean, nullable=False, default=False)
    conversion_matching = Column(JSONB, nullable=False, default=list)
    payout_customisations = Column(JSONB, nullable=False, default=list)
    status = Column(String(16), nullable=False, default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CapiPixelBindingORM(TenantMixin, Base):
    """Attach a pixel to a traffic channel (scope='channel') or an offer (scope='offer')."""
    __tablename__ = "capi_pixel_bindings"

    id = Column(Integer, primary_key=True)
    pixel_id = Column(Integer, ForeignKey("capi_pixels.id", ondelete="CASCADE"), nullable=False)
    scope = Column(String(16), nullable=False)
    scope_id = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CapiChannelSettingORM(TenantMixin, Base):
    """Per-traffic-channel integration toggles (active gate + impression cost sync)."""
    __tablename__ = "capi_channel_settings"

    source_id = Column(Integer, primary_key=True)
    active = Column(Boolean, nullable=False, default=True)
    impression_cost_sync = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())