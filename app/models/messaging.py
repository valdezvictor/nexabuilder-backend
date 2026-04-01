import enum
from uuid import uuid4

from sqlalchemy import Column, String, Enum, Boolean, JSON
from sqlalchemy.dialects.postgresql import UUID
from app.db import Base

class MessagingType(enum.Enum):
    sms = "sms"
    email = "email"

class MessagingProvider(Base):
    __tablename__ = "messaging_providers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    type = Column(Enum(MessagingType), nullable=False)
    name = Column(String, nullable=False)  # "sns", "ses", "twilio", etc.
    config = Column(JSON, nullable=False, default=dict)
    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)
