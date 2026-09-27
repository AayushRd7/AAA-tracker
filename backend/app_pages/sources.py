from datetime import datetime
import re
from fastapi import APIRouter, Depends, HTTPException, Request
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


# Built-in traffic source presets, shipped out of the box (RedTrack-style
# traffic channels catalog). Each carries the real pass-through macros the ad
# network uses so the tracker can pick up campaign/site/keyword data out of
# the box, plus its API integration capabilities ("cost" = cost update,
# "pause_campaign" = campaign pause, "blacklist" = blacklist placement,
# "pause_creative" = pause creative).
SOURCE_PRESETS = [
    {"name": "Facebook Ads", "capabilities": ["cost", "pause_campaign", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{{campaign.name}}"),
        _param("Creative ID", "utm_creative", "{{ad.name}}"),
        _param("Site", "utm_source", "{{site_source_name}}"),
        _param("Placement", "placement", "{{placement}}"),
        _param("Keyword", "keyword", ""),
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Google Ads", "capabilities": ["cost", "pause_campaign", "blacklist", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaignid}"),
        _param("Creative ID", "utm_creative", "{creative}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Site", "utm_source", "{placement}"),
        _param("Device", "device", "{device}"),
        _param("Match type", "matchtype", "{matchtype}"),
        _param("Google Click ID", "gclid", "{gclid}"),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "TikTok Ads", "capabilities": ["cost", "pause_campaign", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "__CAMPAIGN_NAME__"),
        _param("Creative ID", "utm_creative", "__AID_NAME__"),
        _param("Site", "utm_source", "__CID_NAME__"),
        _param("Placement", "placement", "__PLACEMENT__"),
        _param("Keyword", "keyword", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Taboola", "capabilities": ["cost", "pause_campaign", "blacklist", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Site", "utm_source", "{site}"),
        _param("Creative ID", "utm_creative", "{thumbnail}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
        _param("Sub id 1", "sub_id_1", "", True),
    ]},
    {"name": "Outbrain", "capabilities": ["cost", "pause_campaign", "blacklist", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Site", "utm_source", "{publisher_name}"),
        _param("Creative ID", "utm_creative", "{ad_title}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
        _param("Sub id 1", "sub_id_1", "", True),
    ]},
    {"name": "PropellerAds", "capabilities": ["pause_campaign", "blacklist", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "${CAMPAIGN_ID}"),
        _param("Site", "utm_source", "${SUBSOURCE_ID}"),
        _param("Sub id 1", "sub_id_1", "${SUBID}"),
        _param("Sub id 2", "sub_id_2", "${ZONE_ID}", True),
        _param("Cost", "cost", "${COST}"),
        _param("Keyword", "keyword", "${KEYWORD}"),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "ExoClick", "capabilities": ["cost", "pause_campaign", "blacklist", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{variation_id}", True),
        _param("Category", "category", "{category}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "MGID", "capabilities": ["pause_campaign", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Site", "utm_source", "{teaser_source}"),
        _param("Creative ID", "utm_creative", "{teaser_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", ""),
        _param("Sub id 1", "sub_id_1", "", True),
    ]},
    {"name": "Revcontent", "capabilities": ["cost", "pause_campaign", "blacklist", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{widget_id}"),
        _param("Creative ID", "utm_creative", "{content_id}"),
        _param("Site", "utm_source", "{source}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Zeropark", "capabilities": ["cost", "pause_campaign", "blacklist", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaignid}"),
        _param("Sub id 1", "sub_id_1", "{source}"),
        _param("Sub id 2", "sub_id_2", "{target}", True),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{cid}"),
    ]},
    {"name": "HilltopAds", "capabilities": ["pause_campaign", "blacklist"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Adsterra", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{subid}"),
        _param("Sub id 2", "sub_id_2", "{zone_id}", True),
        _param("Site", "utm_source", "{source_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Clickadu", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaignid}"),
        _param("Sub id 1", "sub_id_1", "{zoneid}"),
        _param("Sub id 2", "sub_id_2", "{siteid}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{clickid}"),
    ]},
    {"name": "TrafficJunky", "capabilities": ["cost", "pause_campaign", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign}"),
        _param("Creative ID", "utm_creative", "{ad}"),
        _param("Site", "utm_source", "{application}"),
        _param("Keyword", "keyword", "{query}"),
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", "{tjclickid}"),
    ]},
    {"name": "TrafficStars", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "RichAds", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Push.House", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{subscriber_id}", True),
        _param("Site", "utm_source", "{site_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "EvaDav", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "AdMaven", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Kadam", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign}"),
        _param("Sub id 1", "sub_id_1", "{site}"),
        _param("Creative ID", "utm_creative", "{banner}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click}"),
    ]},
    {"name": "PopAds", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaignid}"),
        _param("Sub id 1", "sub_id_1", "{websiteid}"),
        _param("Sub id 2", "sub_id_2", "{popupid}", True),
        _param("Keyword", "keyword", "{keyword}"),
        _param("External ID", "external_id", "{visitor_id}"),
    ]},
    {"name": "Bing Ads (Microsoft)", "capabilities": ["cost", "pause_campaign", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{CampaignId}"),
        _param("Creative ID", "utm_creative", "{AdId}"),
        _param("Keyword", "keyword", "{Keyword}"),
        _param("Device", "device", "{Device}"),
        _param("Match type", "matchtype", "{MatchType}"),
        _param("Bing Click ID", "msclkid", "{msclkid}"),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Push Monkey", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{feed_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Adsterra CPA", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{subid}"),
        _param("Sub id 2", "sub_id_2", "{zone_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "GemForth / Galaksion", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "EpicGameAds", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign}"),
        _param("Sub id 1", "sub_id_1", "{zone}"),
        _param("Sub id 2", "sub_id_2", "{site}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click}"),
    ]},
    {"name": "Snapchat Ads", "capabilities": ["cost"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Site", "utm_source", "{publisher_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{snap_id}"),
    ]},
    {"name": "Pinterest Ads", "capabilities": ["cost"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Twitter / X Ads", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{twclid}"),
    ]},
    {"name": "LinkedIn Ads", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{li_fat_id}"),
    ]},
    {"name": "Reddit Ads", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{ad_id}"),
        _param("Site", "utm_source", "{subreddit}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Quora Ads", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{ad_id}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Spotify Ads", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Twitch Ads", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Hotstar Ads", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "JioAds", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "AdRoll", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{ad_id}"),
        _param("Site", "utm_source", "{source}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Criteo", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Site", "utm_source", "{publisher_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "TripleLift", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Site", "utm_source", "{publisher_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "The Trade Desk", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Site", "utm_source", "{publisher_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{ttd_id}"),
    ]},
    {"name": "DV360 (Display & Video 360)", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Site", "utm_source", "{site}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Verizon Media / Yahoo", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "StackAdapt", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Site", "utm_source", "{publisher_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Adform", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{banner_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "PopCash", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaignid}"),
        _param("Sub id 1", "sub_id_1", "{zoneid}"),
        _param("Sub id 2", "sub_id_2", "{siteid}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{visitor_id}"),
    ]},
    {"name": "RichPush", "capabilities": ["cost", "pause_campaign", "blacklist", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{subscriber_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "MegaPush", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{feed_id}"),
        _param("Sub id 2", "sub_id_2", "{widget_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "DaoPush", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Ezmob", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Airpush", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{creative_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "InMobi", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Site", "utm_source", "{site_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Vungle", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Unity Ads", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "AppLovin", "capabilities": ["cost"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Mintegral", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "ironSource", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Chartboost", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Digital Turbine", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Huawei Ads", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Yandex Direct", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{ad_id}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Device", "device", "{device_type}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{yclid}"),
    ]},
    {"name": "MyTarget", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Baidu", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{bd_vid}"),
    ]},
    {"name": "Naver", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{ad_id}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Daum / Kakao", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Buzzoola", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Site", "utm_source", "{site_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Engageya", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{widget_id}"),
        _param("Site", "utm_source", "{publisher_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "AdNow", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{teaser_id}"),
        _param("Site", "utm_source", "{widget_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Content.ad", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{ad_id}"),
        _param("Site", "utm_source", "{widget_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "VK Ads", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{ad_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Apple Search Ads", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{attribution_token}"),
    ]},
    {"name": "MobiAds", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaignid}"),
        _param("Sub id 1", "sub_id_1", "{zoneid}"),
        _param("Sub id 2", "sub_id_2", "{siteid}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{clickid}"),
    ]},
    {"name": "ActiveRevenue", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "AdOperator", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "RollerAds", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{subscriber_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Toro Advertising", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Adblade", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{ad_id}"),
        _param("Site", "utm_source", "{site_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Plugrush", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaignid}"),
        _param("Sub id 1", "sub_id_1", "{zoneid}"),
        _param("Sub id 2", "sub_id_2", "{siteid}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{visitor_id}"),
    ]},
    {"name": "AdCash", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Adnium", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Google Ads (No-redirect tracking)", "capabilities": ["cost", "pause_campaign", "blacklist", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaignid}"),
        _param("Creative ID", "utm_creative", "{creative}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Site", "utm_source", "{placement}"),
        _param("Device", "device", "{device}"),
        _param("Match type", "matchtype", "{matchtype}"),
        _param("Google Click ID", "gclid", "{gclid}"),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Google (PMax only)", "capabilities": ["cost", "pause_campaign"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaignid}"),
        _param("Creative ID", "utm_creative", "{creative}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Site", "utm_source", "{placement}"),
        _param("Device", "device", "{device}"),
        _param("Match type", "matchtype", "{matchtype}"),
        _param("Google Click ID", "gclid", "{gclid}"),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Meta (ex Facebook)", "capabilities": ["cost", "pause_campaign", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{{campaign.name}}"),
        _param("Creative ID", "utm_creative", "{{ad.name}}"),
        _param("Site", "utm_source", "{{site_source_name}}"),
        _param("Placement", "placement", "{{placement}}"),
        _param("Keyword", "keyword", ""),
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "TikTok", "capabilities": ["cost", "pause_campaign", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "__CAMPAIGN_NAME__"),
        _param("Creative ID", "utm_creative", "__AID_NAME__"),
        _param("Site", "utm_source", "__CID_NAME__"),
        _param("Placement", "placement", "__PLACEMENT__"),
        _param("Keyword", "keyword", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "BingAds", "capabilities": ["cost", "pause_campaign", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{CampaignId}"),
        _param("Creative ID", "utm_creative", "{AdId}"),
        _param("Keyword", "keyword", "{Keyword}"),
        _param("Device", "device", "{Device}"),
        _param("Match type", "matchtype", "{MatchType}"),
        _param("Bing Click ID", "msclkid", "{msclkid}"),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "ChatGPT Ads", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "TrafficForce", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "PopADS", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaignid}"),
        _param("Sub id 1", "sub_id_1", "{websiteid}"),
        _param("Sub id 2", "sub_id_2", "{popupid}", True),
        _param("Keyword", "keyword", "{keyword}"),
        _param("External ID", "external_id", "{visitor_id}"),
    ]},
    {"name": "MobFox", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "LeadBolt", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Inmobi", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Site", "utm_source", "{site_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Avazu", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "AirPush", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{creative_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Adcash", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "AdSimilate", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "AdamoAds", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "PlugRush", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaignid}"),
        _param("Sub id 1", "sub_id_1", "{zoneid}"),
        _param("Sub id 2", "sub_id_2", "{siteid}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{visitor_id}"),
    ]},
    {"name": "VisitWeb", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "SelfAdvertiser.com", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Popcash", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaignid}"),
        _param("Sub id 1", "sub_id_1", "{zoneid}"),
        _param("Sub id 2", "sub_id_2", "{siteid}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{visitor_id}"),
    ]},
    {"name": "PropellerAds.com", "capabilities": ["pause_campaign", "blacklist", "pause_creative"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "${CAMPAIGN_ID}"),
        _param("Site", "utm_source", "${SUBSOURCE_ID}"),
        _param("Sub id 1", "sub_id_1", "${SUBID}"),
        _param("Sub id 2", "sub_id_2", "${ZONE_ID}", True),
        _param("Cost", "cost", "${COST}"),
        _param("Keyword", "keyword", "${KEYWORD}"),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "PPCMate", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Ligatus", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Tonic", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "RoyalAds", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Mobusi", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "StartApp", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "HasTraffic", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Yengo", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "ReklamStore DSP", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "MegaPu.sh", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Reach Effect", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Adskeeper", "capabilities": ["pause_campaign", "blacklist", "pause_creative"], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Datspush", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Advertizer", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Global Network", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Appreciate DSP", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Ad2games", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "PocketMath", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "UngAds", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Snapchat", "capabilities": ["cost"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Site", "utm_source", "{publisher_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{snap_id}"),
    ]},
    {"name": "Reddit", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{ad_id}"),
        _param("Site", "utm_source", "{subreddit}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "RTX Platform", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Advertise.com", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Pocketmath Mobile DSP", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Pushground", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "For publishers", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Yeesshh", "capabilities": ["pause_campaign", "blacklist", "pause_creative"], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Yahoo Gemini", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "BidVertiser", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Admixer", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Clickadilla", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "LuckyAds", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Oblivki.biz", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "ADxAD", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Pinterest", "capabilities": ["cost"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Keyword", "keyword", "{keyword}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Twin Red", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Dao Ad", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "Noviclick", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Nomads", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "RichPops", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "ExplorAds Pop", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "ExplorAds Push", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "EZmob", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Sub id 1", "sub_id_1", "{zone_id}"),
        _param("Sub id 2", "sub_id_2", "{site_id}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "AdHub", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Adtelligent", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Adavice DSP", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Adscompass", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Epom DSP", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "TacoLoco", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Mondiad", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Adavice Media", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "AWIN", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Traforama", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "MyBid", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Pushub", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "OnClickA", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Adport", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Targeleon", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "MediaGo", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Newsbreak", "capabilities": ["cost"], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "KWAI", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "RoiAds (push traffic)", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "RoiAds (pop traffic)", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Traffic Factory", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Mobidea Push", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "SolAds Media", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Juicy Ads", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Octoclick", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Applovin", "capabilities": ["cost"], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign_id}"),
        _param("Creative ID", "utm_creative", "{creative_id}"),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click_id}"),
    ]},
    {"name": "BIGO Ads", "capabilities": ["cost"], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Rumble", "capabilities": ["cost"], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Clickaine", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "EpicAds.net", "capabilities": [], "settings": [
        _param("Ad campaign ID", "utm_campaign", "{campaign}"),
        _param("Sub id 1", "sub_id_1", "{zone}"),
        _param("Sub id 2", "sub_id_2", "{site}", True),
        _param("Cost", "cost", "{cost}"),
        _param("External ID", "external_id", "{click}"),
    ]},
    {"name": "Mobplus", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Klaviyo", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Kayzen", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "RiverTraffic (CPC campaigns)", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "RiverTraffic (CPM campaigns: no costs)", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Vrume", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Decide", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "FatAds", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "7SearchPPC", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "TrafficHaus", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "SmartNews", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "TrafficHunt", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Whop", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
    {"name": "Other", "capabilities": [], "settings": [
        _param("Sub id 1", "sub_id_1", "", True),
        _param("Sub id 2", "sub_id_2", "", True),
        _param("Sub id 3", "sub_id_3", ""),
        _param("Cost", "cost", ""),
        _param("External ID", "external_id", ""),
    ]},
]


# Brand domains for catalog logos (favicons served via /api/sources/favicon).
# Well-known mappings first; presets whose name IS a domain auto-derive.
_SOURCE_LOGO_DOMAINS = {
    "Facebook Ads": "facebook.com", "Meta (ex Facebook)": "meta.com",
    "Google Ads": "google.com", "Google (PMax only)": "google.com",
    "Google Ads (No-redirect tracking)": "google.com",
    "Bing Ads (Microsoft)": "bing.com", "BingAds": "bing.com",
    "TikTok Ads": "tiktok.com", "TikTok": "tiktok.com",
    "Taboola": "taboola.com", "Outbrain": "outbrain.com",
    "Revcontent": "revcontent.com", "MGID": "mgid.com",
    "PropellerAds": "propellerads.com", "PropellerAds.com": "propellerads.com",
    "ExoClick": "exoclick.com", "Zeropark": "zeropark.com",
    "PopADS": "popads.net", "Popcash": "popcash.net",
    "Adsterra": "adsterra.com", "HilltopAds": "hilltopads.com",
    "Adcash": "adcash.com", "PopAds": "popads.net",
    "TrafficStars": "trafficstars.com", "TrafficJunky": "trafficjunky.com",
    "TrafficForce": "trafficforce.com", "Traffic Factory": "trafficfactory.com",
    "TrafficHaus": "traffichaus.com", "TrafficHunt": "affilight.com",
    "RichPush": "richpush.com", "RichPops": "richpops.com",
    "Clickadu": "clickadu.com", "Clickadilla": "clickadilla.com",
    "Adskeeper": "adskeeper.com", "AdMaven": "admaven.com",
    "AdOperator": "adoperator.com", "AdNow": "adnow.com",
    "Adnium": "adnium.com", "Adform": "adform.com",
    "Avazu": "avazu.com", "AirPush": "airpush.com",
    "LeadBolt": "leadbolt.com", "Inmobi": "inmobi.com",
    "MobFox": "mobfox.com", "Mobusi": "mobusi.com",
    "StartApp": "startapp.com", "Unity Ads": "unity.com",
    "AppLovin": "applovin.com", "Applovin": "applovin.com",
    "Vungle": "vungle.com", "Chartboost": "chartboost.com",
    "Snapchat": "snapchat.com", "Pinterest": "pinterest.com",
    "Reddit": "reddit.com", "X (Twitter)": "x.com",
    "KWAI": "kwai.com", "BIGO Ads": "bigo.tv",
    "Rumble": "rumble.com", "Newsbreak": "newsbreak.com",
    "SmartNews": "smartnews.com", "Criteo": "criteo.com",
    "MyTarget": "target.my.com",
    "PlugRush": "plugrush.com", "EroAdvertising": "eroadvertising.com",
    "ReklamStore DSP": "reklamstore.com", "Epom DSP": "epom.com",
    "Admixer": "admixer.com", "Adtelligent": "adtelligent.com",
    "Mondiad": "mondiad.com", "RollerAds": "rollerads.com",
    "Pushground": "pushground.com", "Push.House": "push.house",
    "Datspush": "datspush.com", "EZmob": "ezmob.com",
    "TacoLoco": "tacoloco.com", "MyBid": "mybid.io",
    "Mobidea Push": "mobidea.com", "Yeesshh": "yeesshh.com",
    "Content.ad": "content.ad", "Ligatus": "ligatus.com",
    "Adknowledge": "adknowledge.com", "Sedo": "sedo.com",
    "Tonic": "tonic.com", "Awin": "awin.com",
    "Impact": "impact.com", "Everflow": "everflow.io",
    "TUNE (ex HasOffers)": "tune.com", "ChatGPT Ads": "openai.com",
}
# Curated full-logo overrides (favicons are tiny; some brands deserve better).
# When set, the catalog uses this URL directly instead of the favicon proxy.
_SOURCE_LOGO_URLS = {
    "Ad2games": "https://files.startupranking.com/startup/thumb/59015_fc7ff7df388c24b028de73095f314dc93eac6179_ad2games_l.png",
}
_SOURCE_POSTBACK_TEMPLATE = "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"
# Sources whose conversion postback is keyed on a sub-id rather than the
# external-id slot (verified against each platform's postback docs pattern).
_SOURCE_POSTBACK_MACRO_OVERRIDES = {
    "PropellerAds": "${SUBID}",
    "PopAds": "${SUBID}",
}


def _source_preset_postback(preset: dict) -> str:
    """Ready-to-hand-to-the-source S2S postback: the tracker's /pb/ URL with
    the SOURCE's own click-id macro in place of {click_id}. Empty when the
    preset doesn't map a click macro (better blank than a wrong URL). The
    YOUR-TRACKER-DOMAIN placeholder is resolved by the UI at copy/create."""
    name = preset.get("name", "")
    macro = _SOURCE_POSTBACK_MACRO_OVERRIDES.get(name, "")
    if not macro:
        for param in preset.get("settings", []):
            if param.get("parameter") == "external_id" and param.get("token"):
                macro = param["token"]
                break
    if not macro:
        return ""
    return _SOURCE_POSTBACK_TEMPLATE.replace("{click_id}", macro)


for _preset in SOURCE_PRESETS:
    _name = _preset["name"]
    if _name in _SOURCE_LOGO_URLS:
        _preset["logo_url"] = _SOURCE_LOGO_URLS[_name]
    if _name in _SOURCE_LOGO_DOMAINS:
        _preset["logo_domain"] = _SOURCE_LOGO_DOMAINS[_name]
    elif re.search(r"[a-z0-9-]+\.[a-z]{2,}", _name, re.I):
        # Names that already look like domains (SelfAdvertiser.com etc.)
        _m = re.search(r"([a-z0-9-]+(?:\.[a-z0-9-]+)+)", _name, re.I)
        _preset["logo_domain"] = _m.group(1).lower()
    # else: no logo_domain → UI renders the initial-letter fallback
    _preset["postback"] = _source_preset_postback(_preset)


def seed_source_presets(db: Session):
    """Backfill built-in traffic source presets by name (idempotent), so both
    fresh installs and existing ones end up with the full catalog. Only the
    name/settings are stored on the row; capabilities live on SOURCE_PRESETS
    and are served by the /presets endpoint."""
    existing_names = {name for (name,) in db.query(SourceORM.name).all()}
    missing = [p for p in SOURCE_PRESETS if p["name"] not in existing_names]
    if missing:
        db.add_all([SourceORM(name=p["name"], settings=p["settings"]) for p in missing])
    if not db.query(SettingsORM).filter_by(name="source_presets_seeded").first():
        db.add(SettingsORM(name="source_presets_seeded", value="1"))
    db.commit()


@router.get("/presets")
def get_source_presets():
    """The full template catalog with API integration capabilities,
    alphabetical with "Other" pinned last."""
    presets = sorted(
        ({"name": p["name"], "capabilities": p["capabilities"],
          "logo_domain": p.get("logo_domain"), "logo_url": p.get("logo_url"),
          "postback": p.get("postback"), "params": p["settings"]}
         for p in SOURCE_PRESETS),
        key=lambda p: (p["name"].lower() == "other", p["name"].lower()),
    )
    return {"presets": presets}


@router.get("/favicon/{domain}")
def get_source_favicon(domain: str):
    """Brand favicon proxy — reuses the affiliates module's implementation
    (shared in-memory cache, empty-PNG 200 on miss) so the browser never
    logs failed-resource console errors for missing logos."""
    from app_pages.affiliates import get_favicon as _aff_favicon
    return _aff_favicon(domain)

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
def create_source(payload: SourceIn, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    source = SourceORM(**payload.dict())
    db.add(source)
    try:
        db.commit()
        db.refresh(source)
        from auth import get_caller
        caller, _ = get_caller(request)
        audit_event(caller or "api_token", "create", "sources", str(source.id),
                    {"name": source.name},
                    request.client.host if request.client else "")
        return source
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Source with this name already exists.")


@router.patch("/{source_id}", response_model=SourceOut)
def update_source(source_id: int, payload: SourceIn, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    source = db.query(SourceORM).filter(SourceORM.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    changed = []
    for key, value in payload.dict(exclude_unset=True).items():
        if getattr(source, key, None) != value:
            changed.append(key)
        setattr(source, key, value)

    db.commit()
    db.refresh(source)
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "sources", str(source_id),
                {"fields": changed}, request.client.host if request.client else "")
    return source


@router.delete("/{source_id}")
def delete_source(source_id: int, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    source = db.query(SourceORM).filter(SourceORM.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    db.delete(source)
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "delete", "sources", str(source_id),
                {"name": source.name}, request.client.host if request.client else "")
    return {"message": "Source deleted"}
