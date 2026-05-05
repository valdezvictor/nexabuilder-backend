# app/routers/auth.py
# Fixed: login resolves tenant via user_tenants junction table
# No longer depends on Host header — works from any origin

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func

from app.schemas.auth import LoginRequest, LoginResponse
from app.core.security import verify_password, create_access_token, create_refresh_token
from app.core.auth import get_current_user
from app.db import get_sessionmaker
from app.models.user import User
from app.models.tenant import Tenant
from app.models.user_tenant import UserTenant

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest):
    """
    Authenticate with email + password.
    Tenant is resolved via user_tenants table — not from Host header.
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:

        # Find user by email
        result = await db.execute(
            select(User).where(User.email == payload.email)
        )
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        # Find tenant via user_tenants junction table
        ut_result = await db.execute(
            select(UserTenant).where(UserTenant.user_id == user.id)
        )
        user_tenant = ut_result.scalar_one_or_none()

        if not user_tenant:
            raise HTTPException(status_code=401, detail="No tenant assigned")

        tenant_result = await db.execute(
            select(Tenant).where(Tenant.id == user_tenant.tenant_id)
        )
        tenant = tenant_result.scalar_one_or_none()

        if not tenant:
            raise HTTPException(status_code=401, detail="Tenant not found")

        # Update last login
        user.last_login_at = func.now()
        await db.commit()

        # Create tokens
        token_data = {
            "sub":    str(user.id),
            "tenant": str(tenant.id),
            "role":   user.role.value
        }
        return LoginResponse(
            access_token=create_access_token(token_data),
            refresh_token=create_refresh_token(token_data)
        )


@router.get("/me")
async def auth_me(identity=Depends(get_current_user)):
    """Return the current authenticated user's full identity."""
    user   = identity["user"]
    tenant = identity["tenant"]
    return {
        "id":    str(user.id),
        "email": user.email,
        "role":  user.role.value,
        "tenant": {
            "id":     str(tenant.id),
            "name":   tenant.name,
            "domain": tenant.domain,
            "type":   tenant.type.value
        }
    }
