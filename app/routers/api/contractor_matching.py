# app/routers/api/contractor_matching.py
# Contractor matching engine using 243k CSLB contractors
# Internal routing: high-score SoCal leads go to Victor's crew first

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from pydantic import BaseModel
from typing import Optional
from app.core.auth import get_current_user
from app.db import get_sessionmaker
from app.models.lead import Lead
from app.services.sms import send_sms
import boto3, os

router = APIRouter(prefix="/api/leads", tags=["Contractor Matching"])

# ─── Internal routing config ───────────────────────────────────────────────
INTERNAL_CONTRACTOR = {
    "id": "victor-internal-001",
    "name": "Victor Valdez / NexaBuilder Crew",
    "phone": "+15624588855",
    "email": "victor@nexabuilder.com",
    "is_internal": True,
    "verticals": [
        "home_services", "new_construction", "roofing",
        "electrical", "plumbing", "landscaping"
    ],
    "classifications": ["B", "C-27", "C-10", "C-36", "C-53", "C-13"],
    "coverage_zips": ["90", "91", "92"],  # All SoCal prefixes
}

# Routing thresholds
INTERNAL_ROUTE_MIN_SCORE = 7        # complexity_score >= 7
INTERNAL_ROUTE_VERTICALS = ["home_services", "new_construction"]
SOOCAL_ZIP_PREFIXES = ("90", "91", "92")


def should_route_internal(lead: Lead, ai_assessment: dict) -> bool:
    """
    Returns True if this lead should go to Victor's internal crew first.
    Criteria: SoCal zip + home_services or new_construction + complexity >= 7
    """
    zip_code = (lead.postal_code or "")
    if not any(zip_code.startswith(p) for p in SOOCAL_ZIP_PREFIXES):
        return False
    if lead.vertical not in INTERNAL_ROUTE_VERTICALS:
        return False
    score = ai_assessment.get("complexity_score", 0)
    if score < INTERNAL_ROUTE_MIN_SCORE:
        return False
    return True


@router.post("/{lead_id}/match-contractors")
async def match_contractors(
    lead_id: int,
    identity: dict = Depends(get_current_user),
):
    """
    Match a lead to contractors from the CSLB database.
    1. Check internal routing first (Victor's crew)
    2. If not internal, find top CSLB contractors by zip + classification
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

    ai_assessment = lead.ai_assessment or {}

    # Step 1: Check internal routing
    internal_match = should_route_internal(lead, ai_assessment)

    if internal_match:
        # Notify Victor via SMS
        lead_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "New Lead"
        project = lead.project_type or lead.vertical or "project"
        score = ai_assessment.get("complexity_score", "N/A")
        cost = ai_assessment.get("estimated_cost_range", "TBD")

        sms_msg = (
            f"NexaBuilder INTERNAL LEAD: {lead_name} | {project} | "
            f"ZIP {lead.postal_code} | Score {score}/10 | {cost} | "
            f"Review: https://admin.nexabuilder.com/leads/{lead_id}"
        )
        send_sms(INTERNAL_CONTRACTOR["phone"], sms_msg)

        return {
            "lead_id": lead_id,
            "routing": "internal",
            "message": "Lead routed to internal crew for review",
            "matched_contractor": INTERNAL_CONTRACTOR,
            "routing_reason": f"SoCal ZIP {lead.postal_code}, {lead.vertical}, complexity {score}/10",
            "notification_sent": True,
        }

    # Step 2: Match from CSLB database
    license_types = ai_assessment.get("license_types_needed", [])
    zip_code = lead.postal_code or ""
    zip_prefix = zip_code[:3] if len(zip_code) >= 3 else zip_code

    # Build classification filter from AI assessment
    classification_filter = _build_classification_filter(
        lead.vertical or "", lead.project_type or "", license_types
    )

    async with SessionLocal() as db:
        query = text("""
            SELECT
                license_no, business_name, full_business_name,
                city, zip_code, phone, primary_status,
                classifications, county, email
            FROM contractors
            WHERE primary_status = 'CLEAR'
            AND zip_code LIKE :zip_prefix
            AND (
                :classification = '' OR
                classifications ILIKE :classification_like
            )
            ORDER BY
                CASE WHEN zip_code = :exact_zip THEN 0 ELSE 1 END,
                business_name
            LIMIT 10
        """)

        rows = await db.execute(query, {
            "zip_prefix": zip_prefix + "%",
            "classification": classification_filter,
            "classification_like": f"%{classification_filter}%",
            "exact_zip": zip_code,
        })
        contractors = rows.fetchall()

    if not contractors:
        # Widen search to county level
        async with SessionLocal() as db:
            query2 = text("""
                SELECT license_no, business_name, full_business_name,
                       city, zip_code, phone, primary_status,
                       classifications, county, email
                FROM contractors
                WHERE primary_status = 'CLEAR'
                AND state = 'CA'
                AND zip_code LIKE :state_prefix
                LIMIT 5
            """)
            rows2 = await db.execute(query2, {
                "state_prefix": zip_code[:2] + "%"
            })
            contractors = rows2.fetchall()

    matches = [{
        "license_no": r[0],
        "business_name": r[1] or r[2],
        "city": r[3],
        "zip_code": r[4],
        "phone": r[5],
        "status": r[6],
        "classifications": r[7],
        "county": r[8],
        "email": r[9],
        "is_internal": False,
    } for r in contractors]

    return {
        "lead_id": lead_id,
        "routing": "external",
        "lead_zip": zip_code,
        "vertical": lead.vertical,
        "classification_searched": classification_filter,
        "matches_found": len(matches),
        "contractors": matches,
    }


def _build_classification_filter(vertical: str, project_type: str, license_types: list) -> str:
    """Map project type to CSLB classification codes."""
    mapping = {
        "pool": "C-53",
        "Pool": "C-53",
        "roofing": "C-39",
        "Roofing": "C-39",
        "electrical": "C-10",
        "plumbing": "C-36",
        "framing": "C-5",
        "concrete": "C-8",
        "landscaping": "C-27",
        "hvac": "C-20",
        "new_construction": "B",
        "addition": "B",
        "remodel": "B",
    }
    for key, code in mapping.items():
        if key.lower() in project_type.lower() or key.lower() in vertical.lower():
            return code

    # Try license_types from AI assessment
    for lt in license_types:
        for key, code in mapping.items():
            if key.lower() in lt.lower():
                return code

    return "B"  # General contractor fallback
