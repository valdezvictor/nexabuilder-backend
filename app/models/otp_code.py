import enum
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.db import Base


class OTPCode(Base):
    """
    Short-lived one-time password for email/SMS verification.
    6-digit code, 10-minute TTL, max 5 attempts.
    """
    __tablename__ = "otp_codes"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(UUID(as_uuid=False), nullable=False, index=True)
    code       = Column(String(6), nullable=False)
    channel    = Column(String(10), nullable=False)   # email | sms
    purpose    = Column(String(30), nullable=False)   # verification | login
    is_used    = Column(Boolean, nullable=False, default=False)
    attempts   = Column(Integer, nullable=False, default=0)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
