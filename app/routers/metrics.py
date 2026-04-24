from fastapi import APIRouter

router = APIRouter(tags=["metrics"])

@router.get("/admin/stats")
def admin_stats():
    return {
        "leads_today": 124,
        "routing_success_rate": 98.5,
        "active_partners": 12,
        "system_health": "Healthy"
    }
