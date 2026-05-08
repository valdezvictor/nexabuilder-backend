# app/routers/api/magic_link.py
# Magic link auth for member portal (no password required)
# Corrected from Copilot:
# - Uses get_sessionmaker() not get_async_session()
# - Uses settings.JWT_SECRET not JWT_SECRET_KEY
# - Uses python-jose not PyJWT
# - Tenant resolved via user_tenants table
# - create_access_token matches actual signature in security.py

from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from pydantic import BaseModel

from app.core.config import settings
from app.db import get_sessionmaker
from app.models.user import User
from app.models.tenant import Tenant
from app.models.user_tenant import UserTenant
from jose import jwt, JWTError
from app.core.security import create_access_token, create_refresh_token

router = APIRouter(prefix="/auth/magic-link", tags=["Magic Link Auth"])

MAGIC_LINK_EXP_MINUTES = 15


async def _send_magic_link_email(email: str, token: str):
    """Send magic link email via AWS SES"""
    import boto3
    from botocore.exceptions import ClientError

    magic_url = f"https://member.nexabuilder.com/auth/verify?token={token}"
    print(f"[MAGIC LINK] To: {email} | URL: {magic_url}")

    try:
        ses = boto3.client("ses", region_name="us-east-1")
        ses.send_email(
            Source="NexaBuilder <noreply@nexabuilder.com>",
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": "Your NexaBuilder access link", "Charset": "UTF-8"},
                "Body": {
                    "Html": {
                        "Data": f"""
                        <div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto;">
                          <h2 style="color: #2563eb;">Access Your Project</h2>
                          <p>Click the button below to access your NexaBuilder dashboard.</p>
                          <a href="{magic_url}"
                             style="display: inline-block; padding: 12px 24px; background: #2563eb;
                                    color: white; text-decoration: none; border-radius: 6px;
                                    font-size: 16px; margin: 16px 0;">
                            Access My Project
                          </a>
                          <p style="color: #666; font-size: 14px;">
                            This link expires in 15 minutes. If you didn't request this, ignore this email.
                          </p>
                          <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
                          <p style="color: #999; font-size: 12px;">
                            NexaBuilder — Connecting you with the right service provider.
                          </p>
                        </div>
                        """,
                        "Charset": "UTF-8"
                    },
                    "Text": {
                        "Data": f"Access your NexaBuilder project: {magic_url}",
                        "Charset": "UTF-8"
                    }
                }
            }
        )
        print(f"[SES] Magic link email sent to {email}")
    except ClientError as e:
        print(f"[SES ERROR] {e.response['Error']['Message']} - falling back to console log")
        print(f"[MAGIC LINK FALLBACK] {magic_url}")


def _create_magic_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "type": "magic_link",
        "exp": datetime.utcnow() + timedelta(minutes=MAGIC_LINK_EXP_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


class MagicLinkRequest(BaseModel):
    email: str


@router.post("")
async def request_magic_link(payload: MagicLinkRequest):
    """
    Request a magic link for passwordless login.
    Always returns success to prevent user enumeration.
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(User).where(User.email == payload.email))
        user = result.scalar_one_or_none()

        if user:
            token = _create_magic_token(str(user.id), user.email)
            await _send_magic_link_email(payload.email, token)

    return {"message": "If an account exists for that email, a link has been sent."}


@router.get("/verify")
async def verify_magic_link(token: str = Query(...)):
    """
    Verify a magic link token and issue a full access token.
    Called when the user clicks the link in their email.
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=400, detail="Invalid or expired magic link")

    token_type = payload.get("type")
    # Accept magic_link tokens AND direct lead access tokens (phone-only leads)
    if token_type not in ("magic_link", None):
        raise HTTPException(status_code=400, detail="Invalid token type")
    
    # For direct access tokens (phone-only leads), issue token directly
    if token_type is None and payload.get("role") == "lead":
        return {
            "access_token":  token,  # reuse the direct token
            "refresh_token": token,
            "token_type":    "bearer",
        }

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid token payload")

    from uuid import UUID
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        user = await db.get(User, UUID(user_id))
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Get tenant via user_tenants table
        ut_result = await db.execute(
            select(UserTenant).where(UserTenant.user_id == user.id)
        )
        user_tenant = ut_result.scalar_one_or_none()
        tenant_id = str(user_tenant.tenant_id) if user_tenant else ""

        token_data = {
            "sub":    str(user.id),
            "tenant": tenant_id,
            "role":   user.role.value,
        }

        return {
            "access_token":  create_access_token(token_data),
            "refresh_token": create_refresh_token(token_data),
            "token_type":    "bearer",
        }
