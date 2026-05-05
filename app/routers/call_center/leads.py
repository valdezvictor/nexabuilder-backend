from fastapi import APIRouter, Form, Depends, Request, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional, List, Dict, Any
import time as _time

from app.db import get_db
from app.models.lead import Lead
from app.services.settings import get_setting

router = APIRouter()

# ---------- Lead index (Used by Call Center) ----------
@router.get("/leads/")
async def lead_index(db: AsyncSession = Depends(get_db)):
    # Now that the DB is synced, we can query the columns directly
    result = await db.execute(select(Lead).order_by(Lead.created_at.desc()))
    leads = result.scalars().all()

    return [
        {
            "id": lead.id,
            "created_at": lead.created_at.isoformat() + "Z" if lead.created_at else None,
            "status": lead.state,
            "first_name": lead.first_name or "N/A", 
            "last_name": lead.last_name or "",
            "vertical": lead.vertical,
            "source": lead.source,
            "postal_code": lead.postal_code,
            "lead_score": lead.ai_score, # Mapping DB ai_score to Frontend lead_score
            #"routing_tier": lead.routing_tier,
        }
        for lead in leads
    ]

# ---------- Mark contacted ----------
@router.post("/api/mark-contacted/{lead_id}")
async def mark_contacted(lead_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Lead).filter(Lead.id == lead_id))
    lead = result.scalar_one_or_none()

    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead.contacted = True
    await db.commit()
    return {"status": "success", "lead_id": lead_id}

# ---------- DataTables (Admin View) ----------
@router.get("/api/leads")
async def datatables_leads(request: Request, db: AsyncSession = Depends(get_db)):
    draw = int(request.query_params.get("draw", 1))
    start = int(request.query_params.get("start", 0))
    length = int(request.query_params.get("length", 25))

    # Count total leads
    count_result = await db.execute(select(func.count(Lead.id)))
    total = count_result.scalar()

    # Get paginated leads
    stmt = select(Lead).order_by(Lead.created_at.desc()).offset(start).limit(length)
    result = await db.execute(stmt)
    leads = result.scalars().all()

    data = []
    for lead in leads:
        data.append({
            "id": lead.id,
            "created_at": lead.created_at.strftime("%Y-%m-%d %H:%M") if lead.created_at else "-",
            "name": f"{lead.first_name} {lead.last_name}",
            "vertical": lead.vertical,
            "postal_code": lead.postal_code,
            "lead_score": lead.lead_score or "-",
            "routing_tier": lead.routing_tier or "-",
            "status": lead.status,
        })

    return {
        "draw": draw,
        "recordsTotal": total,
        "recordsFiltered": total,
        "data": data,
    }

# ---------- Latest lead ID ----------
@router.get("/api/leads/latest-id")
async def latest_lead_id(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Lead).order_by(Lead.id.desc()).limit(1))
    latest = result.scalar_one_or_none()
    return {"latest_id": latest.id if latest else 0}

# ---------- Create Lead (Restored & Async) ----------
@router.post("/leads/create")
async def create_lead(
    request: Request,
    db: AsyncSession = Depends(get_db),
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    city: str = Form(...),
    state: str = Form(...),
    vertical: str = Form(...),
    project_type: str = Form(...),
    budget_min: int = Form(...),
    budget_max: int = Form(...),
    source: Optional[str] = Form("manual"),
):
    if not email:
        email = f"noemail+{int(_time.time())}@nexabuilder.com"

    new_lead = Lead(
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        city=city,
        state=state,
        vertical=vertical,
        # ... map other fields based on your model
    )

    db.add(new_lead)
    await db.commit()
    await db.refresh(new_lead)
    return {"status": "success", "lead_id": new_lead.id}

# ---------- Settings (Restored & Async) ----------
@router.get("/api/settings/new-lead-banner")
async def get_new_lead_banner_setting(db: AsyncSession = Depends(get_db)):
    # Safely get the setting
    enabled = get_setting(db, "enable_new_lead_banner")
    return {"enabled": enabled}
