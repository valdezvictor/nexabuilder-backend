# app/routers/api/partner_routing.py
# DB-driven partner contractor vertical match router
# Partners are managed via admin dashboard — no code changes needed
# to add/remove/pause any partner

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from typing import Optional
from app.db import get_sessionmaker
from app.core.auth import get_current_user

router = APIRouter(prefix="/api/partners", tags=["Partner Routing"])


# ─── Vertical keyword mapping ─────────────────────────────────────────────────
VERTICAL_KEYWORDS = {
    "windows":      ["window","windows","ventana","ventanas","replacement window"],
    "siding":       ["siding","revestimiento","facade","fachada","hardie"],
    "insulation":   ["insulation","insulacion","attic","atico","energy","energia"],
    "exterior":     ["exterior","outside","afuera","facade"],
    "water_damage": ["water damage","water leak","flood","flooding","inundacion",
                     "daño de agua","agua","leak","plumbing emergency"],
    "mold":         ["mold","moho","mildew","fungus","hongos","black mold"],
    "fire_damage":  ["fire damage","fire","incendio","smoke damage","fuego"],
    "smoke_damage": ["smoke","humo","soot","carbon"],
    "emergency":    ["emergency","emergencia","urgent","urgente","asap","inmediato",
                     "burst pipe","flooding","fire"],
    "restoration":  ["restoration","restauracion","cleanup","limpieza","remediation"],
    "roofing":      ["roof","roofing","techo","shingle","tile roof","re-roof"],
}


def match_verticals(project_type: str, description: str, vertical: str) -> list[str]:
    """Extract matched vertical keywords from project details."""
    combined = " ".join([
        project_type or "", description or "", vertical or ""
    ]).lower()

    matched = []
    for v, keywords in VERTICAL_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            matched.append(v)
    return matched


async def find_matching_partners(
    lead_score: int,
    project_type: str,
    description: str,
    vertical: str,
    postal_code: str,
    db,
) -> list[dict]:
    """
    Find active partners that match this lead's verticals and score.
    Ordered by priority (highest first).
    Partners can be toggled on/off from admin without code changes.
    """
    matched_verticals = match_verticals(project_type, description, vertical)
    if not matched_verticals:
        return []

    zip_prefix = (postal_code or "")[:2]

    result = await db.execute(text("""
        SELECT
            id, name, slug, email, phone, website,
            verticals, min_score, commission_pct, flat_fee,
            payment_model, accepts_phone_transfer,
            accepts_ping_post, priority, notes,
            api_endpoint, api_key_ssm_path
        FROM partner_contractors
        WHERE is_active = TRUE
          AND min_score <= :score
          AND (
              zip_prefixes IS NULL
              OR zip_prefixes = '{}'
              OR :zip_prefix = ANY(zip_prefixes)
          )
        ORDER BY priority DESC
    """), {"score": lead_score, "zip_prefix": zip_prefix})

    partners = result.fetchall()
    matches = []

    for p in partners:
        partner_verticals = p[6] or []
        # Check if any partner vertical matches our lead's verticals
        overlap = set(partner_verticals) & set(matched_verticals)
        if overlap:
            matches.append({
                "id": str(p[0]),
                "name": p[1],
                "slug": p[2],
                "email": p[3],
                "phone": p[4],
                "website": p[5],
                "matched_verticals": list(overlap),
                "min_score": p[7],
                "commission_pct": p[8],
                "flat_fee": p[9],
                "payment_model": p[10],
                "accepts_phone_transfer": p[11],
                "accepts_ping_post": p[12],
                "priority": p[13],
                "api_endpoint": p[15],
                "has_api": bool(p[15]),
            })

    return matches


# ─── API Endpoints ────────────────────────────────────────────────────────────

@router.get("")
async def list_partners(
    active_only: bool = True,
    identity: dict = Depends(get_current_user)
):
    """List all partner contractors. Toggle active_only=false to see all."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        query = """
            SELECT id, name, slug, email, phone, website,
                   verticals, min_score, commission_pct, payment_model,
                   accepts_phone_transfer, accepts_ping_post,
                   is_active, priority, notes, created_at
            FROM partner_contractors
        """
        if active_only:
            query += " WHERE is_active = TRUE"
        query += " ORDER BY priority DESC"

        result = await db.execute(text(query))
        rows = result.fetchall()

    return [{
        "id": str(r[0]),
        "name": r[1],
        "slug": r[2],
        "email": r[3],
        "phone": r[4],
        "website": r[5],
        "verticals": r[6],
        "min_score": r[7],
        "commission_pct": r[8],
        "payment_model": r[9],
        "accepts_phone_transfer": r[10],
        "accepts_ping_post": r[11],
        "is_active": r[12],
        "priority": r[13],
        "notes": r[14],
        "created_at": r[15].isoformat() if r[15] else None,
    } for r in rows]


@router.post("")
async def create_partner(
    name: str,
    slug: str,
    verticals: list[str],
    min_score: int = 5,
    commission_pct: float = 10.0,
    payment_model: str = "commission",
    email: Optional[str] = None,
    phone: Optional[str] = None,
    website: Optional[str] = None,
    zip_prefixes: Optional[list[str]] = None,
    accepts_phone_transfer: bool = False,
    accepts_ping_post: bool = True,
    priority: int = 50,
    notes: Optional[str] = None,
    identity: dict = Depends(get_current_user),
):
    """Add a new partner contractor. No code changes needed."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await db.execute(text("""
            INSERT INTO partner_contractors (
                name, slug, email, phone, website,
                verticals, zip_prefixes, min_score,
                commission_pct, payment_model,
                accepts_phone_transfer, accepts_ping_post,
                priority, notes, is_active
            ) VALUES (
                :name, :slug, :email, :phone, :website,
                :verticals, :zip_prefixes, :min_score,
                :commission_pct, :payment_model,
                :accepts_phone_transfer, :accepts_ping_post,
                :priority, :notes, TRUE
            )
        """), {
            "name": name, "slug": slug, "email": email,
            "phone": phone, "website": website,
            "verticals": verticals,
            "zip_prefixes": zip_prefixes or ["90","91","92"],
            "min_score": min_score,
            "commission_pct": commission_pct,
            "payment_model": payment_model,
            "accepts_phone_transfer": accepts_phone_transfer,
            "accepts_ping_post": accepts_ping_post,
            "priority": priority,
            "notes": notes,
        })
        await db.commit()
    return {"status": "created", "partner": name}


@router.patch("/{slug}/toggle")
async def toggle_partner(
    slug: str,
    is_active: bool,
    identity: dict = Depends(get_current_user),
):
    """
    Activate or deactivate a partner instantly.
    is_active=false pauses all lead routing to this partner.
    No code changes, no deploy needed.
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(text("""
            UPDATE partner_contractors
            SET is_active = :is_active, updated_at = NOW()
            WHERE slug = :slug
            RETURNING name, is_active
        """), {"slug": slug, "is_active": is_active})
        row = result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Partner not found")
        await db.commit()

    status = "ACTIVATED" if is_active else "PAUSED"
    return {
        "partner": row[0],
        "status": status,
        "is_active": row[1],
        "message": f"{row[0]} has been {status}. Lead routing updated immediately."
    }


@router.patch("/{slug}")
async def update_partner(
    slug: str,
    is_active: Optional[bool] = None,
    min_score: Optional[int] = None,
    commission_pct: Optional[float] = None,
    priority: Optional[int] = None,
    verticals: Optional[list[str]] = None,
    notes: Optional[str] = None,
    identity: dict = Depends(get_current_user),
):
    """Update any partner field without touching code."""
    updates = []
    params = {"slug": slug}
    if is_active is not None:
        updates.append("is_active = :is_active")
        params["is_active"] = is_active
    if min_score is not None:
        updates.append("min_score = :min_score")
        params["min_score"] = min_score
    if commission_pct is not None:
        updates.append("commission_pct = :commission_pct")
        params["commission_pct"] = commission_pct
    if priority is not None:
        updates.append("priority = :priority")
        params["priority"] = priority
    if verticals is not None:
        updates.append("verticals = :verticals")
        params["verticals"] = verticals
    if notes is not None:
        updates.append("notes = :notes")
        params["notes"] = notes

    if not updates:
        return {"message": "No changes provided"}

    updates.append("updated_at = NOW()")
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await db.execute(text(
            f"UPDATE partner_contractors SET {', '.join(updates)} WHERE slug = :slug"
        ), params)
        await db.commit()
    return {"status": "updated", "slug": slug}


@router.post("/match")
async def match_lead_to_partners(
    lead_id: int,
    identity: dict = Depends(get_current_user),
):
    """
    Match a specific lead to available partners.
    Returns ordered list of matching partners with routing decision.
    """
    from app.models.lead import Lead
    from sqlalchemy import select

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        ai = lead.ai_assessment or {}
        score = ai.get("composite_score") or ai.get("complexity_score", 5)

        matches = await find_matching_partners(
            lead_score=score,
            project_type=lead.project_type or "",
            description=lead.project_description or "",
            vertical=lead.vertical or "",
            postal_code=lead.postal_code or "",
            db=db,
        )

    return {
        "lead_id": lead_id,
        "lead_score": score,
        "vertical": lead.vertical,
        "project_type": lead.project_type,
        "partners_found": len(matches),
        "partners": matches,
        "routing": "partner" if matches else "network",
        "message": f"Found {len(matches)} partner(s) for this lead" if matches
                   else "No partner match — route to lead network",
    }
