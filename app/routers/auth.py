from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from app.schemas.auth import LoginRequest, LoginResponse
from app.core.security import verify_password, create_access_token, create_refresh_token
from app.core.tenant import get_tenant
from app.core.auth import get_current_user
from app.db import get_sessionmaker
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, tenant=Depends(get_tenant)):
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Find user by email AND tenant
        stmt = (
            select(User)
            .where(User.email == payload.email)
        )
        result = await db.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        # Update last login
        user.last_login_at = func.now()
        await db.commit()

        # Create tokens
        access = create_access_token({
            "sub": str(user.id),
            "tenant": str(tenant.id),
            "role": user.role.value
        })

        refresh = create_refresh_token({
            "sub": str(user.id),
            "tenant": str(tenant.id)
        })

        return LoginResponse(
            access_token=access,
            refresh_token=refresh
        )

@router.get("/me")
async def auth_me(identity = Depends(get_current_user)):
    user = identity["user"]
    tenant = identity["tenant"]

    return {
        "id": str(user.id),
        "email": user.email,
        "role": user.role.value,
        "tenant": {
            "id": str(tenant.id),
            "name": tenant.name,
            "domain": tenant.domain,
            "type": tenant.type.value
        }
    }
