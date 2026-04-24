from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.models.lead import Lead
from app.models.contractor import Contractor
from app.models.routing_event import RoutingEvent
from app.schemas.lead_timeline import LeadTimelineResponse, TimelineEvent, AssignedContractor


router = APIRouter(prefix="/api/leads", tags=["Leads"])


@router.get("/{lead_id}/timeline", response_model=LeadTimelineResponse)
async def get_lead_timeline(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
):
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "Lead not found")

    contractor = None
    if lead.contractor_id:
        contractor = await db.get(Contractor, lead.contractor_id)

    result = await db.execute(
        select(RoutingEvent)
        .where(RoutingEvent.lead_id == lead.id)
        .order_by(RoutingEvent.created_at.asc())
    )
    events = result.scalars().all()

    timeline_events = [
        TimelineEvent(
            id=e.id,
            event_type=e.event_type,
            payload=e.payload,
            created_at=e.created_at,
            contractor_id=e.contractor_id,
        )
        for e in events
    ]

    performance_deltas = [
        float(e.payload.get("delta"))
        for e in events
        if e.event_type.startswith("performance_")
        and isinstance(e.payload, dict)
        and "delta" in e.payload
    ]

    contractor_payload = None
    if contractor:
        contractor_payload = AssignedContractor(
            id=contractor.id,
            name=getattr(contractor, "name", ""),
            performance_score=contractor.performance_score,
        )

    return LeadTimelineResponse(
        id=lead.id,
        vertical=lead.vertical,
        city=lead.city,
        state=lead.state,
        ai_score=getattr(lead, "ai_score", None),
        contractor=contractor_payload,
        events=timeline_events,
        performance_deltas=performance_deltas,
    )
