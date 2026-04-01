import enum
from sqlalchemy import Column, String, Enum, DateTime, ForeignKey, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from uuid import uuid4
from app.db import Base

class TenantType(enum.Enum):
    admin = "admin"
    partner = "partner"
    contractor = "contractor"
    agent = "agent"
    lead = "lead"

class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String, unique=True, nullable=False)
    domain = Column(String, unique=True, nullable=False)
    type = Column(Enum(TenantType), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

