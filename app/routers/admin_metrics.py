# app/routers/admin_metrics.py
# Dashboard metrics endpoints for Admin Console
# Called by frontend/admin-console/src/api/adminMetrics.ts + dashboard.ts

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, text
from datetime import datetime, timezone, timedelta

from app.db import get_sessionmaker
from app.core.auth import get_current_user

router = APIRouter(prefix="/admin/metrics", tags=["Admin Metrics"])


async def get_db():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        yield db


# ── GET /admin/metrics/leads/today ──────────────────────────────────────────
@router.get("/leads/today")
async def leads_today(user: dict = Depends(get_current_user)):
    """Total leads ingested today."""
    try:
        from app.models.lead import Lead
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            result = await db.execute(
                select(func.count()).select_from(Lead).where(
                    Lead.created_at >= today_start
                )
            )
            total = result.scalar() or 0
        return {"total": total}
    except Exception:
        return {"total": 0}


# ── GET /admin/metrics/routing/success-rate ──────────────────────────────────
@router.get("/routing/success-rate")
async def routing_success_rate(user: dict = Depends(get_current_user)):
    """Routing success rate over the last 24 hours (percentage)."""
    try:
        from app.models.routing_event import RoutingEvent
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            total_result = await db.execute(
                select(func.count()).select_from(RoutingEvent).where(
                    RoutingEvent.created_at >= since
                )
            )
            total = total_result.scalar() or 0

            if total == 0:
                return {"rate": 0.0, "total": 0}

            success_result = await db.execute(
                select(func.count()).select_from(RoutingEvent).where(
                    RoutingEvent.created_at >= since,
                    RoutingEvent.status == "success"
                )
            )
            success = success_result.scalar() or 0
            rate = round((success / total) * 100, 1)
        return {"rate": rate, "total": total, "success": success}
    except Exception:
        return {"rate": 0.0, "total": 0}


# ── GET /admin/metrics/partners/active ───────────────────────────────────────
@router.get("/partners/active")
async def active_partners(user: dict = Depends(get_current_user)):
    """Count of active partner tenants."""
    try:
        from app.models.tenant import Tenant, TenantType
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            result = await db.execute(
                select(func.count()).select_from(Tenant).where(
                    Tenant.type == TenantType.partner,
                    Tenant.is_active == True
                )
            )
            count = result.scalar() or 0
        return {"count": count}
    except Exception:
        return {"count": 0}


# ── GET /admin/metrics/system-health ─────────────────────────────────────────
@router.get("/system-health")
async def system_health(user: dict = Depends(get_current_user)):
    """Overall system health — DB + API status."""
    try:
        from app.db import test_connection
        db_ok = await test_connection() == 1
        status = "healthy" if db_ok else "degraded"
        return {"status": status, "db": "ok" if db_ok else "error"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ── GET /dashboard/stats ─────────────────────────────────────────────────────
# Called by frontend/admin-console/src/api/dashboard.ts → fetchDashboardStats()
from fastapi import APIRouter as _APIRouter
dashboard_router = _APIRouter(prefix="/dashboard", tags=["Dashboard"])


@dashboard_router.get("/stats")
async def dashboard_stats(user: dict = Depends(get_current_user)):
    """Combined dashboard stats — single call for the dashboard home page."""
    try:
        from app.models.lead import Lead
        from app.models.tenant import Tenant, TenantType
        from app.db import test_connection

        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            leads_result = await db.execute(
                select(func.count()).select_from(Lead).where(
                    Lead.created_at >= today_start
                )
            )
            total_leads = leads_result.scalar() or 0

            partners_result = await db.execute(
                select(func.count()).select_from(Tenant).where(
                    Tenant.type == TenantType.partner,
                    Tenant.is_active == True
                )
            )
            active_partners = partners_result.scalar() or 0

        return {
            "total_leads":      total_leads,
            "routing_success":  "N/A",
            "active_partners":  active_partners,
            "system_status":    "healthy",
        }
    except Exception as e:
        return {
            "total_leads":     0,
            "routing_success": "N/A",
            "active_partners": 0,
            "system_status":   "error",
        }
