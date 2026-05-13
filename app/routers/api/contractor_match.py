# app/routers/api/contractor_match.py
# CSLB contractor matching engine
# Matches leads to licensed contractors by: vertical, classification, zip, status

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from typing import Optional
from app.core.auth import get_current_user
from app.db import get_sessionmaker

router = APIRouter(prefix="/api/contractors", tags=["Contractor Matching"])

# Map NexaBuilder verticals/project types to CSLB license classifications
CLASSIFICATION_MAP = {
    "pool": ["C-53", "C53"],
    "roofing": ["C-39", "C39"],
    "electrical": ["C-10", "C10"],
    "plumbing": ["C-36", "C36"],
    "hvac": ["C-20", "C20"],
    "framing": ["C-5", "C5"],
    "concrete": ["C-8", "C8"],
    "landscaping": ["C-27", "C27"],
    "painting": ["C-33", "C33"],
    "solar": ["C-46", "C46"],
    "general": ["B", "A"],
    "new_construction": ["A", "B"],
    "remodel": ["B"],
    "addition": ["B", "A"],
    "home_services": ["B", "C-36", "C-10", "C-39"],
    "retaining_wall": ["C-29", "C29"],
    "masonry": ["C-29", "C29"],
    "drywall": ["C-9", "C9"],
    "flooring": ["C-15", "C15"],
    "fencing": ["C-13", "C13"],
    "insulation": ["C-2", "C2"],
}


@router.get("/search")
async def search_contractors(
    zip_code: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    county: Optional[str] = Query(None),
    classification: Optional[str] = Query(None),
    project_type: Optional[str] = Query(None),
    status: str = Query("CLEAR"),
    limit: int = Query(20, le=100),
    identity: dict = Depends(get_current_user),
):
    """Search CSLB contractors by location and classification."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        conditions = ["primary_status = :status"]
        params = {"status": status, "limit": limit}

        if zip_code:
            conditions.append("zip_code = :zip_code")
            params["zip_code"] = zip_code

        if city:
            conditions.append("UPPER(city) LIKE :city")
            params["city"] = f"%{city.upper()}%"

        if county:
            conditions.append("UPPER(county) LIKE :county")
            params["county"] = f"%{county.upper()}%"

        # Map project type to classifications
        if project_type and project_type.lower() in CLASSIFICATION_MAP:
            classes = CLASSIFICATION_MAP[project_type.lower()]
            class_conditions = " OR ".join(
                [f"classifications LIKE '%{c}%'" for c in classes]
            )
            conditions.append(f"({class_conditions})")

        if classification:
            conditions.append("classifications LIKE :classification")
            params["classification"] = f"%{classification}%"

        where = " AND ".join(conditions)
        query = f"""
            SELECT license_no, business_name, full_business_name,
                   city, county, zip_code, phone, classifications,
                   primary_status, expiration_date, business_type
            FROM contractors
            WHERE {where}
            ORDER BY expiration_date DESC NULLS LAST
            LIMIT :limit
        """

        result = await db.execute(text(query), params)
        rows = result.fetchall()

        return {
            "count": len(rows),
            "filters": {"zip_code": zip_code, "city": city, "project_type": project_type, "status": status},
            "contractors": [
                {
                    "license_no": r[0],
                    "business_name": r[1] or r[2],
                    "city": r[3],
                    "county": r[4],
                    "zip_code": r[5],
                    "phone": r[6],
                    "classifications": r[7],
                    "status": r[8],
                    "expiration_date": r[9].isoformat() if r[9] else None,
                    "business_type": r[10],
                }
                for r in rows
            ]
        }


@router.get("/match/{lead_id}")
async def match_contractors_for_lead(
    lead_id: int,
    limit: int = Query(10, le=50),
    identity: dict = Depends(get_current_user),
):
    """
    Auto-match CSLB contractors to a specific lead.
    Uses lead's zip code, project type, and AI assessment to find best matches.
    """
    from sqlalchemy import select
    from app.models.lead import Lead

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Lead not found")

        # Get classifications from project type + AI assessment
        project_type = (lead.project_type or "").lower()
        vertical = (lead.vertical or "").lower()
        ai = lead.ai_assessment or {}
        license_types = ai.get("license_types_needed", [])

        # Build classification search list
        classes = []
        if project_type in CLASSIFICATION_MAP:
            classes.extend(CLASSIFICATION_MAP[project_type])
        if vertical in CLASSIFICATION_MAP:
            classes.extend(CLASSIFICATION_MAP[vertical])

        # Also use AI-recommended license types
        for lt in license_types:
            for key, vals in CLASSIFICATION_MAP.items():
                if any(v in lt for v in vals):
                    classes.extend(vals)

        if not classes:
            classes = ["B"]  # General contractor fallback

        # Build location search
        zip_code = lead.postal_code
        city = lead.city

        # Search by zip first, then expand to city
        class_conditions = " OR ".join([f"classifications LIKE '%{c}%'" for c in list(set(classes))[:6]])

        query = f"""
            SELECT license_no, business_name, full_business_name,
                   city, county, zip_code, phone, classifications,
                   primary_status, expiration_date, business_type,
                   CASE WHEN zip_code = :zip_code THEN 1
                        WHEN UPPER(city) = :city THEN 2
                        ELSE 3 END as proximity_score
            FROM contractors
            WHERE primary_status = 'CLEAR'
            AND ({class_conditions})
            AND (zip_code = :zip_code OR UPPER(city) LIKE :city_like)
            AND (expiration_date IS NULL OR expiration_date > NOW())
            ORDER BY proximity_score, expiration_date DESC
            LIMIT :limit
        """

        params = {
            "zip_code": zip_code or "",
            "city": (city or "").upper(),
            "city_like": f"%{(city or '').upper()}%",
            "limit": limit,
        }

        rows = (await db.execute(text(query), params)).fetchall()

        return {
            "lead_id": lead_id,
            "lead_name": f"{lead.first_name or ''} {lead.last_name or ''}".strip(),
            "project_type": lead.project_type,
            "postal_code": lead.postal_code,
            "city": lead.city,
            "classifications_searched": list(set(classes)),
            "match_count": len(rows),
            "ai_recommended_licenses": license_types,
            "contractors": [
                {
                    "license_no": r[0],
                    "business_name": r[1] or r[2],
                    "city": r[3],
                    "county": r[4],
                    "zip_code": r[5],
                    "phone": r[6],
                    "classifications": r[7],
                    "status": r[8],
                    "expiration_date": r[9].isoformat() if r[9] else None,
                    "proximity": "Same ZIP" if r[11] == 1 else "Same City" if r[11] == 2 else "Nearby",
                }
                for r in rows
            ]
        }


@router.get("/stats")
async def contractor_stats(identity: dict = Depends(get_current_user)):
    """Get contractor database statistics."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        total = (await db.execute(text("SELECT COUNT(*) FROM contractors"))).scalar()
        active = (await db.execute(text("SELECT COUNT(*) FROM contractors WHERE primary_status = 'CLEAR'"))).scalar()
        by_county = (await db.execute(text("""
            SELECT county, COUNT(*) as cnt
            FROM contractors WHERE primary_status = 'CLEAR' AND county IS NOT NULL
            GROUP BY county ORDER BY cnt DESC LIMIT 10
        """))).fetchall()

        return {
            "total_contractors": total,
            "active_contractors": active,
            "top_counties": [{"county": r[0], "count": r[1]} for r in by_county]
        }
