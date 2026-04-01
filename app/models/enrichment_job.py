# app/models/enrichment_job.py

from sqlalchemy import Integer, String, Text, DateTime, ForeignKey, JSON, func
from sqlalchemy.orm import mapped_column
from app.db import Base


class EnrichmentJob(Base):
    __tablename__ = "enrichment_jobs"

    id = mapped_column(Integer, primary_key=True)

    # Universal target
    entity_type = mapped_column(String(50), nullable=False)  # 'contractor', 'license', 'trade', 'zipcode', 'lead'
    entity_id = mapped_column(Integer, nullable=False)       # ID in the target table

    job_type = mapped_column(String(50), nullable=False)     # 'geocode_address', 'normalize_trade', etc.
    status = mapped_column(String(50), default="pending")    # 'pending', 'processing', 'success', 'failed'

    payload = mapped_column(JSON, nullable=True)             # input data for the job
    result = mapped_column(JSON, nullable=True)              # output data from the job

    error_message = mapped_column(Text, nullable=True)

    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at = mapped_column(DateTime(timezone=True), onupdate=func.now())
