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
        "lead_status": getattr(lead, "lead_status", "submitted") or "submitted",
        "ai_assessment": getattr(lead, "ai_assessment", None),
        "estimate": getattr(lead, "estimate", None),
        "project_type": getattr(lead, "project_type", None),
        "source": getattr(lead, "source", None),
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




@router.get("/me")
async def get_my_leads(
    identity: dict = Depends(get_current_user),
):
    """
    Returns the most recent lead(s) for the authenticated user.
    Matches by email from the JWT token — this is the member portal entry point.
    Called by Dashboard when no lead_id is in localStorage.
    """
    from app.models.user import User
    from uuid import UUID

    # Get email from the JWT identity
    user_obj = identity.get("user")
    user_email = None

    # Try to get email from user object
    if user_obj and hasattr(user_obj, "email"):
        user_email = user_obj.email
    
    # Fallback: look up by user_id (sub claim)
    user_id = identity.get("sub") or identity.get("user_id")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        if not user_email and user_id:
            try:
                user = await db.get(User, UUID(str(user_id)))
                if user:
                    user_email = user.email
            except Exception:
                pass

        if not user_email:
            raise HTTPException(status_code=404, detail="No leads found for this account")

        # Find leads by email — most recent first
        stmt = (
            select(Lead)
            .where(Lead.email == user_email)
            .order_by(Lead.created_at.desc())
            .limit(5)
        )
        result = await db.execute(stmt)
        leads_list = result.scalars().all()

        if not leads_list:
            raise HTTPException(
                status_code=404,
                detail="No project found for this account. Please submit a project at nexabuilder.com/get-quote/"
            )

        return {
            "leads": [_lead_to_dict(l) for l in leads_list],
            "most_recent": _lead_to_dict(leads_list[0]),
            "total": len(leads_list),
            "email": user_email,
        }




@router.get("/my-projects")
async def get_my_projects(
    identity: dict = Depends(get_current_user),
):
    """
    Returns all projects for the authenticated user, grouped by property address.
    Each address group contains all verticals assessed for that property.
    Powers the member portal multi-project dashboard.
    """
    from app.models.user import User
    from uuid import UUID

    user_obj = identity.get("user")
    user_email = None
    if user_obj and hasattr(user_obj, "email"):
        user_email = user_obj.email

    user_id = identity.get("sub") or identity.get("user_id")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        if not user_email and user_id:
            try:
                user = await db.get(User, UUID(str(user_id)))
                if user:
                    user_email = user.email
            except Exception:
                pass

        if not user_email:
            raise HTTPException(status_code=404, detail="No projects found")

        # Get all leads for this user, ordered newest first
        stmt = (
            select(Lead)
            .where(Lead.email == user_email)
            .order_by(Lead.created_at.desc())
        )
        result = await db.execute(stmt)
        all_leads = result.scalars().all()

        if not all_leads:
            raise HTTPException(
                status_code=404,
                detail="No projects found. Start your first assessment at nexabuilder.com/get-quote/"
            )

        # Group by normalized address (postal_code + address_line1 or just postal_code)
        # Key: postal_code|address_line1 or just postal_code if no address
        from collections import defaultdict
        address_groups = defaultdict(list)

        for lead in all_leads:
            addr_key = (lead.postal_code or "unknown") + "|" + (lead.address_line1 or "")
            address_groups[addr_key].append(_lead_to_dict(lead))

        # Build grouped response
        properties = []
        for addr_key, lead_list in address_groups.items():
            parts = addr_key.split("|", 1)
            postal = parts[0]
            address = parts[1] if len(parts) > 1 and parts[1] else None

            # Use most recent lead's city/state for the property
            most_recent = lead_list[0]
            properties.append({
                "address_key": addr_key,
                "address_line1": address or None,
                "postal_code": postal,
                "city": most_recent.get("city"),
                "state": most_recent.get("state"),
                "project_count": len(lead_list),
                "projects": lead_list,
            })

        return {
            "email": user_email,
            "property_count": len(properties),
            "total_projects": len(all_leads),
            "properties": properties,
        }

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
            "lead_status": getattr(lead, "lead_status", "submitted") or "submitted",
            "assigned_contractor_id": getattr(lead, "assigned_contractor_id", None),
            "internal_notes": getattr(lead, "internal_notes", None),
        }


LEAD_STATUSES = ['submitted','review','matched','site_visit','quote','approved','complete','cancelled']
STATUS_LABELS = {'submitted':'Project Submitted','review':'Under Review','matched':'Matched with Provider',
    'site_visit':'Site Visit Scheduled','quote':'Quote in Progress','approved':'Quote Approved',
    'complete':'Project Complete','cancelled':'Cancelled'}


@router.patch("/{lead_id}/status")
async def update_lead_status(
    lead_id: int, status: str, notes: str = None,
    contractor_id: str = None, identity: dict = Depends(get_current_user),
):
    from fastapi import HTTPException
    from datetime import datetime
    from sqlalchemy import select as sa_select
    if status not in LEAD_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(sa_select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        old_status = getattr(lead, "lead_status", "submitted") or "submitted"
        lead.lead_status = status
        lead.status_updated_at = datetime.utcnow()
        if notes:
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
            existing = getattr(lead, "internal_notes", "") or ""
            lead.internal_notes = "[" + ts + "] " + notes + "\n" + existing
        if contractor_id:
            lead.assigned_contractor_id = contractor_id
            lead.assigned_at = datetime.utcnow()
        await db.commit()
        return {"lead_id": lead_id, "old_status": old_status, "new_status": status,
                "status_label": STATUS_LABELS.get(status, status)}
