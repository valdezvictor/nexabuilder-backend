from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.db import Base


class ContractorAccount(Base):
    """
    CSLB-verified contractor portal account.
    Created when a contractor registers with their license number.
    Must pass CSLB identity challenge before full portal access.
    """
    __tablename__ = "contractor_accounts"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    user_id            = Column(UUID(as_uuid=False), nullable=False, unique=True, index=True)
    license_number     = Column(String(100), nullable=False, index=True)
    state_code         = Column(String(2), nullable=False, default="CA")
    cslb_verified      = Column(Boolean, nullable=False, default=False)
    challenge_status   = Column(String(30), nullable=False, default="pending")
    # pending | passed | failed | locked
    challenge_attempts = Column(Integer, nullable=False, default=0)
    challenge_passed_at = Column(DateTime(timezone=True), nullable=True)
    contractor_db_id   = Column(Integer, nullable=True)   # links to contractors.id
    company_name       = Column(String(255), nullable=True)
    created_at         = Column(DateTime(timezone=True), server_default=func.now())
    updated_at         = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
