from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from app.db import get_db

router = APIRouter(prefix="/admin-console", tags=["Admin Console"])


# ---------------------------------------------------------
# 1. System Overview
# ---------------------------------------------------------
@router.get("/overview")
def system_overview(db: Session = Depends(get_db)):
    now = datetime.utcnow()
    today = now.date()
    week_start = today - timedelta(days=today.weekday())

    total_leads = db.query(Lead).count()
    leads_today = db.query(Lead).filter(Lead.created_at >= today).count()
    leads_this_week = db.query(Lead).filter(Lead.created_at >= week_start).count()

    contractors = db.query(Contractor).count()
    available_contractors = db.query(Contractor).filter(Contractor.is_available == True).count()

    routing_logs = db.query(RoutingDecisionLog).count()

    return {
        "total_leads": total_leads,
        "leads_today": leads_today,
        "leads_this_week": leads_this_week,
        "contractors_total": contractors,
        "contractors_available": available_contractors,
        "routing_logs_total": routing_logs,
    }


# ---------------------------------------------------------
# 2. Global Lead List
# ---------------------------------------------------------
@router.get("/leads")
def admin_lead_list(db: Session = Depends(get_db)):
    leads = (
        db.query(Lead)
        .order_by(Lead.created_at.desc())
        .limit(500)
        .all()
    )
    return [
        {
            "id": l.id,
            "first_name": l.first_name,
            "last_name": l.last_name,
            "vertical": l.vertical,
            "postal_code": l.postal_code,
            "created_at": l.created_at.isoformat() + "Z",
        }
        for l in leads
    ]


# ---------------------------------------------------------
# 3. Lead Details (JSON)
# ---------------------------------------------------------
@router.get("/leads/{lead_id}")
def admin_lead_details(lead_id: int, db: Session = Depends(get_db)):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Lead not found")

    offers = db.query(LeadOffer).filter(LeadOffer.lead_id == lead_id).all()
    external_posts = db.query(LeadExternalPost).filter(LeadExternalPost.lead_id == lead_id).all()
    logs = (
        db.query(RoutingDecisionLog)
        .filter(RoutingDecisionLog.lead_id == lead_id)
        .order_by(RoutingDecisionLog.created_at.desc())
        .all()
    )

    return {
        "lead": lead,
        "offers": offers,
        "external_posts": external_posts,
        "routing_logs": logs,
    }


# ---------------------------------------------------------
# 4. Routing Logs (global)
# ---------------------------------------------------------
@router.get("/routing-logs")
def admin_routing_logs(db: Session = Depends(get_db)):
    logs = (
        db.query(RoutingDecisionLog)
        .order_by(RoutingDecisionLog.created_at.desc())
        .limit(200)
        .all()
    )
    return logs


# ---------------------------------------------------------
# 5. Contractor List (global)
# ---------------------------------------------------------
@router.get("/contractors")
def admin_contractors(db: Session = Depends(get_db)):
    return db.query(Contractor).all()


# ---------------------------------------------------------
# 6. Routing Engine Settings (scaffold)
# ---------------------------------------------------------
@router.get("/routing/settings")
def get_routing_settings():
    return {
        "ai_weight": 0.30,
        "zip_radius_default": 50,
        "tier_thresholds": {
            "A": 90,
            "B": 70,
            "C": 50,
        },
    }


@router.post("/routing/settings")
def update_routing_settings(payload: dict):
    # TODO: persist settings in DB
    return {"status": "ok", "updated": payload}
