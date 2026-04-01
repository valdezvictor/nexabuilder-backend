# app/models/license.py

from sqlalchemy import Integer, String, Text, ForeignKey, UniqueConstraint, DateTime, Boolean
from sqlalchemy.orm import mapped_column, relationship
from app.db import Base


class License(Base):
    __tablename__ = "licenses"

    id = mapped_column(Integer, primary_key=True)
    contractor_id = mapped_column(ForeignKey("contractors.id"), nullable=False)

    state_code = mapped_column(String(2), nullable=False, index=True)
    license_number = mapped_column(String(100), nullable=False)
    status = mapped_column(String(50), nullable=True)
    contractor_name = mapped_column(String(255), nullable=True)

    # Extended metadata
    license_type = mapped_column(String(100), nullable=True)
    classification = mapped_column(String(100), nullable=True)
    issue_date = mapped_column(DateTime(timezone=True), nullable=True)
    expiration_date = mapped_column(DateTime(timezone=True), nullable=True)
    bond_amount = mapped_column(Integer, nullable=True)
    workers_comp_status = mapped_column(String(50), nullable=True)
    workers_comp_expiration = mapped_column(DateTime(timezone=True), nullable=True)
    primary_license = mapped_column(Boolean, nullable=True)

    scrape_run_id = mapped_column(ForeignKey("scrape_runs.id"), nullable=True)

    contractor = relationship("Contractor", back_populates="licenses")
    scrape_run = relationship("ScrapeRun", back_populates="licenses")

    __table_args__ = (
        UniqueConstraint("state_code", "license_number", name="uq_license_state_number"),
    )
