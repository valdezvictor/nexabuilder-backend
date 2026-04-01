from fastapi import APIRouter, Depends, HTTPException
from app.services.ai_lead_scoring import predict_lead_quality
from sqlalchemy.orm import Session
from app.db import get_db

from app.services.ai_lead_scoring import predict_lead_quality

router = APIRouter(prefix="/api/ai", tags=["AI"])

# AI Lead Score
@router.post("/lead-score")
def ai_lead_score(payload: dict):
    try:
        features = {
            "phone": payload.get("phone"),
            "email": payload.get("email"),
            "budget_max": payload.get("budget_max"),
            "vertical": payload.get("vertical"),
        }

        result = predict_lead_quality(features)
        return {
            "ai_score": result.get("ai_score"),
            "explanations": result.get("explanations", []),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Routing Summary
@router.post("/routing-summary")
def routing_summary(lead_id: int, db: Session = Depends(get_db)):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    (
        log,
        explanation_list,
        scored_contractors,
        alerts,
        requires_financing_escalation,
        summary,
    ) = route_lead(lead, db)

    ai_summary = build_ai_routing_summary(
        lead=lead,
        summary=summary,
        alerts=alerts,
        scored_contractors=scored_contractors,
    )

    return ai_summary
