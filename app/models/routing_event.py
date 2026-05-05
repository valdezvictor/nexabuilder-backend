from sqlalchemy import Integer, String, DateTime, ForeignKey, JSON, func
from sqlalchemy.orm import mapped_column, relationship
from app.db import Base

class RoutingEvent(Base):
    __tablename__ = "routing_events"

    id = mapped_column(Integer, primary_key=True)
    lead_id = mapped_column(ForeignKey("leads.id"), nullable=False)
    contractor_id = mapped_column(ForeignKey("contractors.id"), nullable=True)
    event_type = mapped_column(String(50), nullable=False)  # "scored", "assigned", "no_match"
    payload = mapped_column(JSON, nullable=True)
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())

    lead = relationship("Lead")
    contractor = relationship("Contractor")
