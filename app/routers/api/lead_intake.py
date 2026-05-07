# app/routers/api/lead_intake.py
# Public endpoint for member portal lead submission
# Creates both a Lead record and a User account for magic link auth

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import select
from uuid import uuid4

from app.db import get_sessionmaker
from app.models.lead import Lead
from app.models.user import User, UserRole, UserStatus
from app.models.user_tenant import UserTenant
from app.models.tenant import Tenant
from app.core.security import hash_password

router = APIRouter(prefix="/api/leads", tags=["Lead Intake"])


class LeadIntakeRequest(BaseModel):
    vertical:     str
    project_type: Optional[str] = None
    first_name:   Optional[str] = None
    last_name:    Optional[str] = None
    email:        Optional[str] = None
    phone:        Optional[str] = None
    postal_code:  Optional[str] = None
    description:  Optional[str] = None


@router.post("/intake")
async def submit_lead(payload: LeadIntakeRequest):
    """
    Public endpoint - no auth required.
    Creates a Lead record and ensures a User account exists for magic link auth.
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Create the lead record
        lead = Lead(
            vertical=payload.vertical,
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
            phone=payload.phone,
            postal_code=payload.postal_code,
        )
        db.add(lead)
        await db.flush()

        # Ensure user account exists for magic link auth
        if payload.email:
            existing = await db.execute(
                select(User).where(User.email == payload.email)
            )
            user = existing.scalar_one_or_none()

            if not user:
                # Get member tenant
                tenant_result = await db.execute(
                    select(Tenant).where(Tenant.domain == "member.nexabuilder.com")
                )
                tenant = tenant_result.scalar_one_or_none()

                # Create user account with random password (magic link only)
                user = User(
                    id=uuid4(),
                    email=payload.email,
                    password_hash=hash_password(str(uuid4())),  # random, never used
                    role=UserRole.lead,
                    status=UserStatus.active
                )
                db.add(user)
                await db.flush()

                if tenant:
                    db.add(UserTenant(
                        id=uuid4(),
                        user_id=user.id,
                        tenant_id=tenant.id
                    ))

        await db.commit()
        await db.refresh(lead)

        return {
            "id":      lead.id,
            "message": "Lead submitted successfully",
            "email":   lead.email,
        }
