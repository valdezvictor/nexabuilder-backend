from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db import get_db

router = APIRouter(prefix="/contractor-dashboard", tags=["Contractor Dashboard"])


def get_contractor_or_404(db: Session, contractor_id: int):
    contractor = db.query(Contractor).filter(Contractor.id == contractor_id).first()
    if not contractor:
        raise HTTPException(404, "Contractor not found")
    return contractor


# ---------------------------------------------------------
# 1. Contractor Summary
# ---------------------------------------------------------
@router.get("/{contractor_id}/summary")
def contractor_summary(contractor_id: int, db: Session = Depends(get_db)):
    c = get_contractor_or_404(db, contractor_id)

    return {
        "id": c.id,
        "business_name": c.business_name,
        "legal_name": c.legal_name,
        "email_primary": c.email_primary,
        "phone_primary": c.phone_primary,
        "postal_code": c.postal_code,
        "state_code": c.state_code,
        "license_number": c.license_number,
        "license_status": c.license_status,
        "is_available": c.is_available,
        "daily_capacity": c.daily_capacity,
        "weekly_capacity": c.weekly_capacity,
        "leads_today": c.leads_today,
        "leads_this_week": c.leads_this_week,
        "acceptance_rate": c.acceptance_rate,
        "tier": c.tier,
    }


# ---------------------------------------------------------
# 2. Performance Metrics
# ---------------------------------------------------------
@router.get("/{contractor_id}/metrics")
def contractor_metrics(contractor_id: int, db: Session = Depends(get_db)):
    get_contractor_or_404(db, contractor_id)

    offers = (
        db.query(LeadOffer)
        .filter(LeadOffer.contractor_id == contractor_id)
        .all()
    )

    accepted = sum(1 for o in offers if o.accepted)
    declined = sum(1 for o in offers if o.declined)
    total = len(offers)

    logs = (
        db.query(RoutingDecisionLog)
        .filter(RoutingDecisionLog.contractor_id == contractor_id)
        .all()
    )

    ai_scores = [l.ai_score for l in logs if l.ai_score is not None]
    routing_scores = [l.routing_score for l in logs if l.routing_score is not None]

    return {
        "total_offers": total,
        "accepted": accepted,
        "declined": declined,
        "acceptance_rate": accepted / total if total else 0,
        "avg_ai_score": sum(ai_scores) / len(ai_scores) if ai_scores else None,
        "avg_routing_score": sum(routing_scores) / len(routing_scores) if routing_scores else None,
    }


# ---------------------------------------------------------
# 3. Recent Leads
# ---------------------------------------------------------
@router.get("/{contractor_id}/leads")
def contractor_leads(contractor_id: int, db: Session = Depends(get_db)):
    get_contractor_or_404(db, contractor_id)

    offers = (
        db.query(LeadOffer)
        .filter(LeadOffer.contractor_id == contractor_id)
        .order_by(LeadOffer.invited_at.desc())
        .limit(50)
        .all()
    )

    result = []
    for o in offers:
        lead = db.query(Lead).filter(Lead.id == o.lead_id).first()
        if not lead:
            continue

        result.append({
            "lead_id": lead.id,
            "vertical": lead.vertical,
            "postal_code": lead.postal_code,
            "budget_max": lead.budget_max,
            "invited_at": o.invited_at.isoformat() + "Z" if o.invited_at else None,
            "accepted": o.accepted,
            "declined": o.declined,
        })

    return result


# ---------------------------------------------------------
# 4. Routing Insights
# ---------------------------------------------------------
@router.get("/{contractor_id}/routing-insights")
def contractor_routing_insights(contractor_id: int, db: Session = Depends(get_db)):
    get_contractor_or_404(db, contractor_id)

    logs = (
        db.query(RoutingDecisionLog)
        .filter(RoutingDecisionLog.contractor_id == contractor_id)
        .order_by(RoutingDecisionLog.created_at.desc())
        .limit(100)
        .all()
    )

    return [
        {
            "lead_id": log.lead_id,
            "included": log.included,
            "exclusion_reasons": log.exclusion_reasons,
            "ai_score": log.ai_score,
            "ai_label": log.ai_label,
            "ai_confidence": log.ai_confidence,
            "rules_score": log.rules_score,
            "routing_score": log.routing_score,
            "contractor_snapshot": {
                "name": log.contractor_name,
                "available": log.contractor_available,
                "daily_capacity": log.contractor_daily_capacity,
                "leads_today": log.contractor_leads_today,
            },
            "created_at": log.created_at.isoformat() + "Z",
        }
        for log in logs
    ]


# ---------------------------------------------------------
# 5. Coverage & Preferences
# ---------------------------------------------------------
@router.get("/{contractor_id}/coverage")
def contractor_coverage(contractor_id: int, db: Session = Depends(get_db)):
    get_contractor_or_404(db, contractor_id)
    return db.query(ContractorCoverage).filter(ContractorCoverage.contractor_id == contractor_id).all()


@router.get("/{contractor_id}/verticals")
def contractor_verticals(contractor_id: int, db: Session = Depends(get_db)):
    get_contractor_or_404(db, contractor_id)
    return db.query(ContractorVerticalPreference).filter(ContractorVerticalPreference.contractor_id == contractor_id).all()


@router.get("/{contractor_id}/project-types")
def contractor_project_types(contractor_id: int, db: Session = Depends(get_db)):
    get_contractor_or_404(db, contractor_id)
    return db.query(ContractorProjectType).filter(ContractorProjectType.contractor_id == contractor_id).all()
