# app/models/contractor.py

from sqlalchemy import Integer, String, DateTime, Float, func
from sqlalchemy.orm import mapped_column, relationship
from app.db import Base


class Contractor(Base):
    __tablename__ = "contractors"

    id = mapped_column(Integer, primary_key=True)

    # Identity
    name = mapped_column(String(255), nullable=False, index=True)  # display name
    legal_name = mapped_column(String(255), nullable=True)
    dba_name = mapped_column(String(255), nullable=True)
    entity_type = mapped_column(String(50), nullable=True)         # LLC, Corp, Sole Prop, etc.

    # Contact
    phone = mapped_column(String(50), nullable=True)
    email = mapped_column(String(255), nullable=True)
    website = mapped_column(String(255), nullable=True)

    # Address
    address_line1 = mapped_column(String(255), nullable=True)
    address_line2 = mapped_column(String(255), nullable=True)
    city = mapped_column(String(100), nullable=True)
    state = mapped_column(String(2), nullable=True)
    postal_code = mapped_column(String(10), nullable=True)
    county = mapped_column(String(100), nullable=True)

    # Geo
    latitude = mapped_column(Float, nullable=True)
    longitude = mapped_column(Float, nullable=True)

    # Service area
    service_radius = mapped_column(Integer, nullable=True)  # miles

    # Enrichment
    last_enriched_at = mapped_column(DateTime(timezone=True), nullable=True)

    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at = mapped_column(DateTime(timezone=True), onupdate=func.now())

    licenses = relationship("License", back_populates="contractor")
    leads = relationship("Lead", back_populates="contractor")
    trades = relationship("Trade", secondary="contractor_trades", back_populates="contractors")
