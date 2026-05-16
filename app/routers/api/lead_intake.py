# app/routers/api/lead_intake.py
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import select
from uuid import uuid4
from datetime import datetime, timedelta

from app.db import get_sessionmaker
from app.models.lead import Lead
from app.models.user import User, UserRole, UserStatus
from app.models.user_tenant import UserTenant
from app.models.tenant import Tenant
from app.core.security import hash_password
from app.services.sms import send_magic_link_sms
from app.routers.api.contractor_matching import should_route_internal, INTERNAL_CONTRACTOR
from app.services.ai_intake import assess_lead
from jose import jwt
from app.core.config import settings

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
    source:       Optional[str] = "web_form"  # web_form, call_center_inbound, call_center_outbound, tv_ad, radio_ad, referral


def _create_access_token(user_id: str, tenant_id: str) -> str:
    """Create a 30-day access token for phone-only leads"""
    payload = {
        "sub": user_id,
        "tenant": tenant_id,
        "role": "lead",
        "exp": datetime.utcnow() + timedelta(days=30),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


@router.post("/intake")
async def submit_lead(payload: LeadIntakeRequest):
    """
    Public endpoint - no auth required.
    Creates a Lead record and a User account for portal access.
    Phone-only leads get a direct-access token URL (for SMS).
    """
    if not payload.email and not payload.phone:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Email or phone is required")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Create the lead record
        lead = Lead(
            vertical=payload.vertical,
            project_type=payload.project_type,
            project_description=payload.description,
            source=payload.source,
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
            phone=payload.phone,
            postal_code=payload.postal_code,
        )
        db.add(lead)
        await db.flush()

        # Get member tenant
        tenant_result = await db.execute(
            select(Tenant).where(Tenant.domain == "member.nexabuilder.com")
        )
        tenant = tenant_result.scalar_one_or_none()
        tenant_id = str(tenant.id) if tenant else ""

        # Find or create user account
        user = None
        if payload.email:
            existing = await db.execute(select(User).where(User.email == payload.email))
            user = existing.scalar_one_or_none()

        if not user:
            # For phone-only: generate internal email alias
            email_for_account = payload.email or f"lead-{lead.id}@nexabuilder.internal"
            user = User(
                id=uuid4(),
                email=email_for_account,
                password_hash=hash_password(str(uuid4())),
                role=UserRole.lead,
                status=UserStatus.active
            )
            db.add(user)
            await db.flush()

            if tenant:
                db.add(UserTenant(id=uuid4(), user_id=user.id, tenant_id=tenant.id))

        await db.commit()
        await db.refresh(lead)

        # Run AI intake assessment
        ai_assessment = assess_lead(
            vertical=payload.vertical,
            project_type=payload.project_type,
            description=payload.description,
            postal_code=payload.postal_code,
            budget=getattr(payload, 'budget', None),
            first_name=payload.first_name,
            last_name=payload.last_name,
            phone=payload.phone,
            email=payload.email,
        )

        # For phone-only leads: generate direct access URL with 30-day token
        token_url = None
        if payload.phone and not payload.email:
            direct_token = _create_access_token(str(user.id), tenant_id)
            token_url = f"https://member.nexabuilder.com/auth/verify?token={direct_token}"

        # Save AI assessment to lead record
        if lead.id and ai_assessment.get('ai_assessed'):
            lead.ai_assessment = ai_assessment
            await db.commit()

        # Auto internal routing check
        if ai_assessment.get('ai_assessed'):
            if should_route_internal(lead, ai_assessment):
                from app.services.sms import send_sms
                lead_name = f"{payload.first_name or ''} {payload.last_name or ''}".strip() or "New Lead"
                score = ai_assessment.get('complexity_score', 'N/A')
                cost = ai_assessment.get('estimated_cost_range', 'TBD')
                sms_msg = (
                    f"NexaBuilder INTERNAL LEAD: {lead_name} | "
                    f"{payload.project_type or payload.vertical} | "
                    f"ZIP {payload.postal_code} | Score {score}/10 | {cost} | "
                    f"Review: https://admin.nexabuilder.com/leads/{lead.id}"
                )
                send_sms(INTERNAL_CONTRACTOR['phone'], sms_msg)
                print(f"[INTERNAL ROUTE] Lead #{lead.id} routed to Victor's crew")

        # Auto-send SMS for phone-only leads
        if token_url and payload.phone:
            phone = payload.phone.replace("-","").replace(" ","").replace("(","").replace(")","")
            if not phone.startswith("+"):
                phone = "+1" + phone
            send_magic_link_sms(phone, token_url)

        return {
            "id":       lead.id,
            "message":  "Lead submitted successfully",
            "email":    lead.email,
            "phone":    lead.phone,
            "source":   payload.source,
            "token_url": token_url,
            "ai_assessment": ai_assessment,
        }
