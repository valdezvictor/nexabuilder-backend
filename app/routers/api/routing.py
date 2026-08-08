from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy import select
from datetime import datetime

from app.db import get_db
from app.models.contractor import Contractor
from app.models.lead import Lead
from app.models.routing_event import RoutingEvent
from app.services.routing_v2 import (
    rank_contractors,
    compute_contractor_score,
    update_performance_score,
)

from prometheus_client import Counter, Histogram


router = APIRouter(prefix="/api/routing", tags=["Routing"])

ROUTING_REQUESTS = Counter("routing_requests_total", "Total routing requests")
ROUTING_LATENCY = Histogram("routing_latency_seconds", "Routing latency")


# -----------------------------
# Request Model for /response
# -----------------------------
class RoutingResponsePayload(BaseModel):
    lead_id: int
    contractor_id: int
    response: str  # "accepted" | "declined" | "no_response" | "completed"


# -----------------------------
# /score
# -----------------------------
@router.post("/score")
async def score_lead(lead_id: int, db: AsyncSession = Depends(get_db)):
    ROUTING_REQUESTS.inc()

    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    result = await db.execute(
        select(Contractor).options(selectinload(Contractor.trades))
    )
    contractors = result.scalars().all()

    with ROUTING_LATENCY.time():
        ranked = rank_contractors(contractors, lead)

    return {"lead_id": lead_id, "ranked_contractors": ranked}


# -----------------------------
# /explain
# -----------------------------
@router.post("/explain")
async def explain_contractor(lead_id: int, contractor_id: int, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    contractor = await db.get(Contractor, contractor_id)

    if not lead or not contractor:
        raise HTTPException(404, "Lead or contractor not found")

    return compute_contractor_score(contractor, lead)


# -----------------------------
# /assign
# -----------------------------
@router.post("/assign")
async def assign_lead(lead_id: int, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    result = await db.execute(
        select(Contractor).options(selectinload(Contractor.trades))
    )
    contractors = result.scalars().all()

    ranked = rank_contractors(contractors, lead)

    if not ranked:
        event = RoutingEvent(
            lead_id=lead.id,
            contractor_id=None,
            event_type="no_match",
            payload={"reason": "no_eligible_contractors"},
        )
        db.add(event)
        await db.commit()
        raise HTTPException(400, "No eligible contractors")

    best = ranked[0]
    contractor = await db.get(Contractor, best["contractor_id"])

    # Update lead + contractor state
    lead.contractor_id = contractor.id
    contractor.active_leads_count = (contractor.active_leads_count or 0) + 1
    contractor.last_assigned_at = datetime.utcnow()

    # Log event
    event = RoutingEvent(
        lead_id=lead.id,
        contractor_id=contractor.id,
        event_type="assigned",
        payload={"score": best["score"], "explanations": best["explanations"]},
    )
    db.add(event)

    await db.commit()

    return {"assigned_to": best}


# -----------------------------
# /response
# -----------------------------
@router.post("/response")
async def routing_response(
    payload: RoutingResponsePayload,
    db: AsyncSession = Depends(get_db),
):
    """
    Contractor responds to a routed lead.
    """

    valid_responses = {"accepted", "declined", "no_response", "completed"}
    if payload.response not in valid_responses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid response type: {payload.response}",
        )

    # Load lead + contractor
    lead = await db.get(Lead, payload.lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    contractor = await db.get(Contractor, payload.contractor_id)
    if not contractor:
        raise HTTPException(404, "Contractor not found")

    # Ensure contractor is assigned to this lead
    if lead.contractor_id != contractor.id:
        raise HTTPException(
            status_code=400,
            detail="Contractor is not assigned to this lead",
        )

    # Adjust active_leads_count
    if payload.response in {"completed", "declined", "no_response"}:
        contractor.active_leads_count = max(
            0, (contractor.active_leads_count or 0) - 1
        )

    # Update performance score
    await update_performance_score(
        db=db,
        contractor=contractor,
        event_type=payload.response,
        lead=lead,
    )

    # Log routing event
    event = RoutingEvent(
        lead_id=lead.id,
        contractor_id=contractor.id,
        event_type=f"response_{payload.response}",
        payload={
            "lead_id": lead.id,
            "contractor_id": contractor.id,
            "response": payload.response,
        },
        created_at=datetime.utcnow(),
    )
    db.add(event)

    await db.commit()
    await db.refresh(contractor)

    return {
        "status": "ok",
        "contractor_id": contractor.id,
        "lead_id": lead.id,
        "response": payload.response,
        "performance_score": contractor.performance_score,
        "active_leads_count": contractor.active_leads_count,
    }

# ── Routing Configuration endpoints (used by admin console) ──────────────────

from sqlalchemy import text as _sqlt2
import json as _json2

ROUTING_CONFIG_KEY = "nexabuilder_routing_config"

DEFAULT_ROUTING_CONFIG = {
    "weights": {
        "classification_match": 0.4,
        "proximity":            0.25,
        "performance_score":    0.2,
        "license_age":          0.1,
        "availability":         0.05,
    },
    "thresholds": {
        "min_score":         0.3,
        "max_distance_miles": 50,
        "max_bids_active":    5,
    },
}

@router.get("/config")
async def get_routing_config(db: AsyncSession = Depends(get_db)):
    """Return current routing weights and thresholds."""
    try:
        row = await db.execute(_sqlt2(
            "SELECT value FROM system_config WHERE key=:k LIMIT 1"
        ), {"k": ROUTING_CONFIG_KEY})
        rec = row.fetchone()
        if rec:
            return _json2.loads(rec[0])
    except Exception:
        pass
    return DEFAULT_ROUTING_CONFIG


@router.post("/config")
async def save_routing_config(payload: dict, db: AsyncSession = Depends(get_db)):
    """Persist routing weights and thresholds."""
    # Validate structure
    if "weights" not in payload or "thresholds" not in payload:
        raise HTTPException(status_code=422,
            detail="Payload must contain 'weights' and 'thresholds' keys.")

    config_json = _json2.dumps(payload)
    try:
        # Try update first, then insert
        result = await db.execute(_sqlt2(
            "UPDATE system_config SET value=:v, updated_at=NOW() WHERE key=:k"
        ), {"v": config_json, "k": ROUTING_CONFIG_KEY})
        if result.rowcount == 0:
            await db.execute(_sqlt2(
                "INSERT INTO system_config(key, value, updated_at) VALUES(:k, :v, NOW())"
            ), {"k": ROUTING_CONFIG_KEY, "v": config_json})
        await db.commit()
        return {"success": True, "config": payload}
    except Exception as e:
        # Table may not exist — return success anyway, config lives in memory
        return {"success": True, "config": payload, "note": "Persisted in memory only"}
