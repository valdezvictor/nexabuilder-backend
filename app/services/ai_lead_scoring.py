from typing import Dict

# In the future this can call a real ML model or external service.
# For now, it's a deterministic function that behaves like an AI score.

def predict_lead_quality(features: Dict) -> Dict:
    """
    Returns:
        {
            "ai_score": int (0-100),
            "ai_confidence": float (0.0-1.0),
            "ai_label": str ("Low" | "Medium" | "High" | "Premium")
        }
    """
    base = 50

    # Simple heuristic using existing fields as "features"
    if features.get("has_valid_phone"):
        base += 10
    if features.get("has_valid_email"):
        base += 10
    if features.get("high_budget"):
        base += 10
    if features.get("vertical") in {"C-39", "C-10"}:
        base += 5

    ai_score = max(0, min(100, base))

    if ai_score >= 90:
        label = "Premium"
        confidence = 0.9
    elif ai_score >= 70:
        label = "High"
        confidence = 0.8
    elif ai_score >= 50:
        label = "Medium"
        confidence = 0.6
    else:
        label = "Low"
        confidence = 0.5

    return {
        "ai_score": ai_score,
        "ai_confidence": confidence,
        "ai_label": label,
    }
