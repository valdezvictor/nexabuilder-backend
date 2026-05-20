from sqlalchemy import Column, Integer, String, DateTime, Text, Numeric, ForeignKey
from sqlalchemy.sql import func
from app.db import Base

class ProjectMilestone(Base):
    """
    One row per project phase.
    BOTH contractor and homeowner must confirm before escrow releases payment.
    This is the core of the dual-confirmation payment model.
    """
    __tablename__ = "project_milestones"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    lead_id            = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)
    bid_id             = Column(Integer, nullable=True, index=True)
    milestone_number   = Column(Integer, nullable=False)
    title              = Column(String(200), nullable=False)
    description        = Column(Text, nullable=True)
    phase_amount       = Column(Numeric(12, 2), nullable=False)
    nexabuilder_fee    = Column(Numeric(12, 2), nullable=True)
    status             = Column(String(30), nullable=False, default="pending")
    # pending | contractor_confirmed | homeowner_confirmed | both_confirmed | released | disputed

    contractor_confirmed_at = Column(DateTime(timezone=True), nullable=True)
    homeowner_confirmed_at  = Column(DateTime(timezone=True), nullable=True)
    escrow_release_at       = Column(DateTime(timezone=True), nullable=True)
    contractor_notes        = Column(Text, nullable=True)
    homeowner_notes         = Column(Text, nullable=True)
    dispute_reason          = Column(Text, nullable=True)
    created_at              = Column(DateTime(timezone=True), server_default=func.now())
    updated_at              = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
