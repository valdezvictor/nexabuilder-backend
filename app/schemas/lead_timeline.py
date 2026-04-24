from typing import List, Optional, Any
from datetime import datetime
from pydantic import BaseModel


class TimelineEvent(BaseModel):
    id: int
    event_type: str
    payload: Any
    created_at: datetime
    contractor_id: Optional[int] = None


class AssignedContractor(BaseModel):
    id: int
    name: str
    performance_score: Optional[float] = None


class LeadTimelineResponse(BaseModel):
    id: int
    vertical: Optional[str]
    city: Optional[str]
    state: Optional[str]
    ai_score: Optional[float]
    contractor: Optional[AssignedContractor]
    events: List[TimelineEvent]
    performance_deltas: List[float]
