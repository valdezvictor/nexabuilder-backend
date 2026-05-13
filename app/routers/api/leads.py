# app/routers/api/leads.py
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.db import get_sessionmaker
from app.models.lead import Lead

router = APIRouter(prefix="/api/leads", tags=["Leads"])


def _lead_to_dict(lead: Lead) -> dict:
    return {
        "id":           lead.id,
        "first_name":   lead.first_name,
        "last_name":    lead.last_name,
        "email":        lead.email,
        "phone":        lead.phone,
        "vertical":     lead.vertical,
        "postal_code":  lead.postal_code,
        "city":         lead.city,
        "state":        lead.state,
        "ai_score":     lead.ai_score,
        "routing_tier": lead.routing_tier,
        "created_at":   str(lead.created_at) if lead.created_at else None,
    }


@router.get("")
async def list_leads(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    identity: dict = Depends(get_current_user),
):
    # TODO: scope by contractor integer ID once user-contractor mapping exists
    # For now all authenticated users see all leads
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        stmt = select(Lead).order_by(Lead.created_at.desc()).limit(limit).offset(offset)
        result = await db.execute(stmt)
        leads = result.scalars().all()
        return [_lead_to_dict(l) for l in leads]


@router.get("/{lead_id}")
async def get_lead(
    lead_id: int,
    identity: dict = Depends(get_current_user),
):
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        return _lead_to_dict(lead)


class LeadStatusUpdate(BaseModel):
    status: str


@router.put("/{lead_id}/status")
async def update_lead_status(
    lead_id: int,
    payload: LeadStatusUpdate,
    identity: dict = Depends(get_current_user),
):
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        await db.execute(
            update(Lead).where(Lead.id == lead_id).values(routing_tier=payload.status)
        )
        await db.commit()
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        return _lead_to_dict(result.scalar_one())


# ── Intake endpoint for member portal ────────────────────────────────────────
from pydantic import BaseModel as _BaseModel
from typing import Optional as _Optional

class LeadIntakeRequest(_BaseModel):
    vertical:      str
    project_type:  str
    first_name:    _Optional[str] = None
    last_name:     _Optional[str] = None
    email:         str
    phone:         _Optional[str] = None
    address_line1: _Optional[str] = None
    project_scope: _Optional[str] = None

@router.post("/intake")
async def lead_intake(payload: LeadIntakeRequest):
    """
    Public intake form submission from member portal.
    Creates a lead record and triggers magic link email.
    """
    from app.models.lead import Lead
    from app.routers.api.magic_link import send_magic_link_email, create_magic_token
    from app.models.user import User, UserRole, UserStatus
    from app.models.user_tenant import UserTenant
    from app.models.tenant import Tenant
    from app.core.security import hash_password
    from uuid import uuid4
    from sqlalchemy import select

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Get member tenant
        tenant_result = await db.execute(
            select(Tenant).where(Tenant.domain == "member.nexabuilder.com")
        )
        tenant = tenant_result.scalar_one_or_none()

        # Find or create user for this email
        user_result = await db.execute(select(User).where(User.email == payload.email))
        user = user_result.scalar_one_or_none()

        if not user:
            user = User(
                id=uuid4(),
                email=payload.email,
                password_hash=hash_password(str(uuid4())),  # random unusable password
                role=UserRole.lead,
                status=UserStatus.active,
            )
            db.add(user)
            await db.flush()
            if tenant:
                db.add(UserTenant(id=uuid4(), user_id=user.id, tenant_id=tenant.id))

        # Create lead record
        lead = Lead(
            email=payload.email,
            phone=payload.phone,
            first_name=payload.first_name,
            last_name=payload.last_name,
            vertical=payload.vertical,
            address_line1=payload.address_line1,
        )
        db.add(lead)
        await db.commit()

        # Send magic link
        token = create_magic_token(str(user.id), user.email)
        await send_magic_link_email(user.email, token)

    return {"message": "Submission received. Check your email for a secure link."}


@router.get("/{lead_id}")
async def get_lead(
    lead_id: int,
    identity: dict = Depends(get_current_user),
):
    """Get a single lead by ID."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        from sqlalchemy import select as sa_select
        result = await db.execute(sa_select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Lead not found")

        return {
            "id": lead.id,
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "email": lead.email,
            "phone": lead.phone,
            "vertical": lead.vertical,
            "project_type": getattr(lead, "project_type", None),
            "project_description": getattr(lead, "project_description", None),
            "postal_code": lead.postal_code,
            "city": lead.city,
            "state": lead.state,
            "source": getattr(lead, "source", None),
            "routing_tier": lead.routing_tier,
            "ai_score": lead.ai_score,
            "ai_assessment": getattr(lead, "ai_assessment", None),
            "estimate": getattr(lead, "estimate", None),
            "created_at": lead.created_at.isoformat() if lead.created_at else None,
        }
