from fastapi import APIRouter, Depends, HTTPException
from typing import List
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from db import get_db
from models.affiliate_networks import AffiliateNetworkORM
from models.offers import OfferORM  # imported for the offer check

router = APIRouter()

# Built-in affiliate network presets, shipped out of the box.
# {click_id} is replaced by the network's subid macro value.
NETWORK_PRESETS = [
    {"name": "MaxBounty", "offer_parameters": "s1={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "Admitad", "offer_parameters": "subid={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "Adsterra", "offer_parameters": "sub1={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "Affise", "offer_parameters": "sub1={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "ClickDealer", "offer_parameters": "sub1={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "CrakRevenue", "offer_parameters": "sub1={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "CPAGrip", "offer_parameters": "subid={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "CPAlead", "offer_parameters": "subid={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "Dr.Cash", "offer_parameters": "subid={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "Everad", "offer_parameters": "subid={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "HasOffers", "offer_parameters": "aff_sub={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "HilltopAds", "offer_parameters": "sub_id={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "Leadbit", "offer_parameters": "sub_id={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "Mobidea", "offer_parameters": "subid={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "MyLead", "offer_parameters": "sub_id={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "PropellerAds", "offer_parameters": "sub_id={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "Zeydoo", "offer_parameters": "sub_id={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "T3Leads", "offer_parameters": "subid={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "ClickBank", "offer_parameters": "tid={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
    {"name": "Galaksion", "offer_parameters": "subid={click_id}", "s2s_postback": "https://YOUR-TRACKER-DOMAIN/pb/{click_id}/{status}/{payout}"},
]


def seed_network_presets(db: Session):
    """Seed built-in affiliate network presets exactly once (marker in settings),
    adding any presets missing by name so existing installs get them too."""
    from models.settings import SettingsORM
    if db.query(SettingsORM).filter_by(name="network_presets_seeded").first():
        return
    existing = {name for (name,) in db.query(AffiliateNetworkORM.name).all()}
    missing = [p for p in NETWORK_PRESETS if p["name"] not in existing]
    if missing:
        db.add_all([AffiliateNetworkORM(**p) for p in missing])
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


@router.get("/", response_model=List[AffiliateNetworkOut])
def get_networks(db: Session = Depends(get_db)):
    seed_network_presets(db)
    return db.query(AffiliateNetworkORM).order_by(AffiliateNetworkORM.id.desc()).all()


@router.post("/")
def create_network(data: AffiliateNetworkIn, db: Session = Depends(get_db)):
    new = AffiliateNetworkORM(**data.dict())
    db.add(new)
    try:
        db.commit()
        db.refresh(new)
        return {"message": "Affiliate network created", "id": new.id}
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Affiliate network with this name already exists")


@router.patch("/{network_id}")
def update_network(network_id: int, data: AffiliateNetworkIn, db: Session = Depends(get_db)):
    net = db.query(AffiliateNetworkORM).filter_by(id=network_id).first()
    if not net:
        raise HTTPException(status_code=404, detail="Affiliate network not found")

    for key, value in data.dict().items():
        setattr(net, key, value)

    db.commit()
    return {"message": "Affiliate network updated"}


@router.delete("/{network_id}")
def delete_network(network_id: int, db: Session = Depends(get_db)):
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
    return {"message": "Affiliate network deleted"}

