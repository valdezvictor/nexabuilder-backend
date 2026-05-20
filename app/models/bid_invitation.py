from sqlalchemy import Column, Integer, String, DateTime, Text, Numeric, Boolean, ForeignKey, Index
from sqlalchemy.sql import func
from app.db import Base


class BidInvitation(Base):
    """
    One record per contractor per lead.
    Tracks the full bid lifecycle: sent → viewed → accepted/declined.
    """
    __tablename__ = "bid_invitations"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    lead_id        = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)
    contractor_id  = Column(Integer, ForeignKey("contractors.id"), nullable=False, index=True)
    account_id     = Column(Integer, nullable=True, index=True)
    status         = Column(String(30), nullable=False, default="pending")
    # pending | sent | viewed | accepted | declined | expired | withdrawn

    sent_at        = Column(DateTime(timezone=True), nullable=True)
    viewed_at      = Column(DateTime(timezone=True), nullable=True)
    responded_at   = Column(DateTime(timezone=True), nullable=True)
    expires_at     = Column(DateTime(timezone=True), nullable=True)

    decline_reason = Column(Text, nullable=True)
    bid_amount     = Column(Numeric(12, 2), nullable=True)
    bid_notes      = Column(Text, nullable=True)
    commission_pct = Column(Numeric(5, 2), nullable=True, default=10.0)

    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    updated_at     = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
