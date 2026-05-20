from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from app.db import Base


class ContractorAgreement(Base):
    """
    Legal agreement signed by contractor on first portal access.
    Typed full name = electronic signature.
    attorney_reviewed flag set to True once Mario Trujillo approves v1.0.
    """
    __tablename__ = "contractor_agreements"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    contractor_account_id = Column(Integer, nullable=False, index=True)
    user_id               = Column(UUID(as_uuid=False), nullable=False, index=True)
    agreement_version     = Column(String(20), nullable=False)  # "1.0"

    agreed_at             = Column(DateTime(timezone=True), nullable=False)
    ip_address            = Column(String(45), nullable=True)
    user_agent            = Column(Text, nullable=True)

    full_name_signed      = Column(String(255), nullable=False)
    email_signed          = Column(String(255), nullable=False)
    license_number        = Column(String(100), nullable=False)

    terms_acknowledged    = Column(JSONB, nullable=True)
    attorney_reviewed     = Column(Boolean, nullable=False, default=False)

    created_at            = Column(DateTime(timezone=True), server_default=func.now())
