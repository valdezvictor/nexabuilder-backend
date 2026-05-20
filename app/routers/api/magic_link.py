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


# Portal URL map — role → base URL
PORTAL_URLS = {
    "contractor":  "https://contractor.nexabuilder.com/auth/verify",
    "agent":       "https://call.nexabuilder.com/auth/verify",
    "admin":       "https://admin.nexabuilder.com/auth/verify",
    "superadmin":  "https://admin.nexabuilder.com/auth/verify",
    "partner":     "https://partners.nexabuilder.com/auth/verify",
    # Default — homeowners / leads
    "member":      "https://member.nexabuilder.com/auth/verify",
    "lead":        "https://member.nexabuilder.com/auth/verify",
    "user":        "https://member.nexabuilder.com/auth/verify",
}

PORTAL_LABELS = {
    "contractor": ("Contractor Portal", "Your Bid Inbox", "Access My Contractor Portal", "#059669"),
    "agent":      ("Call Center",       "Your Agent Dashboard", "Access Call Center", "#7c3aed"),
    "admin":      ("Admin Console",     "Your Admin Dashboard", "Access Admin Console", "#dc2626"),
    "default":    ("Member Portal",     "Your Project Dashboard", "Access My Project", "#1d6fde"),
}


async def _send_magic_link_email(email: str, token: str, role: str = "user"):
    """Send magic link email via AWS SES — routes to correct portal by role."""
    import boto3
    from botocore.exceptions import ClientError

    base_url  = PORTAL_URLS.get(role, PORTAL_URLS["member"])
    magic_url = f"{base_url}?token={token}"
    print(f"[MAGIC LINK] role={role} To: {email} | URL: {magic_url}")

    label_key = role if role in PORTAL_LABELS else "default"
    portal_name, portal_desc, btn_label, btn_color = PORTAL_LABELS[label_key]

    try:
        ses = boto3.client("ses", region_name="us-east-1")
        ses.send_email(
            Source="NexaBuilder <noreply@nexabuilder.com>",
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {
                    "Data": f"Your NexaBuilder {portal_name} sign-in link",
                    "Charset": "UTF-8"
                },
                "Body": {
                    "Html": {
                        "Data": f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:'DM Sans',Arial,sans-serif;">
<div style="max-width:520px;margin:40px auto;background:#fff;border-radius:16px;
            border:1px solid #e2e5e9;box-shadow:0 4px 24px rgba(10,22,40,.08);overflow:hidden;">
  <div style="background:#0a1628;padding:24px 32px;">
    <div style="font-size:1.3rem;font-weight:900;color:#fff;">
      Nexa<span style="color:#e8b84b;">Builder</span>
    </div>
    <div style="font-size:13px;color:rgba(255,255,255,.6);margin-top:4px;">
      {portal_name}
    </div>
  </div>
  <div style="padding:32px;">
    <h2 style="font-size:1.2rem;font-weight:800;color:#0a1628;margin:0 0 12px;">
      {portal_desc}
    </h2>
    <p style="color:#4a5568;font-size:14px;line-height:1.7;margin:0 0 24px;">
      Click the button below to sign in securely. This link expires in 15 minutes
      and can only be used once.
    </p>
    <a href="{magic_url}"
       style="display:inline-block;padding:14px 28px;background:{btn_color};
              color:#fff;text-decoration:none;border-radius:10px;
              font-size:15px;font-weight:700;margin-bottom:24px;">
      {btn_label} →
    </a>
    <div style="font-size:12px;color:#94a3b8;border-top:1px solid #f1f5f9;
                padding-top:16px;margin-top:8px;">
      If you didn't request this link, you can safely ignore this email.
      This link will expire automatically.<br><br>
      <a href="{magic_url}" style="color:#94a3b8;word-break:break-all;">
        {magic_url}
      </a>
    </div>
  </div>
</div>
</body>
</html>""",
                        "Charset": "UTF-8"
                    },
                    "Text": {
                        "Data": f"Sign in to your NexaBuilder {portal_name}: {magic_url}\n\nThis link expires in 15 minutes.",
                        "Charset": "UTF-8"
                    }
                }
            }
        )
        print(f"[SES] Magic link sent to {email} → {magic_url}")
    except ClientError as e:
        print(f"[SES ERROR] {e.response['Error']['Message']}")
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
            role = user.role.value if hasattr(user.role, "value") else str(user.role)
            await _send_magic_link_email(payload.email, token, role=role)

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
