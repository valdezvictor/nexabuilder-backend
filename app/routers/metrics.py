from fastapi import APIRouter

router = APIRouter(prefix="/api/metrics", tags=["metrics"])

@router.get("/leads/today")
async def get_leads_today():
    return {"count": 0}

@router.get("/routing/success")
async def get_routing_success():
    return {"success_rate": 0.0}

@router.get("/partners/active")
async def get_active_partners():
    return {"active_partners": 0}

@router.get("/system/health")
async def get_system_health():
    return {"status": "healthy"}
