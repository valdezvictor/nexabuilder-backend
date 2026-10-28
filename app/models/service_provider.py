# app/models/service_provider.py
# Service providers: notary, loan processor, insurance agent, permit runner
# Auto-matched to leads by zip proximity + service type

import enum
from sqlalchemy import Column, String, Enum, Boolean, Float, DateTime, Text, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
from uuid import uuid4
from app.db import Base


class ServiceType(enum.Enum):
    notary = "notary"                      # Physical signatures, permit docs
    loan_processor = "loan_processor"      # Construction loans, HELOCs
    insurance_agent = "insurance_agent"    # Homeowner, liability, builder's risk
    permit_runner = "permit_runner"        # City submission, follow-up
    escrow_officer = "escrow_officer"      # Construction draw management


class ServiceProviderStatus(enum.Enum):
    active = "active"
    inactive = "inactive"
    suspended = "suspended"


class ServiceProvider(Base):
    __tablename__ = "service_providers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    email = Column(String, unique=True, index=True, nullable=False)
    phone = Column(String, nullable=True)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    company_name = Column(String(200), nullable=True)

    # Service type and coverage
    service_type = Column(String(50), nullable=False)
    status = Column(String(20), default='active')

    # Geographic coverage
    postal_codes = Column(JSONB, nullable=True)   # List of zip codes served
    cities = Column(JSONB, nullable=True)          # List of cities served
    states = Column(JSONB, nullable=True)          # List of states licensed in
    max_radius_miles = Column(Integer, default=25) # Max distance from base zip

    # Base location
    base_postal_code = Column(String(10), nullable=True)
    base_city = Column(String(100), nullable=True)
    base_state = Column(String(2), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    # Payment
    flat_rate = Column(Float, nullable=True)       # Flat fee per job (e.g. $150 notary)
    commission_pct = Column(Float, nullable=True)  # % of contract (e.g. 1.5 for loan)
    payment_model = Column(String(20), default="flat_rate")  # flat_rate | commission

    # Availability
    available = Column(Boolean, default=True)
    max_concurrent_jobs = Column(Integer, default=5)
    current_job_count = Column(Integer, default=0)

    # Auth
    password_hash = Column(String, nullable=True)
    access_token = Column(String, nullable=True)   # For magic link / portal access

    # Metadata
    license_number = Column(String(100), nullable=True)
    license_state = Column(String(2), nullable=True)
    notes = Column(Text, nullable=True)
    rating = Column(Float, nullable=True)
    jobs_completed = Column(Integer, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
