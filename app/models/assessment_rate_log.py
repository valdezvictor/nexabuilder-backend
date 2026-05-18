from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func
from app.db import Base


class AssessmentRateLog(Base):
    """
    Rolling rate limit log for known-address re-assessments.

    Only logged when the submitted address already has a property_assessment
    record (i.e. has a lead attached). Fresh addresses skip this entirely —
    the property gate handles those.

    Rule: max 2 logged attempts per user per rolling 60-minute window.
    """
    __tablename__ = "assessment_rate_log"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_id      = Column(String(36), nullable=False, index=True)
    address_hash = Column(String(64), nullable=False)
    lead_id      = Column(Integer, nullable=True)
    attempted_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
