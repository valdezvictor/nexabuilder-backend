from fastapi import Depends, HTTPException, Header
from jose import jwt, JWTError
from app.core.config import settings
from app.db import get_sessionmaker
from app.models.user import User
from app.models.tenant import Tenant

async def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.split(" ")[1]

    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = payload.get("sub")
    tenant_id = payload.get("tenant")

    if not user_id or not tenant_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Load user
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Load tenant
        tenant = await db.get(Tenant, tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")

        return {
            "user": user,
            "tenant": tenant,
            "role": user.role.value
        }
