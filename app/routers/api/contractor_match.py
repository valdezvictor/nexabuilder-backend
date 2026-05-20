# app/routers/api/contractor_match.py
# CSLB contractor matching engine — v2
# Priority matching: primary classification first, expand by county, score by proximity

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text, select, update
from typing import Optional
from app.core.auth import get_current_user
from app.db import get_sessionmaker
from app.models.lead import Lead

router = APIRouter(prefix="/api/contractors", tags=["Contractor Matching"])

# Primary classification for each vertical — most important license type first
PRIMARY_CLASSIFICATION = {
    "pool":             "C-53",
    "pool_installation": "C-53",
    "roofing":          "C-39",
    "electrical":       "C-10",
    "plumbing":         "C-36",
    "hvac":             "C-20",
    "framing":          "C-5",
    "concrete":         "C-8",
    "landscaping":      "C-27",
    "painting":         "C-33",
    "solar":            "C-46",
    "general":          "B",
    "new_construction": "A",
    "remodel":          "B",
    "addition":         "B",
    "home_services":    "B",
    "retaining_wall":   "C-29",
    "masonry":          "C-29",
    "drywall":          "C-9",
    "flooring":         "C-15",
    "fencing":          "C-13",
    "insulation":       "C-2",
    "bathroom_remodel": "B",
    "home_remodeling":  "B",
}

# All valid classifications per vertical (for fallback search)
ALL_CLASSIFICATIONS = {
    "pool":             ["C-53", "C53"],
    "roofing":          ["C-39", "C39"],
    "electrical":       ["C-10", "C10"],
    "plumbing":         ["C-36", "C36"],
    "hvac":             ["C-20", "C20"],
    "framing":          ["C-5", "C5"],
    "concrete":         ["C-8", "C8"],
    "landscaping":      ["C-27", "C27"],
    "painting":         ["C-33", "C33"],
    "solar":            ["C-46", "C46"],
    "general":          ["B", "A"],
    "new_construction": ["A", "B"],
    "remodel":          ["B"],
    "addition":         ["B", "A"],
    "home_services":    ["B", "C-36", "C-10", "C-39"],
    "retaining_wall":   ["C-29", "C29"],
    "masonry":          ["C-29", "C29"],
    "drywall":          ["C-9", "C9"],
    "flooring":         ["C-15", "C15"],
    "fencing":          ["C-13", "C13"],
    "insulation":       ["C-2", "C2"],
    "bathroom_remodel": ["B"],
    "home_remodeling":  ["B"],
}


def _get_classifications(vertical: str, project_type: str, ai_licenses: list) -> tuple[str, list]:
    """
    Returns (primary_class, all_classes) for a given vertical/project_type.
    primary_class drives the first-pass search.
    all_classes is used for the fallback broader search.
    """
    key = (vertical or "").lower().replace(" ", "_")
    pt_key = (project_type or "").lower().replace(" ", "_").replace(" ", "_")

    primary = (
        PRIMARY_CLASSIFICATION.get(pt_key)
        or PRIMARY_CLASSIFICATION.get(key)
        or "B"
    )
    classes = list(set(
        ALL_CLASSIFICATIONS.get(pt_key, [])
        + ALL_CLASSIFICATIONS.get(key, [])
    ))
    if not classes:
        classes = [primary]

    return primary, classes


async def _match_query(db, primary_class: str, all_classes: list,
                       zip_code: str, county: str, city: str, limit: int) -> list:
    """
    Two-pass matching:
    Pass 1: Exact primary classification, same county
    Pass 2: Any classification in the vertical, same county
    Results ranked: same ZIP > same city > rest of county
    """

    class_like_primary = f"%{primary_class}%"
    all_like = " OR ".join([f"classifications ILIKE '%{c}%'" for c in all_classes[:8]])

    query = f"""
        WITH ranked AS (
            SELECT
                license_no, business_name, full_business_name,
                city, county, zip_code, phone, classifications,
                primary_status, expiration_date, business_type,
                CASE
                    WHEN classifications ILIKE :primary_like THEN 1
                    ELSE 2
                END as class_rank,
                CASE
                    WHEN zip_code = :zip THEN 1
                    WHEN UPPER(city) = UPPER(:city) THEN 2
                    ELSE 3
                END as proximity_rank
            FROM contractors
            WHERE primary_status = 'CLEAR'
            AND (expiration_date IS NULL OR expiration_date > NOW())
            AND ({all_like})
            AND UPPER(county) ILIKE :county_like
        )
        SELECT * FROM ranked
        ORDER BY class_rank, proximity_rank, license_no DESC
        LIMIT :limit
    """

    params = {
        "primary_like": class_like_primary,
        "zip": zip_code or "",
        "city": city or "",
        "county_like": f"%{county}%" if county else "%",
        "limit": limit,
    }

    rows = (await db.execute(text(query), params)).fetchall()
    return rows


@router.get("/match/{lead_id}")
async def match_contractors_for_lead(
    lead_id: int,
    limit: int = Query(10, le=50),
    identity: dict = Depends(get_current_user),
):
    """
    Match CSLB contractors to a lead using primary classification first,
    expanding by county. Results ranked by classification match + proximity.
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Lead not found")

        vertical = (lead.vertical or "").lower()
        project_type = (lead.project_type or "").lower()
        ai = lead.ai_assessment or {}
        ai_licenses = ai.get("license_types_needed", [])

        primary_class, all_classes = _get_classifications(vertical, project_type, ai_licenses)

        zip_code = lead.postal_code or ""
        city = lead.city or ""

        # Resolve county from ZIP if city/county missing
        county = ""
        if zip_code:
            r_county = await db.execute(text(
                "SELECT county FROM contractors WHERE zip_code = :z AND county IS NOT NULL LIMIT 1"
            ), {"z": zip_code})
            row = r_county.fetchone()
            if row:
                county = row[0]

        rows = await _match_query(db, primary_class, all_classes, zip_code, county, city, limit)

        contractors = []
        for r in rows:
            proximity = "Same ZIP" if r[10] == 1 else "Same City" if r[10] == 2 else "Same County"
            primary_match = r[9] == 1
            contractors.append({
                "license_number": r[0],
                "contractor_name": r[1] or r[2],
                "city": r[3],
                "county": r[4],
                "zip_code": r[5],
                "phone": r[6],
                "classifications": r[7],
                "primary_status": r[8],
                "expiration_date": r[9].isoformat() if r[9] else None,
                "business_type": r[10],
                "proximity": proximity,
                "primary_classification_match": primary_match,
                "score": round(
                    (1.0 if primary_match else 0.6) *
                    (1.0 if r[10] == 1 else 0.85 if r[10] == 2 else 0.7),
                    2
                ),
                "match_reason": f"{'Primary: ' + primary_class if primary_match else 'Alt classification'} · {proximity}",
            })

        # Demo mode: VIP leads see full contractor list with rankings
        demo_flags = getattr(lead, 'demo_flags', {}) or {}
        is_demo    = demo_flags.get('show_all_contractors', False)
        demo_user  = demo_flags.get('demo_user', None)

        return {
            "lead_id":    lead_id,
            "lead_name":  f"{lead.first_name or ''} {lead.last_name or ''}".strip(),
            "vertical":   lead.vertical,
            "project_type": lead.project_type,
            "postal_code": zip_code,
            "city":        city,
            "county":      county,
            "primary_classification": primary_class,
            "classifications_searched": all_classes,
            "ai_recommended_licenses": ai_licenses,
            "match_count": len(contractors),
            "matches":     contractors,
            "contractors": contractors,           # backward compat
            "is_demo":     is_demo,
            "demo_user":   demo_user,
            # For demo leads: show vetting status and ranking explanation
            "vetting_note": (
                f"Showing {len(contractors)} CSLB-verified contractors ranked by "
                f"license match and proximity. NexaBuilder will send bid invitations "
                f"to the top matches and present the accepted contractor to the homeowner."
            ) if is_demo else None,
        }


@router.post("/assign/{lead_id}")
async def assign_contractor_to_lead(
    lead_id: int,
    license_number: str,
    identity: dict = Depends(get_current_user),
):
    """
    Assign a specific contractor to a lead.
    Updates lead.contractor_id and lead.lead_status = 'matched'.
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Find contractor by license
        r = await db.execute(text(
            "SELECT id, license_no, business_name, city, classifications "
            "FROM contractors WHERE license_no = :lic LIMIT 1"
        ), {"lic": license_number})
        contractor = r.fetchone()
        if not contractor:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"Contractor {license_number} not found")

        # Update lead
        await db.execute(text(
            "UPDATE leads SET contractor_id = :cid, lead_status = 'matched' "
            "WHERE id = :lid"
        ), {"cid": contractor[0], "lid": lead_id})
        await db.commit()

        return {
            "success": True,
            "lead_id": lead_id,
            "lead_status": "matched",
            "assigned_contractor": {
                "id": contractor[0],
                "license_number": contractor[1],
                "name": contractor[2],
                "city": contractor[3],
                "classifications": contractor[4],
            }
        }


@router.post("/auto-assign/{lead_id}")
async def auto_assign_best_contractor(
    lead_id: int,
    identity: dict = Depends(get_current_user),
):
    """
    Automatically assign the top-scored contractor to a lead.
    Called after intake when the lead score is high enough.
    """
    from fastapi import HTTPException

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        vertical = (lead.vertical or "").lower()
        project_type = (lead.project_type or "").lower()
        ai = lead.ai_assessment or {}
        ai_licenses = ai.get("license_types_needed", [])

        primary_class, all_classes = _get_classifications(vertical, project_type, ai_licenses)
        zip_code = lead.postal_code or ""
        city = lead.city or ""

        county = ""
        if zip_code:
            r_county = await db.execute(text(
                "SELECT county FROM contractors WHERE zip_code = :z AND county IS NOT NULL LIMIT 1"
            ), {"z": zip_code})
            row = r_county.fetchone()
            if row:
                county = row[0]

        rows = await _match_query(db, primary_class, all_classes, zip_code, county, city, 1)
        if not rows:
            raise HTTPException(status_code=404, detail="No matching contractors found in your area")

        best = rows[0]

        # Get contractor DB id
        r2 = await db.execute(text(
            "SELECT id FROM contractors WHERE license_no = :lic LIMIT 1"
        ), {"lic": best[0]})
        contractor_row = r2.fetchone()
        contractor_db_id = contractor_row[0] if contractor_row else None

        # Update lead
        await db.execute(text(
            "UPDATE leads SET contractor_id = :cid, lead_status = 'matched' WHERE id = :lid"
        ), {"cid": contractor_db_id, "lid": lead_id})
        await db.commit()

        return {
            "success": True,
            "lead_id": lead_id,
            "lead_status": "matched",
            "assigned_contractor": {
                "license_number": best[0],
                "name": best[1] or best[2],
                "city": best[3],
                "county": best[4],
                "zip_code": best[5],
                "phone": best[6],
                "classifications": best[7],
                "primary_classification": primary_class,
                "match_reason": f"Top match: {primary_class} · {'Same ZIP' if best[10] == 1 else 'Same County'}",
            }
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
        if project_type and project_type.lower().replace(" ", "_") in ALL_CLASSIFICATIONS:
            classes = ALL_CLASSIFICATIONS[project_type.lower().replace(" ", "_")]
            cc = " OR ".join([f"classifications ILIKE '%{c}%'" for c in classes])
            conditions.append(f"({cc})")
        if classification:
            conditions.append("classifications ILIKE :classification")
            params["classification"] = f"%{classification}%"

        where = " AND ".join(conditions)
        query = f"""
            SELECT license_no, business_name, full_business_name,
                   city, county, zip_code, phone, classifications,
                   primary_status, expiration_date, business_type
            FROM contractors WHERE {where}
            ORDER BY expiration_date DESC NULLS LAST
            LIMIT :limit
        """
        rows = (await db.execute(text(query), params)).fetchall()

        return {
            "count": len(rows),
            "filters": {"zip_code": zip_code, "city": city, "project_type": project_type},
            "contractors": [
                {
                    "license_number": r[0],
                    "contractor_name": r[1] or r[2],
                    "city": r[3],
                    "county": r[4],
                    "zip_code": r[5],
                    "phone": r[6],
                    "classifications": r[7],
                    "primary_status": r[8],
                    "expiration_date": r[9].isoformat() if r[9] else None,
                }
                for r in rows
            ]
        }


@router.get("/stats")
async def contractor_stats(identity: dict = Depends(get_current_user)):
    """Get contractor database statistics."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        total  = (await db.execute(text("SELECT COUNT(*) FROM contractors"))).scalar()
        active = (await db.execute(text("SELECT COUNT(*) FROM contractors WHERE primary_status = 'CLEAR'"))).scalar()
        by_county = (await db.execute(text("""
            SELECT county, COUNT(*) as cnt FROM contractors
            WHERE primary_status = 'CLEAR' AND county IS NOT NULL
            GROUP BY county ORDER BY cnt DESC LIMIT 10
        """))).fetchall()

        return {
            "total_contractors": total,
            "active_contractors": active,
            "coverage": "California (CSLB)",
            "top_counties": [{"county": r[0], "count": r[1]} for r in by_county]
        }
