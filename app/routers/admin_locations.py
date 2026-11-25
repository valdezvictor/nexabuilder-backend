"""
Admin Locations Router — /admin/counties + /admin/leads
Registered with prefix="/api" → serves:
  GET /api/admin/counties
  GET /api/admin/leads/   (admin console dashboard)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db

router = APIRouter(prefix="/admin", tags=["admin-locations"])


@router.get("/counties")
async def list_counties(db: AsyncSession = Depends(get_db)):
    result = await db.execute(text(
        "SELECT id, slug, name, h1_title, meta_description, city_count, updated_at "
        "FROM nb_counties ORDER BY name"
    ))
    rows = result.mappings().all()
    return [
        {
            "id": r["id"],
            "slug": r["slug"] or "",
            "county_slug": r["slug"] or "",
            "name": r["name"] or "",
            "h1_title": r["h1_title"] or "",
            "meta_description": r["meta_description"] or "",
            "city_count": r["city_count"] or 0,
            "stub_count": 0,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows
    ]


@router.get("/leads/")
async def admin_leads_list(
    limit: int = Query(default=50, le=200),
    ordering: str = Query(default="-created_at"),
    db: AsyncSession = Depends(get_db),
):
    """
    Admin lead list for the dashboard — served at /api/admin/leads/
    The existing leads router has a double-prefix bug (/api/api/leads);
    this endpoint provides the correct path the admin console calls.
    """
    # Safe ordering: only allow known columns
    order_col = "created_at DESC" if ordering.startswith("-") else "created_at ASC"
    result = await db.execute(text(
        "SELECT id, first_name, last_name, email, phone, vertical, "
        "postal_code, city, state, ai_score, routing_tier, lead_status, "
        "source, created_at "
        "FROM leads ORDER BY " + order_col + " LIMIT :limit"
    ), {"limit": limit})
    rows = result.mappings().all()
    return {
        "count": len(rows),
        "results": [
            {
                "id": r["id"],
                "first_name": r["first_name"],
                "last_name": r["last_name"],
                "email": r["email"],
                "phone": r["phone"],
                "vertical": r["vertical"],
                "postal_code": r["postal_code"],
                "city": r["city"],
                "state": r["state"],
                "ai_score": r["ai_score"],
                "routing_tier": r["routing_tier"],
                "lead_status": r["lead_status"] or "submitted",
                "source": r["source"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    }


@router.get("/leads/", tags=["admin-locations"])
async def leads_list_alias(
    limit: int = Query(default=50, le=200),
    ordering: str = Query(default="-created_at"),
    db: AsyncSession = Depends(get_db),
):
    """
    Alias at /api/leads/ — admin console dashboard widget calls this path.
    The main leads router has a double-prefix bug (/api/api/leads).
    """
    order_col = "created_at DESC" if ordering.startswith("-") else "created_at ASC"
    result = await db.execute(text(
        "SELECT id, first_name, last_name, email, phone, vertical, "
        "postal_code, city, state, ai_score, routing_tier, lead_status, "
        "source, created_at "
        "FROM leads ORDER BY " + order_col + " LIMIT :limit"
    ), {"limit": limit})
    rows = result.mappings().all()
    return {
        "count": len(rows),
        "results": [
            {
                "id": r["id"],
                "first_name": r["first_name"] or "",
                "last_name": r["last_name"] or "",
                "email": r["email"] or "",
                "phone": r["phone"] or "",
                "vertical": r["vertical"] or "",
                "postal_code": r["postal_code"] or "",
                "city": r["city"] or "",
                "state": r["state"] or "",
                "ai_score": r["ai_score"],
                "routing_tier": r["routing_tier"] or "",
                "lead_status": r["lead_status"] or "",
                "source": r["source"] or "",
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }
