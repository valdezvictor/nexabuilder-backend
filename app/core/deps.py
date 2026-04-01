# app/core/deps.py
from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.tenant import Tenant, TenantType
from app.models.user import User, UserRole
from app.core.security import jwt, ALGORITHM
from app.core.config import settings
from app.models.user import UserStatus
from jose import JWTError

def get_current_user(
    authorization: str = Header(None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db.query(User).get(user_id)
    if not user or user.status != UserStatus.active:
        raise HTTPException(status_code=401, detail="Inactive user")
    return user

def require_roles(*roles: UserRole):
    def wrapper(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Forbidden")
        return user
    return wrapper

def get_tenant_from_host(
    host: str = Header(..., alias="host"),
    db: Session = Depends(get_db),
) -> Tenant:
    # Strip port if present (e.g. "admin.nexabuilder.com:443")
    hostname = host.split(":")[0].lower()

    tenant = db.query(Tenant).filter(Tenant.domain == hostname).first()
    if not tenant:
        raise HTTPException(status_code=400, detail="Unknown tenant/domain")
    return tenant

def require_tenant_type(*allowed_types: TenantType):
    def wrapper(
        tenant: Tenant = Depends(get_tenant_from_host),
    ) -> Tenant:
        if tenant.type not in allowed_types:
            raise HTTPException(status_code=403, detail="Tenant type not allowed")
        return tenant
    return wrapper

def require_role_and_tenant(
    allowed_roles: list[UserRole],
    allowed_tenant_types: list[TenantType],
):
    def wrapper(
        user: User = Depends(get_current_user),
        tenant: Tenant = Depends(get_tenant_from_host),
    ):
        if user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Forbidden (role)")
        if tenant.type not in allowed_tenant_types:
            raise HTTPException(status_code=403, detail="Forbidden (tenant)")
        return {"user": user, "tenant": tenant}
    return wrapper
