# app/models/lead.py

from sqlalchemy import Integer, String, Float, JSON, DateTime, ForeignKey, func
from sqlalchemy.orm import mapped_column, relationship
from app.db import Base


class Lead(Base):
    __tablename__ = "leads"

    id = mapped_column(Integer, primary_key=True)
    contractor_id = mapped_column(ForeignKey("contractors.id"), nullable=True)
    trade_id = mapped_column(ForeignKey("trades.id"), nullable=True)
    zip_id = mapped_column(ForeignKey("zipcodes.id"), nullable=True)

    # Contact / context
    phone = mapped_column(String(50), nullable=True)
    email = mapped_column(String(255), nullable=True)
    budget_max = mapped_column(Integer, nullable=True)
    vertical = mapped_column(String(100), nullable=True)

    # Address
    address_line1 = mapped_column(String(255), nullable=True)
    address_line2 = mapped_column(String(255), nullable=True)
    city = mapped_column(String(100), nullable=True)
    state = mapped_column(String(2), nullable=True)
    postal_code = mapped_column(String(10), nullable=True)

    # Geo
    latitude = mapped_column(Float, nullable=True)
    longitude = mapped_column(Float, nullable=True)

    # AI / scoring
    ai_score = mapped_column(Float, nullable=True)
    ai_explanations = mapped_column(JSON, nullable=True)

    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())

    contractor = relationship("Contractor", back_populates="leads")
    trade = relationship("Trade", back_populates="leads")
    zip = relationship("ZipCode", back_populates="leads")
