from math import radians, sin, cos, sqrt, atan2
from typing import List, Dict, Any, Optional
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai_lead_scoring import predict_lead_quality
from app.models.routing_event import RoutingEvent


# -----------------------------
# Distance calculation (Haversine)
# -----------------------------
def haversine_distance(lat1, lon1, lat2, lon2) -> Optional[float]:
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return None

    R = 3958.8  # miles
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)

    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    c = 2 * atan2(sqrt(a), sqrt(1 - a))

    return R * c


# -----------------------------
# Contractor eligibility
# -----------------------------
def contractor_is_eligible(contractor, lead) -> bool:
    # Active flag
    if getattr(contractor, "is_active", True) is False:
        return False

    # Capacity
    daily_capacity = getattr(contractor, "daily_capacity", None)
    active_count = getattr(contractor, "active_leads_count", 0) or 0

    if daily_capacity is not None and active_count >= daily_capacity:
        return False

    # Distance / service radius
    if (
        contractor.latitude is not None
        and contractor.longitude is not None
        and lead.latitude is not None
        and lead.longitude is not None
    ):
        dist = haversine_distance(
            contractor.latitude,
            contractor.longitude,
            lead.latitude,
            lead.longitude,
        )
        if (
            dist is not None
            and contractor.service_radius is not None
            and dist > contractor.service_radius
        ):
            return False

    # Vertical match (trade)
    if lead.vertical:
        # NOTE: this assumes trades are eagerly loaded in the query
        trades = getattr(contractor, "trades", []) or []
        contractor_verticals = {t.name.lower() for t in trades}
        if lead.vertical.lower() not in contractor_verticals:
            return False

    return True


# -----------------------------
# Scoring components
# -----------------------------
def score_distance(contractor, lead) -> float:
    dist = haversine_distance(
        contractor.latitude,
        contractor.longitude,
        lead.latitude,
        lead.longitude,
    )
    if dist is None:
        # Fallback when we don't have geo data
        return 0.3
    # Linear decay: 1.0 at 0 miles, 0.0 at 50+ miles
    return max(0.0, 1.0 - (dist / 50.0))


def score_vertical(contractor, lead) -> float:
    if not lead.vertical:
        return 0.5
    trades = getattr(contractor, "trades", []) or []
    contractor_verticals = {t.name.lower() for t in trades}
    return 1.0 if lead.vertical.lower() in contractor_verticals else 0.0


def score_ai(lead) -> float:
    if getattr(lead, "ai_score", None) is not None:
        return float(lead.ai_score)

    features = {
        "phone": lead.phone,
        "email": lead.email,
        "budget_max": lead.budget_max,
        "vertical": lead.vertical,
    }
    result = predict_lead_quality(features)
    return float(result.get("ai_score", 0.5))


def score_performance(contractor) -> float:
    if contractor.performance_score is None:
        return 0.5
    return max(0.0, min(1.0, float(contractor.performance_score)))


# -----------------------------
# Composite score
# -----------------------------
def compute_contractor_score(contractor, lead) -> Dict[str, Any]:
    explanations: List[str] = []

    dist_score = score_distance(contractor, lead)
    explanations.append(f"Distance score: {dist_score:.2f}")

    vert_score = score_vertical(contractor, lead)
    explanations.append(f"Vertical match: {vert_score:.2f}")

    ai_score = score_ai(lead)
    explanations.append(f"AI lead score: {ai_score:.2f}")

    perf_score = score_performance(contractor)
    explanations.append(f"Performance score: {perf_score:.2f}")

    # Weighted blend
    final_score = (
        dist_score * 0.30
        + vert_score * 0.30
        + ai_score * 0.25
        + perf_score * 0.15
    )

    return {
        "contractor_id": contractor.id,
        "score": round(final_score, 4),
        "explanations": explanations,
    }


# -----------------------------
# Ranking function
# -----------------------------
def rank_contractors(contractors, lead) -> List[Dict[str, Any]]:
    eligible = [c for c in contractors if contractor_is_eligible(c, lead)]
    scored = [compute_contractor_score(c, lead) for c in eligible]
    return sorted(scored, key=lambda x: x["score"], reverse=True)


# -----------------------------
# Performance score updates
# -----------------------------
async def update_performance_score(
    db: AsyncSession,
    contractor,
    event_type: str,
    lead: Optional[object] = None,
) -> None:
    """
    Adjust contractor.performance_score based on routing outcome.

    event_type:
      - "accepted"
      - "completed"
      - "declined"
      - "no_response"
      - "auto_expired"
    """

    delta = 0.0

    if event_type == "accepted":
        delta = 0.05
        # Bonus for high-quality leads
        if lead is not None and getattr(lead, "ai_score", None) is not None:
            if float(lead.ai_score) > 0.8:
                delta += 0.02

    elif event_type == "completed":
        delta = 0.10

    elif event_type == "declined":
        delta = -0.05

    elif event_type == "no_response":
        delta = -0.10

    elif event_type == "auto_expired":
        delta = -0.15

    current = float(contractor.performance_score or 0.5)
    new_score = max(0.0, min(1.0, current + delta))
    contractor.performance_score = new_score

    event = RoutingEvent(
        lead_id=getattr(lead, "id", None),
        contractor_id=contractor.id,
        event_type=f"performance_{event_type}",
        payload={
            "delta": delta,
            "new_score": new_score,
        },
        created_at=datetime.utcnow(),
    )
    db.add(event)
    # Commit is left to the caller to control transaction boundaries.
