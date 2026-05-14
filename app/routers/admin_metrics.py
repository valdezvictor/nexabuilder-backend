# app/routers/admin_metrics.py
# Dashboard metrics using real DB data

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, text
from datetime import datetime, timezone, timedelta
from app.db import get_sessionmaker
from app.core.auth import get_current_user

router = APIRouter(prefix="/admin/metrics", tags=["Admin Metrics"])
dashboard_router = APIRouter(prefix="/admin", tags=["Admin Dashboard"])


@router.get("/leads/today")
async def leads_today(user: dict = Depends(get_current_user)):
    """Total leads ingested today."""
    try:
        from app.models.lead import Lead
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            result = await db.execute(
                select(func.count()).select_from(Lead).where(Lead.created_at >= today_start)
            )
            total = result.scalar() or 0
        return {"total": total}
    except Exception as e:
        print(f"[METRICS] leads_today error: {e}")
        return {"total": 0}


@router.get("/leads/summary")
async def leads_summary(user: dict = Depends(get_current_user)):
    """Full lead summary with status breakdown."""
    try:
        from app.models.lead import Lead
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            week_start = datetime.now(timezone.utc) - timedelta(days=7)

            total = (await db.execute(select(func.count()).select_from(Lead))).scalar() or 0
            today = (await db.execute(
                select(func.count()).select_from(Lead).where(Lead.created_at >= today_start)
            )).scalar() or 0
            this_week = (await db.execute(
                select(func.count()).select_from(Lead).where(Lead.created_at >= week_start)
            )).scalar() or 0
            ai_assessed = (await db.execute(
                select(func.count()).select_from(Lead).where(
                    Lead.ai_assessment.isnot(None)
                )
            )).scalar() or 0
            internal_routed = (await db.execute(
                select(func.count()).select_from(Lead).where(
                    Lead.lead_status == 'matched'
                )
            )).scalar() or 0

        return {
            "total": total,
            "today": today,
            "this_week": this_week,
            "ai_assessed": ai_assessed,
            "internal_routed": internal_routed,
        }
    except Exception as e:
        print(f"[METRICS] leads_summary error: {e}")
        return {"total": 0, "today": 0, "this_week": 0, "ai_assessed": 0, "internal_routed": 0}


@router.get("/routing/success-rate")
async def routing_success_rate(user: dict = Depends(get_current_user)):
    """Routing success rate based on leads with AI assessment."""
    try:
        from app.models.lead import Lead
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            total = (await db.execute(select(func.count()).select_from(Lead))).scalar() or 0
            assessed = (await db.execute(
                select(func.count()).select_from(Lead).where(Lead.ai_assessment.isnot(None))
            )).scalar() or 0
            rate = round((assessed / total) * 100, 1) if total > 0 else 0.0
        return {"rate": rate, "total": total, "assessed": assessed}
    except Exception as e:
        print(f"[METRICS] routing error: {e}")
        return {"rate": 0.0, "total": 0}


@router.get("/partners/active")
async def active_partners(user: dict = Depends(get_current_user)):
    """Count of active service providers + partners."""
    try:
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            result = await db.execute(text(
                "SELECT COUNT(*) FROM service_providers WHERE status = 'active'"
            ))
            providers = result.scalar() or 0
        return {"count": providers}
    except Exception as e:
        print(f"[METRICS] partners error: {e}")
        return {"count": 0}


@router.get("/contractors/stats")
async def contractor_stats(user: dict = Depends(get_current_user)):
    """CSLB contractor database stats."""
    try:
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            total = (await db.execute(text("SELECT COUNT(*) FROM contractors"))).scalar() or 0
            active = (await db.execute(
                text("SELECT COUNT(*) FROM contractors WHERE primary_status='CLEAR'")
            )).scalar() or 0
            enriched = (await db.execute(
                text("SELECT COUNT(*) FROM contractors WHERE email IS NOT NULL")
            )).scalar() or 0
        return {"total": total, "active": active, "enriched": enriched}
    except Exception as e:
        print(f"[METRICS] contractor_stats error: {e}")
        return {"total": 0, "active": 0, "enriched": 0}


@router.get("/system/health")
async def system_health(user: dict = Depends(get_current_user)):
    """System health check."""
    try:
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return {
            "status": "healthy",
            "database": "connected",
            "api": "running",
            "checked_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        return {"status": "degraded", "error": str(e)}
