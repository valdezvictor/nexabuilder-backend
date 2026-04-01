from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from datetime import datetime
from app.db import get_db

from app.core.templates import templates

router = APIRouter(prefix="/contractor", tags=["Contractor Leads"])

# ---------- List contractor leads ----------
@router.get("/leads", response_class=HTMLResponse)
async def contractor_lead_list(
    request: Request,
    contractor_id: int,
    db: Session = Depends(get_db)
):
    contractor = db.query(Contractor).get(contractor_id)
    leads = db.query(Lead).filter(Lead.assigned_contractor_id == contractor_id).all()

    return templates.TemplateResponse(
        "contractor/leads/list.html",
        {"request": request, "contractor": contractor, "leads": leads}
    )

# ---------- Lead detail ----------
@router.get("/leads/{lead_id}", response_class=HTMLResponse)
async def contractor_lead_detail(
    request: Request,
    contractor_id: int,
    lead_id: int,
    db: Session = Depends(get_db)
):
    contractor = db.query(Contractor).get(contractor_id)
    lead = db.query(Lead).get(lead_id)

    return templates.TemplateResponse(
        "contractor/leads/detail.html",
        {"request": request, "contractor": contractor, "lead": lead}
    )

# ---------- Accept lead ----------
@router.post("/leads/{lead_id}/accept")
async def contractor_accept_lead(
    contractor_id: int,
    lead_id: int,
    db: Session = Depends(get_db)
):
    contractor = db.query(Contractor).get(contractor_id)
    lead = db.query(Lead).get(lead_id)

    # Update contractor performance
    contractor.accepted_leads += 1
    contractor.last_accepted_at = datetime.utcnow()
    contractor.acceptance_rate = contractor.accepted_leads / max(
        1, contractor.accepted_leads + contractor.declined_leads
    )

    # Mark lead as accepted
    lead.status = "accepted"
    lead.accepted_at = datetime.utcnow()

    db.commit()

    return RedirectResponse(
        f"/contractor/leads/{lead_id}?contractor_id={contractor_id}",
        status_code=303
    )

# ---------- Decline lead ----------
@router.post("/leads/{lead_id}/decline")
async def contractor_decline_lead(
    contractor_id: int,
    lead_id: int,
    db: Session = Depends(get_db)
):
    contractor = db.query(Contractor).get(contractor_id)
    lead = db.query(Lead).get(lead_id)

    # Update contractor performance
    contractor.declined_leads += 1
    contractor.last_declined_at = datetime.utcnow()
    contractor.acceptance_rate = contractor.accepted_leads / max(
        1, contractor.accepted_leads + contractor.declined_leads
    )

    # Mark lead as declined
    lead.status = "declined"
    lead.declined_at = datetime.utcnow()

    db.commit()

    return RedirectResponse(
        f"/contractor/leads/{lead_id}?contractor_id={contractor_id}",
        status_code=303
    )
