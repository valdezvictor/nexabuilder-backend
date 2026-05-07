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
