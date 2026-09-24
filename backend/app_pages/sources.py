from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from db import get_db
from models.sources import SourceORM
from models.settings import SettingsORM

from pydantic import BaseModel
from typing import Optional, List, Dict, Any

router = APIRouter()


def _param(name, parameter, token="", editable_name=False):
    """Shorthand for building one paramsIdMapping-style entry."""
    return {"name": name, "parameter": parameter, "token": token, "editable_name": editable_name}


# Built-in traffic source presets, shipped out of the box.
# Each carries the real pass-through macros the ad network uses so the
# tracker can pick up campaign/site/keyword data out of the box.
SOURCE_PRESETS = [
    {"name": "Facebook Ads", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{{campaign.name}}"),
        _param("Creative ID", "utm_creative", "{{ad.name}}"),
        _param("Site", "utm_source", "{{site_source_name}}"),
        _param("Placement", "placement", "{{placement}}"),
        _param("Keyword", "keyword", ""),
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Google Ads", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaignid}"),
        _param("Creative ID", "utm_creative", "{creative}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Site", "utm_source", "{placement}"),
        _param("Device", "device", "{device}"),
        _param("Match Type", "matchtype", "{matchtype}"),
        _param("Google Click ID", "gclid", "{gclid}"),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "TikTok Ads", "settings": [
        _param("AD Campaign ID", "utm_campaign", "__CAMPAIGN_NAME__"),
        _param("Creative ID", "utm_creative", "__AID_NAME__"),
        _param("Site", "utm_source", "__CID_NAME__"),
        _param("Placement", "placement", "__PLACEMENT__"),
        _param("Keyword", "keyword", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Taboola", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Site", "utm_source", "{site}"),
        _param("Creative ID", "utm_creative", "{thumbnail}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
        _param("Sub id 1", "sub_id_1", "", True),
    ]},
    {"name": "Outbrain", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Site", "utm_source", "{publisher_name}"),
        _param("Creative ID", "utm_creative", "{ad_title}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
        _param("Sub id 1", "sub_id_1", "", True),
    ]},
    {"name": "PropellerAds", "settings": [
        _param("AD Campaign ID", "utm_campaign", "${CAMPAIGN_ID}"),
        _param("Site", "utm_source", "${SUBSOURCE_ID}"),
        _param("Sub id 1", "sub_id_1", "${SUBID}"),
        _param("Sub id 2", "sub_id_2", "${ZONE_ID}", True),
        _param("Cost", "cost", "${COST}"),
        _param("Keyword", "keyword", "${KEYWORD}"),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "ExoClick", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{variation_id}", True),
        _param("Category", "category", "{category}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "MGID", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Site", "utm_source", "{teaser_source}"),
        _param("Creative ID", "utm_creative", "{teaser_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", ""),
        _param("Sub id 1", "sub_id_1", "", True),
    ]},
    {"name": "Revcontent", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{widget_id}"),
        _param("Creative ID", "utm_creative", "{content_id}"),
        _param("Site", "utm_source", "{source}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Zeropark", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaignid}"),
        _param("Sub id 1", "sub_id_1", "{source}"),
        _param("Sub id 2", "sub_id_2", "{target}", True),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{cid}"),
    ]},
    {"name": "HilltopAds", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Adsterra", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{subid}"),
        _param("Sub id 2", "sub_id_2", "{zone_id}", True),
        _param("Site", "utm_source", "{source_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Clickadu", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaignid}"),
        _param("Sub id 1", "sub_id_1", "{zoneid}"),
        _param("Sub id 2", "sub_id_2", "{siteid}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{clickid}"),
    ]},
    {"name": "TrafficJunky", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign}"),
        _param("Creative ID", "utm_creative", "{ad}"),
        _param("Site", "utm_source", "{application}"),
        _param("Keyword", "keyword", "{query}"),
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", "{tjclickid}"),
    ]},
    {"name": "TrafficStars", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "RichAds", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Push.House", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{subscriber_id}", True),
        _param("Site", "utm_source", "{site_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "EvaDav", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "AdMaven", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Kadam", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign}"),
        _param("Sub id 1", "sub_id_1", "{site}"),
        _param("Creative ID", "utm_creative", "{banner}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click}"),
    ]},
    {"name": "PopAds", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaignid}"),
        _param("Sub id 1", "sub_id_1", "{websiteid}"),
        _param("Sub id 2", "sub_id_2", "{popupid}", True),
        _param("Keyword", "keyword", "{keyword}"),
        _param("External ID", "external_id", "{visitor_id}"),
    ]},
    {"name": "Bing Ads (Microsoft)", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{CampaignId}"),
        _param("Creative ID", "utm_creative", "{AdId}"),
        _param("Keyword", "keyword", "{Keyword}"),
        _param("Device", "device", "{Device}"),
        _param("Match Type", "matchtype", "{MatchType}"),
        _param("Bing Click ID", "msclkid", "{msclkid}"),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Push Monkey", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{feed_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Adsterra CPA", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{subid}"),
        _param("Sub id 2", "sub_id_2", "{zone_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "GemForth / Galaksion", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "EpicGameAds", "settings": [
        _param("AD Campaign ID", "utm_campaign", "{campaign}"),
        _param("Sub id 1", "sub_id_1", "{zone}"),
        _param("Sub id 2", "sub_id_2", "{site}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click}"),
    ]},
]


def seed_source_presets(db: Session):
    """Seed built-in traffic source presets exactly once (marker in settings),
    adding any presets missing by name so existing installs get them too."""
    if db.query(SettingsORM).filter_by(name="source_presets_seeded").first():
        return
    existing = {name for (name,) in db.query(SourceORM.name).all()}
    missing = [p for p in SOURCE_PRESETS if p["name"] not in existing]
    if missing:
        db.add_all([SourceORM(**p) for p in missing])
    db.add(SettingsORM(name="source_presets_seeded", value="1"))
    db.commit()


class SourceIn(BaseModel):
    name: str
    traffic_loss: Optional[float] = 0
    s2s_postback: Optional[str] = None
    s2s_postback_statuses: Optional[Dict[str, bool]] = {}
    settings: List[Dict[str, Any]] = []
    additional_settings: Dict[str, Any] = {}


class SourceOut(SourceIn):
    id: int
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    class Config:
        orm_mode = True


@router.get("/", response_model=List[SourceOut])
def get_sources(db: Session = Depends(get_db)):
    seed_source_presets(db)
    return db.query(SourceORM).order_by(SourceORM.id.asc()).all()


@router.post("/", response_model=SourceOut)
def create_source(payload: SourceIn, db: Session = Depends(get_db)):
    source = SourceORM(**payload.dict())
    db.add(source)
    try:
        db.commit()
        db.refresh(source)
        return source
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Source with this name already exists.")


@router.patch("/{source_id}", response_model=SourceOut)
def update_source(source_id: int, payload: SourceIn, db: Session = Depends(get_db)):
    source = db.query(SourceORM).filter(SourceORM.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    for key, value in payload.dict(exclude_unset=True).items():
        setattr(source, key, value)

    db.commit()
    db.refresh(source)
    return source


@router.delete("/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_db)):
    source = db.query(SourceORM).filter(SourceORM.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    db.delete(source)
    db.commit()
    return {"message": "Source deleted"}
