from sqlalchemy import Column, Integer, String, DateTime, Numeric, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from app.db import Base

class EscrowTransaction(Base):
    """
    Audit trail for every escrow event.
    Sent to Finance 911 (Raul Cruz) escrow system via API.
    Also hooks into LendAPI (Tim Li) for financed projects.
    """
    __tablename__ = "escrow_transactions"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    lead_id          = Column(Integer, ForeignKey("leads.id"), nullable=False)
    milestone_id     = Column(Integer, nullable=True)
    transaction_type = Column(String(50), nullable=False)
    amount           = Column(Numeric(12, 2), nullable=False)
    fee_amount       = Column(Numeric(12, 2), nullable=True)
    status           = Column(String(30), nullable=False, default="pending")
    escrow_ref       = Column(String(200), nullable=True)   # Finance 911 reference
    lender_ref       = Column(String(200), nullable=True)   # LendAPI loan reference
    payload          = Column(JSONB, nullable=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())
