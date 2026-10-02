from sqlalchemy import Column, Integer, String, TIMESTAMP, func, UniqueConstraint
from models.base import Base, TenantMixin

class AffiliateNetworkORM(TenantMixin, Base):
    __tablename__ = "affiliate_networks"
    __table_args__ = (UniqueConstraint("tenant_id", "name",
                                       name="affiliate_networks_tenant_name_key"),)

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    offer_parameters = Column(String(1024))
    s2s_postback = Column(String(1024))
    # Owner scope parity with campaigns.owner_id: the affiliates:'own' permission
    # limits a caller to affiliate networks they own.
    owner_id = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())
