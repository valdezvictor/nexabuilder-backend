# app/routers/api/lead_intake.py
# Public endpoint for member portal lead submission
# No auth required — this is how new leads enter the system

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import insert

from app.db import get_sessionmaker
from app.models.lead import Lead

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
    Public endpoint — no auth required.
    Accepts lead submission from member portal intake form.
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        lead = Lead(
            vertical=payload.vertical,
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
            phone=payload.phone,
            postal_code=payload.postal_code,
        )
        db.add(lead)
        await db.commit()
        await db.refresh(lead)

        return {
            "id":      lead.id,
            "message": "Lead submitted successfully",
            "email":   lead.email,
        }
