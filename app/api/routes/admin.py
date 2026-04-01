from fastapi import APIRouter, Depends
from app.core.deps import require_role_and_tenant
from app.models.user import UserRole
from app.models.tenant import TenantType

router = APIRouter(prefix="/admin", tags=["admin"])

@router.get("/metrics")
def get_admin_metrics(
    ctx = Depends(
        require_role_and_tenant(
            allowed_roles=[UserRole.admin],
            allowed_tenant_types=[TenantType.admin],
        )
    )
):
    user = ctx["user"]
    tenant = ctx["tenant"]
    return {
        "status": "ok",
        "user_id": str(user.id),
        "tenant": tenant.name,
        "message": "Admin metrics endpoint secured"
    }

# For a future Lead/Member portal:
@router.get("/me")
def get_lead_profile(
    ctx = Depends(
        require_role_and_tenant(
            allowed_roles=[UserRole.lead],
            allowed_tenant_types=[TenantType.lead],
        )
    )
):
    ...

