from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, func
from models.base import Base


class DomainGroupORM(Base):
    """A named group of domains with per-user access grants (D2).

    Domains in a group are hidden from users who don't hold a grant when the
    campaign-binding domain list is built; admins always see everything.
    """
    __tablename__ = "domain_groups"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DomainGroupDomainORM(Base):
    """Membership: which domains belong to a group."""
    __tablename__ = "domain_group_domains"

    group_id = Column(Integer, ForeignKey("domain_groups.id", ondelete="CASCADE"),
                      primary_key=True)
    domain_id = Column(Integer, ForeignKey("domains.id", ondelete="CASCADE"),
                       primary_key=True)


class DomainGroupUserORM(Base):
    """Grant: which users may use the domains of a group."""
    __tablename__ = "domain_group_users"

    group_id = Column(Integer, ForeignKey("domain_groups.id", ondelete="CASCADE"),
                      primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     primary_key=True)
