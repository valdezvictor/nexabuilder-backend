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





@router.get("/{lead_id}/timeline")
async def get_lead_timeline(
    lead_id: int,
    identity: dict = Depends(get_current_user),
):
    """
    Full activity timeline for a lead.
    Combines: status milestones + routing events + click events.
    Used by member portal, contractor portal, and call center portal.
    """
    from app.models.routing_event import RoutingEvent

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Get lead
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Lead not found")

        # Get all routing events
        from sqlalchemy import text
        r_events = await db.execute(text(
            "SELECT re.event_type, re.payload, re.created_at, "
            "c.business_name, c.license_no "
            "FROM routing_events re "
            "LEFT JOIN contractors c ON c.id = re.contractor_id "
            "WHERE re.lead_id = :lid "
            "ORDER BY re.created_at ASC"
        ), {"lid": lead_id})
        raw_events = r_events.fetchall()

    # ── Build timeline entries ─────────────────────────────────────────────
    entries = []

    # 1. Lead created (always first)
    entries.append({
        "id": "created",
        "timestamp": lead.created_at.isoformat() if lead.created_at else None,
        "type": "milestone",
        "actor": "system",
        "icon": "📋",
        "title": "Project Submitted",
        "description": f"{lead.project_type or lead.vertical} assessment submitted for {lead.postal_code}",
        "status": "done",
        "meta": {
            "vertical": lead.vertical,
            "project_type": lead.project_type,
            "source": lead.source,
        }
    })

    # 2. AI Assessment (infer from ai_assessment presence)
    if lead.ai_assessment:
        ai = lead.ai_assessment
        entries.append({
            "id": "ai_assessed",
            "timestamp": lead.created_at.isoformat() if lead.created_at else None,
            "type": "milestone",
            "actor": "ai",
            "icon": "🤖",
            "title": "AI Assessment Complete",
            "description": f"Score {ai.get('complexity_score', '-')}/10 · {ai.get('estimated_cost_range', '')} · {ai.get('complexity_label', '')}",
            "status": "done",
            "meta": {
                "score": ai.get("complexity_score"),
                "cost_range": ai.get("estimated_cost_range"),
                "permit_required": ai.get("permit_required"),
                "license_types": ai.get("license_types_needed", []),
            }
        })

    # 3. Routing events from DB
    event_type_map = {
        # Contractor matching
        "contractor_matched":        ("👷", "Contractor Match Found",      "contractor", "milestone"),
        "bid_sent":                  ("📤", "Bid Invitation Sent",          "system",     "action"),
        "bid_accepted":              ("✅", "Contractor Accepted Bid",      "contractor", "milestone"),
        "bid_declined":              ("❌", "Contractor Declined",          "contractor", "action"),
        "bid_expired":               ("⏰", "Bid Invitation Expired",       "system",     "action"),
        # Status changes
        "status_changed":            ("🔄", "Status Updated",              "admin",      "action"),
        "site_visit_scheduled":      ("📅", "Site Visit Scheduled",        "contractor", "milestone"),
        "quote_sent":                ("📊", "Quote Submitted",              "contractor", "milestone"),
        "quote_approved":            ("🤝", "Quote Approved",               "member",     "milestone"),
        "project_started":           ("🏗",  "Project Started",             "contractor", "milestone"),
        "project_completed":         ("🏆", "Project Completed",           "contractor", "milestone"),
        # Member actions
        "financing_modal_opened":    ("💰", "Viewed Financing Options",    "member",     "engagement"),
        "financing_interest_click":  ("💰", "Requested Financing Info",   "member",     "engagement"),
        "financing_lead_submitted":  ("💰", "Financing Application Sent", "member",     "engagement"),
        "insurance_modal_opened":    ("🛡",  "Viewed Insurance Options",   "member",     "engagement"),
        "insurance_interest_click":  ("🛡",  "Requested Insurance Review", "member",     "engagement"),
        "insurance_lead_submitted":  ("🛡",  "Insurance Request Sent",     "member",     "engagement"),
        "upgrade_selected":          ("⭐", "Materials Upgrade Selected",  "member",     "engagement"),
        "new_project_started":       ("➕", "New Project Added",            "member",     "action"),
        # Call center / agent
        "agent_called":              ("📞", "Agent Call",                  "agent",      "action"),
        "agent_note":                ("📝", "Agent Note Added",            "agent",      "action"),
        "otp_verified":              ("✅", "Identity Verified",            "system",     "milestone"),
        "magic_link_sent":           ("🔗", "Magic Link Sent",              "system",     "action"),
        "portal_accessed":           ("🏠", "Member Portal Accessed",       "member",     "action"),
    }

    for ev in raw_events:
        ev_type, payload, created_at, contractor_name, contractor_lic = ev
        payload = payload or {}

        if ev_type in event_type_map:
            icon, title, actor, ev_class = event_type_map[ev_type]
        else:
            icon, title, actor, ev_class = "📌", ev_type.replace("_", " ").title(), "system", "action"

        # Build description from payload
        desc_parts = []
        if contractor_name:
            desc_parts.append(f"Contractor: {contractor_name} (#{contractor_lic})")
        if payload.get("publisher_id"):
            desc_parts.append(f"Source: {payload['publisher_id']}")
        if payload.get("credit_range"):
            desc_parts.append(f"Credit: {payload['credit_range']}")
        if payload.get("coverage_types"):
            desc_parts.append(f"Coverage: {', '.join(payload['coverage_types'])}")
        if payload.get("estimated_monthly"):
            desc_parts.append(f"Est. monthly: ${payload['estimated_monthly']}")
        if payload.get("note"):
            desc_parts.append(payload["note"])

        entries.append({
            "id": f"{ev_type}_{created_at.timestamp() if created_at else 0}",
            "timestamp": created_at.isoformat() if created_at else None,
            "type": ev_class,
            "actor": actor,
            "icon": icon,
            "title": title,
            "description": " · ".join(desc_parts) if desc_parts else None,
            "status": "done",
            "meta": payload,
        })

    # 4. Current status → future milestones as pending
    STATUS_MILESTONES = [
        ("matched",    "👷", "Contractor Assigned"),
        ("site_visit", "📅", "Site Visit"),
        ("quote",      "📊", "Quote Delivered"),
        ("approved",   "🤝", "Quote Approved"),
        ("complete",   "🏆", "Project Complete"),
    ]

    current_status = lead.lead_status or "submitted"
    STATUS_ORDER = ["submitted", "review", "matched", "site_visit", "quote", "approved", "complete"]
    current_idx = STATUS_ORDER.index(current_status) if current_status in STATUS_ORDER else 0

    for status, icon, label in STATUS_MILESTONES:
        if status not in [STATUS_ORDER[i] for i in range(current_idx + 1)]:
            entries.append({
                "id": f"pending_{status}",
                "timestamp": None,
                "type": "milestone",
                "actor": "system",
                "icon": icon,
                "title": label,
                "description": "Pending",
                "status": "pending",
                "meta": {}
            })

    return {
        "lead_id": lead_id,
        "lead_status": current_status,
        "vertical": lead.vertical,
        "project_type": lead.project_type,
        "total_entries": len(entries),
        "entries": entries,
    }

@router.post("/{lead_id}/events")
async def track_lead_event(
    lead_id: int,
    body: dict,
    identity: dict = Depends(get_current_user),
):
    """
    Track a click or conversion event for a lead.
    Used for financing, insurance, and future network partner click tracking.
    Stores event_type + arbitrary payload (publisher_id, etc.) in routing_events.

    event_type examples:
      financing_modal_opened  financing_interest_click  financing_lead_submitted
      insurance_modal_opened  insurance_interest_click  insurance_lead_submitted
      new_project_started     upgrade_selected
    """
    from app.models.routing_event import RoutingEvent

    event_type  = body.get("event_type", "unknown")
    payload     = body.get("payload", {})

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        event = RoutingEvent(
            lead_id=lead_id,
            event_type=event_type,
            payload=payload,
        )
        db.add(event)
        await db.commit()

    return {"recorded": True, "event_type": event_type, "lead_id": lead_id}

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
