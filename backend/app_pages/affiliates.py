from fastapi import APIRouter, Depends, HTTPException, Request, Response
from typing import List
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

import base64
import re

import httpx

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from db import get_db
from models.affiliate_networks import AffiliateNetworkORM
from models.offers import OfferORM  # imported for the offer check

router = APIRouter()

# Built-in affiliate network presets, shipped out of the box.
# {click_id} is replaced by the network's subid macro value.
_POSTBACK = "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"


def _p(name, offer_parameters, verticals, logo_domain):
    return {"name": name, "offer_parameters": offer_parameters, "s2s_postback": _POSTBACK,
            "verticals": verticals, "logo_domain": logo_domain}


NETWORK_PRESETS = [
    # Legacy / existing presets (kept intact, enriched with verticals + logo)
    _p("MaxBounty", "s1={click_id}", ["Dating", "Installs", "Forex / Binary"], "maxbounty.com"),
    _p("Admitad", "subid={click_id}", ["Adult", "E-commerce", "Travel", "Games", "Financial", "Mobile Apps"], "admitad.com"),
    _p("Adsterra", "s2s={click_id}", [], "adsterra.com"),
    _p("Affise", "sub1={click_id}", ["Technology"], "affise.com"),
    _p("ClickDealer", "s2={click_id}", ["Gambling", "E-commerce", "Installs"], "clickdealer.com"),
    _p("CrakRevenue", "aff_sub={click_id}", ["Gambling", "Adult", "Dating", "Sweepstakes", "Nutra", "Games", "Financial", "Mobile Apps"], "crakrevenue.com"),
    _p("CPAGrip", "tracking_id={click_id}", ["E-commerce", "Financial", "Real Estate", "Technology"], "cpagrip.com"),
    _p("CPAlead", "subid={click_id}", [], "cpalead.com"),
    _p("Dr.Cash", "sub1={click_id}", ["Adult", "Nutra", "Health and Fitness"], "dr.cash"),
    _p("Everad", "sid1={click_id}", ["Nutra", "Health and Fitness"], "everad.com"),
    _p("HasOffers", "aff_sub={click_id}", ["Technology"], "tune.com"),
    _p("Leadbit", "sub1={click_id}", ["Gambling", "Pin-submits", "Adult", "Dating", "Sweepstakes", "Nutra"], "leadbit.com"),
    _p("Mobidea", "tag={click_id}", ["Pin-submits", "Dating", "E-commerce"], "mobidea.com"),
    _p("MyLead", "ml_sub1={click_id}", ["Gambling", "Adult", "Travel", "Education", "Health and Fitness", "Sports", "Technology"], "mylead.io"),
    _p("Zeydoo", "ymid={click_id}", [], "zeydoo.com"),
    _p("T3Leads", "subid={click_id}", [], "t3leads.com"),
    _p("ClickBank", "tid={click_id}", ["Gambling", "Travel", "Games", "Education", "Financial", "Health and Fitness", "Mobile Apps", "Religion and Spirituality", "Sports"], "clickbank.com"),
    # Catalog additions
    _p("3snet", "sub1={click_id}", ["Gambling"], "3snet.io"),
    _p("29Next", "evclid={click_id}", [], "29next.com"),
    _p("Ad2games", "sub_id={click_id}", ["Games"], "ad2games.com"),
    _p("Adcombo", "clickid={click_id}", ["Leadgen", "E-commerce", "Mobile Apps"], "adcombo.com"),
    _p("AdCyd", "sub_id={click_id}", [], "adcyid.com"),
    _p("AddWell", "sub_id={click_id}", [], "addwell.io"),
    _p("Adscend Media", "sub1={click_id}", [], "adscendmedia.com"),
    _p("AdsEmpire", "clickid={click_id}", ["Dating"], "adsempire.com"),
    _p("Adtrafico", "aff_click_id={click_id}", ["Gambling", "CPI", "Dating", "Forex / Binary", "Sweepstakes"], "adtrafico.com"),
    _p("AdultForce", "apb={click_id}", ["Adult"], "adultforce.com"),
    _p("Advibe Media", "sub_id={click_id}", ["E-commerce"], "advibemedia.com"),
    _p("Advidi", "subid={click_id}", ["Leadgen", "Adult", "Dating"], "advidi.com"),
    _p("Affiliate Dragons", "sub_id={click_id}", [], "affiliatedragons.com"),
    _p("Affiliati Network", "sub_id={click_id}", ["Leadgen", "E-commerce", "Sweepstakes", "Nutra", "Financial", "Insurance"], "affiliati.com"),
    _p("Affiliaxe", "aff_sub={click_id}", ["Dating", "E-commerce", "Sweepstakes", "Nutra", "Travel", "Health and Fitness"], "affiliaxe.com"),
    _p("Affsub2", "sub_id={click_id}", ["Gambling", "Dating", "Sweepstakes"], "affsub2.com"),
    _p("AIVIX", "aff_sub={click_id}", ["Gambling", "Adult", "Dating", "Sweepstakes", "Nutra", "Games", "Financial"], "aivix.com"),
    _p("Alfaleads", "s1={click_id}", ["Gambling", "CPI", "Pin-submits", "Leadgen", "Adult", "Dating", "E-commerce", "Installs", "Forex / Binary", "Sweepstakes", "Nutra", "Social", "Games", "Downloads", "Education", "Insurance", "Legal", "Mobile Apps", "Sports"], "alfaleads.com"),
    _p("Big Bang Ads", "aff_sub2={click_id}", ["Pin-submits", "Leadgen", "Sweepstakes"], "bigbangads.com"),
    _p("BillyMob", "sub={click_id}", ["Gambling", "Adult", "Dating", "E-commerce", "Sweepstakes", "Travel", "Social", "Games", "Merchants", "Financial", "Health and Fitness", "Mobile Apps", "Religion and Spirituality"], "billymob.com"),
    _p("Blitzads", "sub_id={click_id}", ["Nutra", "Leadgen", "E-commerce"], "blitzads.com"),
    _p("BuyGoods", "subid2={click_id}", [], "buygoods.com"),
    _p("C3PA", "sub1={click_id}", ["Dating"], "c3pa.net"),
    _p("Capital", "sub_id={click_id}", ["Financial"], "capital.com"),
    _p("Cartpanda", "cid={click_id}", [], "cartpanda.com"),
    _p("Checkout Champ/Konnektive", "c2={click_id}", [], "checkoutchamp.com"),
    _p("CityAds", "xid={click_id}", ["Gambling", "E-commerce"], "cityads.com"),
    _p("Clearpier", "sub_id={click_id}", ["Mobile Apps"], "clearpier.com"),
    _p("Clickbank (S2S postback)", "tid={click_id}", ["Gambling", "Travel", "Games", "Education", "Financial", "Health and Fitness", "Mobile Apps", "Religion and Spirituality", "Sports"], "clickbank.com"),
    _p("Clickdealer", "s2={click_id}", ["Gambling", "Leadgen", "E-commerce", "Installs"], "clickdealer.com"),
    _p("Convert2media", "s2={click_id}", ["Leadgen"], "convert2media.com"),
    _p("CPAGetti", "sub_id={click_id}", ["Nutra"], "cpagetti.com"),
    _p("CPA.house", "sub_id_1={click_id}", [], "cpa.house"),
    _p("CJ Affiliate", "sid={click_id}", [], "cj.com"),
    _p("digistore24", "cid={click_id}", ["Dating", "Nutra", "Carriers", "Education", "E-mail submits", "Health and Fitness", "Style and Fashion", "Technology", "Computing"], "digistore24.com"),
    _p("Everflow", "sub1={click_id}", ["Gambling", "Insurance", "Games"], "everflow.io"),
    _p("Flow Network", "sub_id={click_id}", [], "flownetwork.com"),
    _p("Gasmobi", "externalid={click_id}", ["Sweepstakes", "Nutra", "Financial"], "gasmobi.com"),
    _p("Giddy Up", "sub1={click_id}", ["Health and Fitness", "Technology"], "giddyup.com"),
    _p("Golden Goose", "p1={click_id}", ["Pin-submits", "Mobile Apps"], "goldengoose.com"),
    _p("Gotzha", "s2={click_id}", ["Gambling", "Leadgen", "Sweepstakes"], "gotzha.com"),
    _p("Gurumedia", "sub1={click_id}", [], "gurumedia.io"),
    _p("Impact", "subId1={click_id}", ["Insurance", "Travel"], "impact.com"),
    _p("Invictus Media", "sub_id={click_id}", ["E-commerce", "Health and Fitness"], "invictusmedia.com"),
    _p("juddy.biz", "sub_id={click_id}", [], "juddy.biz"),
    _p("JVZoo", "sub_id={click_id}", ["Education", "Technology", "Computing"], "jvzoo.com"),
    _p("Kimia", "sub_id={click_id}", ["CPI"], "kimia.com"),
    _p("kma.biz", "sub_id={click_id}", ["Adult", "E-commerce", "Health and Fitness", "Style and Fashion", "Technology"], "kma.biz"),
    _p("Leadnomics", "sub_id={click_id}", [], "leadnomics.com"),
    _p("Lemonads", "clickid={click_id}", ["E-commerce", "Sweepstakes", "Nutra", "Games"], "lemonads.com"),
    _p("Los Pollos", "cid={click_id}", ["Adult", "Dating", "Forex / Binary"], "lospollos.com"),
    _p("Lucky Online", "sub_id={click_id}", ["Adult", "E-commerce", "Nutra", "Health and Fitness"], "luckyonline.com"),
    _p("M4TRIX", "sub_id={click_id}", ["Nutra"], "m4trix.io"),
    _p("Madrivo", "sub_id={click_id}", [], "madrivo.com"),
    _p("Masters in Cash", "sub_id={click_id}", [], "mastersincash.com"),
    _p("MaxWeb", "sub_id={click_id}", ["Health and Fitness", "Technology"], "maxweb.com"),
    _p("Media500", "aff_click_id={click_id}", ["Gambling", "Nutra", "Financial"], "media500.com"),
    _p("Mobipium", "tid={click_id}", ["Pin-submits", "Dating"], "mobipium.com"),
    _p("Mobytize", "sub_id={click_id}", ["Dating", "E-commerce", "Sweepstakes", "Games", "Merchants", "Financial", "Mobile Apps"], "mobytize.com"),
    _p("Moja Ai", "sub_id={click_id}", [], "moja.ai"),
    _p("Monetizer", "sub_id={click_id}", ["Mobile Apps"], "monetizer.com"),
    _p("Monetizze", "sub_id={click_id}", ["Education"], "monetizze.com"),
    _p("Mundpay", "sub_id={click_id}", [], "mundpay.com"),
    _p("MyCommerce", "sub_id={click_id}", [], "mycommerce.com"),
    _p("Natifico", "ref_id={click_id}", ["Dating", "Installs", "Sweepstakes", "Games", "Downloads"], "natifico.com"),
    _p("Nexusoffers", "sub_id={click_id}", ["Sweepstakes", "E-mail submits", "Health and Fitness", "Surveys"], "nexusoffers.com"),
    _p("OUTBID", "sub_id={click_id}", [], "outbid.org"),
    _p("PerformCB", "subid2={click_id}", ["Health and Fitness"], "performcb.com"),
    _p("Pinterest CAPI", "sub_id={click_id}", ["Ads"], "pinterest.com"),
    _p("Prestashop", "sub_id={click_id}", ["E-commerce"], "prestashop.com"),
    _p("PROX", "sub_id={click_id}", ["Financial"], "prox.com"),
    _p("Retreaver", "sub_id={click_id}", [], "retreaver.com"),
    _p("Ringba", "sub_id={click_id}", [], "ringba.com"),
    _p("RocketProfit", "sub_id={click_id}", ["Health and Fitness"], "rocketprofit.com"),
    _p("SEDO", "sub_id={click_id}", [], "sedo.com"),
    _p("Shakes.pro", "sub_id={click_id}", ["Nutra", "Health and Fitness", "Technology"], "shakes.pro"),
    _p("Shopify", "sub_id={click_id}", ["E-commerce"], "shopify.com"),
    _p("SmartAdv", "sub_id={click_id}", ["Dating", "E-commerce", "Carriers", "Health and Fitness", "Insurance"], "smartadv.com"),
    _p("Supreme Media", "sub_id={click_id}", [], "suprememedia.com"),
    _p("Terra Leads", "sub_id={click_id}", ["Adult", "Nutra", "Health and Fitness"], "terraleads.com"),
    _p("Tonic", "sub_id={click_id}", [], "tonic.com"),
    _p("TopOffers", "sub_id={click_id}", ["Adult", "Dating", "Sweepstakes"], "topoffers.com"),
    _p("TORO", "sub_id={click_id}", [], "toroadvertising.com"),
    _p("TORO Advertising", "sub_id={click_id}", ["Gambling", "E-commerce", "Games", "Financial", "Health and Fitness"], "toroadvertising.com"),
    _p("Traforce", "sub_id={click_id}", ["Dating"], "traforce.com"),
    _p("Trafee", "sub_id={click_id}", ["Dating", "Adult", "Sweepstakes", "Games"], "trafee.com"),
    _p("Traffic Company", "sub_id={click_id}", [], "trafficcompany.com"),
    _p("TrumpYourAds", "sub_id={click_id}", ["Gambling", "Nutra", "Financial"], "trumpyourads.com"),
    _p("TUNE (ex HasOffers)", "aff_sub={click_id}", ["Mobile Apps", "Financial"], "tune.com"),
    _p("vCommission", "sub_id={click_id}", [], "vcommission.com"),
    _p("Vellko Media", "sub_id={click_id}", ["Dating", "E-commerce", "Nutra", "Insurance"], "vellko.com"),
    _p("Wap.click", "sub_id={click_id}", [], "wap.click"),
    _p("WapEmpire", "sub_id={click_id}", ["Pin-submits", "Adult", "Dating", "Installs", "Social", "Downloads", "Sports"], "wapempire.com"),
    _p("Wildo.click", "sub_id={click_id}", ["Gambling", "Adult"], "wildo.click"),
    _p("WooCommerce", "sub_id={click_id}", ["E-commerce"], "woocommerce.com"),
    _p("WowTrk", "sub_id={click_id}", [], "wowtrk.com"),
    _p("Yeahmobi", "aff_sub={click_id}", ["Gambling", "E-commerce"], "yeahmobi.com"),
    _p("YTZ Network", "sub_id={click_id}", ["Gambling", "Adult", "Dating", "Installs", "Sweepstakes", "Nutra", "Downloads", "Mobile Apps"], "ytznetwork.com"),
    _p("Zorka.Network", "ref_id={click_id}", ["Gambling", "Installs", "Games", "Mobile Apps"], "zorka.network"),
]

# Curated full-logo overrides (favicons are tiny; some brands deserve better).
_NETWORK_LOGO_URLS = {
    "Adcombo": "https://www.adcombo.com/source/images/logo.svg",
    "Ad2games": "https://files.startupranking.com/startup/thumb/59015_fc7ff7df388c24b028de73095f314dc93eac6179_ad2games_l.png",
    "3snet": "https://3snet.co/wp-content/themes/3snet/img/logo.png",
    "MyLead": "https://mylead.global/images/svg/logo_ml.svg",
}
for _preset in NETWORK_PRESETS:
    if _preset["name"] in _NETWORK_LOGO_URLS:
        _preset["logo_url"] = _NETWORK_LOGO_URLS[_preset["name"]]


def seed_network_presets(db: Session):
    """Seed built-in affiliate network presets exactly once (marker in settings),
    adding any presets missing by name so existing installs get them too."""
    from models.settings import SettingsORM
    if db.query(SettingsORM).filter_by(name="network_presets_seeded").first():
        return
    existing = {name for (name,) in db.query(AffiliateNetworkORM.name).all()}
    missing = [p for p in NETWORK_PRESETS if p["name"] not in existing]
    if missing:
        db.add_all([AffiliateNetworkORM(name=p["name"], offer_parameters=p["offer_parameters"],
                                        s2s_postback=p["s2s_postback"]) for p in missing])
    db.add(SettingsORM(name="network_presets_seeded", value="1"))
    db.commit()


class AffiliateNetworkIn(BaseModel):
    name: str
    offer_parameters: Optional[str] = ''
    s2s_postback: Optional[str] = ''


class AffiliateNetworkOut(AffiliateNetworkIn):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


@router.get("/presets")
def get_presets():
    presets = [
        {"name": p["name"], "verticals": p["verticals"], "logo_domain": p["logo_domain"],
         "logo_url": p.get("logo_url"), "offer_parameters": p["offer_parameters"]}
        for p in sorted(NETWORK_PRESETS, key=lambda x: x["name"].lower())
    ]
    return {"presets": presets}


# 1x1 transparent PNG served (with HTTP 200) when no favicon exists, so the
# browser never logs a failed-resource console error for missing logos.
_EMPTY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
_favicon_cache = {}


def _favicon_segment_to_domain(segment: str) -> str:
    """The frontend base64url-encodes the domain behind a "~" marker so
    ad-blocking extensions don't match well-known ad-network domains in the
    URL path (ERR_BLOCKED_BY_CLIENT). Anything that isn't a valid encoded
    value falls back to the raw segment, which the regex check below rejects
    unless it looks like a plain domain."""
    if not segment.startswith("~"):
        return segment
    try:
        body = segment[1:]
        padded = body + "=" * (-len(body) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode("utf-8")
        if re.fullmatch(r"[a-z0-9.\-]+", decoded, re.I):
            return decoded
    except Exception:
        pass
    return segment


@router.get("/favicon/{domain}")
def get_favicon(domain: str):
    domain = _favicon_segment_to_domain(domain)
    if not re.fullmatch(r"[a-z0-9.\-]+", domain, re.I):
        return Response(content=_EMPTY_PNG, media_type="image/png")
    cached = _favicon_cache.get(domain.lower())
    if cached is not None:
        return Response(content=cached, media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})
    try:
        r = httpx.get(f"https://www.google.com/s2/favicons?domain={domain}&sz=64",
                      timeout=5.0, follow_redirects=True)
        content = r.content if r.status_code == 200 and r.content else _EMPTY_PNG
    except Exception:
        content = _EMPTY_PNG
    if len(_favicon_cache) > 500:
        _favicon_cache.clear()
    _favicon_cache[domain.lower()] = content
    return Response(content=content, media_type="image/png",
                    headers={"Cache-Control": "max-age=86400"})


@router.get("/", response_model=List[AffiliateNetworkOut])
def get_networks(db: Session = Depends(get_db)):
    seed_network_presets(db)
    return db.query(AffiliateNetworkORM).order_by(AffiliateNetworkORM.id.desc()).all()


@router.post("/")
def create_network(data: AffiliateNetworkIn, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    new = AffiliateNetworkORM(**data.dict())
    db.add(new)
    try:
        db.commit()
        db.refresh(new)
        from auth import get_caller
        caller, _ = get_caller(request)
        audit_event(caller or "api_token", "create", "affiliate_networks", str(new.id),
                    {"name": new.name}, request.client.host if request.client else "")
        return {"message": "Affiliate network created", "id": new.id}
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Affiliate network with this name already exists")


@router.patch("/{network_id}")
def update_network(network_id: int, data: AffiliateNetworkIn, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    net = db.query(AffiliateNetworkORM).filter_by(id=network_id).first()
    if not net:
        raise HTTPException(status_code=404, detail="Affiliate network not found")

    changed = []
    for key, value in data.dict().items():
        if getattr(net, key, None) != value:
            changed.append(key)
        setattr(net, key, value)

    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "update", "affiliate_networks", str(network_id),
                {"fields": changed}, request.client.host if request.client else "")
    return {"message": "Affiliate network updated"}


@router.delete("/{network_id}")
def delete_network(network_id: int, request: Request, db: Session = Depends(get_db)):
    from audit_logger import audit_event
    # Find the network
    net = db.query(AffiliateNetworkORM).filter_by(id=network_id).first()
    if not net:
        raise HTTPException(status_code=404, detail="Affiliate network not found")

    # Check whether any offers are linked to this network
    has_offers = db.query(OfferORM).filter_by(affiliate_network_id=network_id).first()
    if has_offers:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete affiliate network with linked offers. Delete offers first!"
        )

    db.delete(net)
    db.commit()
    from auth import get_caller
    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "delete", "affiliate_networks", str(network_id),
                {"name": net.name}, request.client.host if request.client else "")
    return {"message": "Affiliate network deleted"}


class NetworkBulkIn(BaseModel):
    ids: List[int]
    action: str  # 'delete'


@router.post("/bulk", response_model=dict)
def bulk_networks(data: NetworkBulkIn, request: Request, db: Session = Depends(get_db)):
    """Bulk delete affiliate networks; networks with linked offers are skipped."""
    from audit_logger import audit_event
    from auth import get_caller
    networks = db.query(AffiliateNetworkORM).filter(
        AffiliateNetworkORM.id.in_(data.ids)).all()
    if data.action != 'delete':
        raise HTTPException(status_code=400, detail=f"Unknown action '{data.action}'")

    blocked, deleted = [], []
    for net in networks:
        if db.query(OfferORM).filter_by(affiliate_network_id=net.id).first():
            blocked.append(net.name)
            continue
        db.delete(net)
        deleted.append(net.id)
    db.commit()

    caller, _ = get_caller(request)
    audit_event(caller or "api_token", "delete", "affiliate_networks",
                ",".join(map(str, deleted)),
                {"bulk": "delete", "count": len(deleted)}, request.client.host if request.client else "")
    return {"message": f"Deleted {len(deleted)} networks",
            "updated": len(deleted), "skipped": blocked}

