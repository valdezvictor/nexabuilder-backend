# app/models/service_job.py
# Tracks assignment of service providers to leads
# Handles job offer, acceptance, document flow, payment

import enum
from sqlalchemy import Column, String, Enum, Float, DateTime, Text, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from uuid import uuid4
from app.db import Base


class JobStatus(enum.Enum):
    offered = "offered"           # SMS/email sent to provider
    accepted = "accepted"         # Provider accepted
    declined = "declined"         # Provider declined
    in_progress = "in_progress"  # Work started
    docs_uploaded = "docs_uploaded"  # Documents submitted
    completed = "completed"       # Job done, payment released
    cancelled = "cancelled"


class ServiceJob(Base):
    __tablename__ = "service_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    # Lead this job is for
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)

    # Assigned service provider
    provider_id = Column(UUID(as_uuid=True), ForeignKey("service_providers.id"), nullable=True)

    # Job details
    service_type = Column(String(50), nullable=False)  # notary, loan_processor, etc.
    status = Column(Enum(JobStatus), default=JobStatus.offered)
    description = Column(Text, nullable=True)          # What needs to be done

    # Payment
    payment_model = Column(String(20), nullable=True)  # flat_rate | commission
    flat_rate = Column(Float, nullable=True)
    commission_pct = Column(Float, nullable=True)
    contract_amount = Column(Float, nullable=True)     # Total contract for % calc
    payment_amount = Column(Float, nullable=True)      # Calculated payment
    payment_status = Column(String(20), default="pending")  # pending|released|paid

    # Offer flow
    offer_sent_at = Column(DateTime(timezone=True), nullable=True)
    offer_accepted_at = Column(DateTime(timezone=True), nullable=True)
    offer_declined_at = Column(DateTime(timezone=True), nullable=True)
    accept_token = Column(String(200), nullable=True)  # One-time token in SMS/email

    # Document tracking
    documents_required = Column(JSONB, nullable=True)  # List of required docs
    documents_uploaded = Column(JSONB, nullable=True)  # S3 keys of uploaded docs

    # Completion
    completed_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
