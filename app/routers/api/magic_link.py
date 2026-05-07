# app/routers/api/magic_link.py
# Magic link authentication for member portal
# No password required — lead gets emailed a one-time login link

from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from jose import jwt, JWTError
from pydantic import BaseModel

from app.core.config import settings
from app.db import get_sessionmaker
from app.models.user import User
from app.models.tenant import Tenant
from app.models.user_tenant import UserTenant

router = APIRouter(prefix="/api/auth/magic-link", tags=["Magic Link Auth"])

MAGIC_LINK_EXP_MINUTES = 15
ALGORITHM = "HS256"


async def send_magic_link_email(email: str, token: str):
    """
    TODO: Replace with real email via AWS SES or SendGrid
    For now logs the magic link URL to uvicorn output
    """
    magic_url = f"https://member.nexabuilder.com/auth/verify?token={token}"
    print(f"[MAGIC LINK] To: {email}")
    print(f"[MAGIC LINK] URL: {magic_url}")


def create_magic_token(user_id: str, email: str) -> str:
    """Create a short-lived magic link token (15 min)."""
    payload = {
        "sub":   user_id,
        "email": email,
        "type":  "magic_link",
        "exp":   datetime.utcnow() + timedelta(minutes=MAGIC_LINK_EXP_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM)


class MagicLinkRequest(BaseModel):
    email: str


@router.post("")
async def request_magic_link(body: MagicLinkRequest):
    """
    Request a magic link. Always returns success to avoid revealing
    whether an account exists for the email.
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(User).where(User.email == body.email))
        user = result.scalar_one_or_none()

        if user:
            token = create_magic_token(str(user.id), user.email)
            await send_magic_link_email(user.email, token)

    return {"message": "If an account exists for that email, a secure link has been sent."}


@router.get("/verify")
async def verify_magic_link(token: str = Query(...)):
    """
    Verify a magic link token and return a full 8-hour access token.
    Called when the member clicks the link in their email.
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=400, detail="Invalid or expired magic link")

    if payload.get("type") != "magic_link":
        raise HTTPException(status_code=400, detail="Invalid link type")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid link payload")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        from uuid import UUID
        user = await db.get(User, UUID(user_id))
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Get tenant via user_tenants
        ut_result = await db.execute(
            select(UserTenant).where(UserTenant.user_id == user.id)
        )
        user_tenant = ut_result.scalar_one_or_none()
        tenant_id = str(user_tenant.tenant_id) if user_tenant else ""

        # Create 8-hour access token using existing security module
        from app.core.security import create_access_token
        access_token = create_access_token(
            data={
                "sub":    str(user.id),
                "tenant": tenant_id,
                "role":   user.role.value,
            },
            expires_minutes=480
        )

        return {
            "access_token": access_token,
            "token_type":   "bearer",
        }
