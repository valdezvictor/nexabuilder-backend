from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.db import Base


class PropertyAssessment(Base):
    """
    Canonical record per normalized property address.

    Dedup key: address_hash (SHA-256 of normalized address string).
    One record per (address_hash, user_id) — same homeowner, same house
    updates the record rather than creating a new one.

    The 90-day window check:
        SELECT * FROM property_assessments
        WHERE address_hash = :hash
        AND last_assessed_at > now() - interval '90 days'
        AND user_id != :current_user_id
    → If any row found, a DIFFERENT user already assessed this address.
    """
    __tablename__ = "property_assessments"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    address_hash        = Column(String(64), nullable=False, index=True)
    address_raw         = Column(String(500), nullable=False)
    address_line1       = Column(String(255), nullable=True)
    city                = Column(String(100), nullable=True)
    state               = Column(String(2), nullable=True)
    postal_code         = Column(String(10), nullable=True)
    user_id             = Column(String(36), nullable=False, index=True)
    lead_id             = Column(Integer, nullable=False, index=True)
    vertical            = Column(String(100), nullable=True)
    permit_verified     = Column(Boolean, nullable=False, default=False)
    homeowner_verified  = Column(Boolean, nullable=False, default=False)
    assessment_count    = Column(Integer, nullable=False, default=1)
    first_assessed_at   = Column(DateTime(timezone=True), server_default=func.now())
    last_assessed_at    = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
