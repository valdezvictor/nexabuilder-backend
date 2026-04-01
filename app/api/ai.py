from fastapi import APIRouter, HTTPException
from app.services.ai_lead_scoring import predict_lead_quality

router = APIRouter(prefix="/api/ai", tags=["AI"])

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
