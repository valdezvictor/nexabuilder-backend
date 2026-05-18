from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func
from app.db import Base


class ActiveProject(Base):
    """
    Links a CSLB-verified contractor to an active project at a specific address.

    A contractor can only run an assessment if:
        1. Their license_number matches a row here
        2. address_hash matches the project address
        3. project_status == 'active'

    Created automatically when:
        - A lead is matched to a contractor (source='nexabuilder_lead')
        - A contractor manually adds a project they're working on (source='contractor_added')
        - Future: permit data import (source='permit_import')
    """
    __tablename__ = "active_projects"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    license_number     = Column(String(100), nullable=False, index=True)
    state_code         = Column(String(2), nullable=False, default="CA")
    address_hash       = Column(String(64), nullable=False, index=True)
    address_line1      = Column(String(255), nullable=True)
    city               = Column(String(100), nullable=True)
    state              = Column(String(2), nullable=True)
    postal_code        = Column(String(10), nullable=True)
    lead_id            = Column(Integer, nullable=True, index=True)
    vertical           = Column(String(100), nullable=True)
    project_status     = Column(String(30), nullable=False, default="active")
    source             = Column(String(50), nullable=True)
    permit_number      = Column(String(100), nullable=True)
    assessment_count   = Column(Integer, nullable=False, default=0)
    last_assessment_at = Column(DateTime(timezone=True), nullable=True)
    created_at         = Column(DateTime(timezone=True), server_default=func.now())
    updated_at         = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
