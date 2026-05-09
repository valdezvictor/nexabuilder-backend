# app/routers/api/estimate.py
# On-demand estimate generation for any lead

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from app.core.auth import get_current_user
from app.db import get_sessionmaker
from app.models.lead import Lead
from app.services.estimator import generate_estimate
from app.services.ai_intake import assess_lead

router = APIRouter(prefix="/api/leads", tags=["Estimates"])


@router.post("/{lead_id}/estimate")
async def create_estimate(
    lead_id: int,
    identity: dict = Depends(get_current_user),
):
    """
    Generate a detailed cost estimate for a lead.
    Runs AI intake assessment + BLS-powered line-item estimator.
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

    # Run AI assessment first
    assessment = assess_lead(
        vertical=lead.vertical or "home_services",
        project_type=None,
        description=None,
        postal_code=lead.postal_code,
    )

    # Generate line-item estimate
    estimate = generate_estimate(
        vertical=lead.vertical or "home_services",
        project_type="General",
        description=None,
        postal_code=lead.postal_code,
        ai_assessment=assessment,
    )

    return {
        "lead_id": lead_id,
        "lead_name": f"{lead.first_name or ''} {lead.last_name or ''}".strip(),
        "vertical": lead.vertical,
        "postal_code": lead.postal_code,
        "ai_assessment": assessment,
        "estimate": estimate,
    }
