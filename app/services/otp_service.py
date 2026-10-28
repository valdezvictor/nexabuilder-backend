"""
app/services/otp_service.py
============================
Generates, stores, sends, and verifies one-time passwords.

Flow:
  1. generate_and_send_otp(user_id, email_or_phone, channel) → code stored, sent
  2. verify_otp(user_id, code, channel) → True/False
     - Marks is_used=True on success
     - Increments attempts on failure
     - Returns False if expired, already used, or max attempts reached

Security:
  - 6-digit numeric code
  - 10-minute TTL
  - Max 5 attempts per code
  - Old unused codes for same user+channel are invalidated on new request
"""
import random
import string
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.models.otp_code import OTPCode

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5


def _generate_code() -> str:
    """6-digit numeric code — easy to read aloud, hard to guess."""
    return "".join(random.choices(string.digits, k=6))


async def generate_otp(
    db: AsyncSession,
    user_id: str,
    channel: str,           # email | sms
    purpose: str = "verification",
) -> str:
    """
    Creates a new OTP, invalidates any unused previous codes for this
    user+channel, and returns the plaintext code for sending.
    """
    # Invalidate existing unused codes for this user+channel
    await db.execute(
        update(OTPCode)
        .where(
            OTPCode.user_id == user_id,
            OTPCode.channel == channel,
            OTPCode.is_used == False,
        )
        .values(is_used=True)
    )

    code = _generate_code()
    otp = OTPCode(
        user_id=user_id,
        code=code,
        channel=channel,
        purpose=purpose,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MINUTES),
    )
    db.add(otp)
    await db.commit()
    return code


async def verify_otp(
    db: AsyncSession,
    user_id: str,
    submitted_code: str,
    channel: str,
) -> dict:
    """
    Verifies a submitted OTP code.

    Returns:
        {"valid": True} on success
        {"valid": False, "reason": "..."} on failure
    """
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(OTPCode)
        .where(
            OTPCode.user_id == user_id,
            OTPCode.channel == channel,
            OTPCode.is_used == False,
            OTPCode.expires_at > now,
        )
        .order_by(OTPCode.created_at.desc())
        .limit(1)
    )
    otp = result.scalars().first()

    if not otp:
        return {"valid": False, "reason": "No active code found. Request a new one."}

    if otp.attempts >= OTP_MAX_ATTEMPTS:
        return {"valid": False, "reason": "Too many attempts. Request a new code."}

    if otp.code != submitted_code.strip():
        otp.attempts += 1
        await db.commit()
        remaining = OTP_MAX_ATTEMPTS - otp.attempts
        return {"valid": False, "reason": f"Incorrect code. {remaining} attempt(s) remaining."}

    # ✓ Valid
    otp.is_used = True
    await db.commit()
    return {"valid": True}


async def send_email_otp(email: str, code: str, purpose: str = "verification"):
    """Send OTP via AWS SES."""
    import boto3
    from botocore.exceptions import ClientError

    subject_map = {
        "verification": "Verify your NexaBuilder account",
        "login": "Your NexaBuilder login code",
    }
    subject = subject_map.get(purpose, "Your NexaBuilder code")

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;">
      <h2 style="color:#1d6fde;">NexaBuilder</h2>
      <p>Your verification code is:</p>
      <div style="font-size:42px;font-weight:900;letter-spacing:12px;color:#0a1628;
                  background:#f5f0e8;padding:24px 32px;border-radius:12px;
                  text-align:center;margin:24px 0;">{code}</div>
      <p style="color:#666;font-size:14px;">
        This code expires in {OTP_TTL_MINUTES} minutes. Do not share it with anyone.
      </p>
      <p style="color:#999;font-size:12px;">
        If you didn't request this code, you can safely ignore this email.
      </p>
    </div>
    """

    try:
        ses = boto3.client("ses", region_name="us-east-1")
        ses.send_email(
            Source="NexaBuilder <noreply@nexabuilder.com>",
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html, "Charset": "UTF-8"},
                    "Text": {"Data": f"Your NexaBuilder code: {code} (expires in {OTP_TTL_MINUTES} min)", "Charset": "UTF-8"},
                },
            },
        )
        print(f"[OTP] Email sent to {email}")
    except ClientError as e:
        print(f"[OTP EMAIL ERROR] {e} — code: {code}")


async def send_sms_otp(phone: str, code: str, purpose: str = "verification"):
    """Send OTP via AWS SNS."""
    import boto3

    msg_map = {
        "verification": f"Your NexaBuilder verification code is: {code}. Expires in {OTP_TTL_MINUTES} min. Do not share.",
        "login": f"Your NexaBuilder login code: {code}. Expires in {OTP_TTL_MINUTES} min.",
    }
    message = msg_map.get(purpose, f"NexaBuilder code: {code}")

    # Normalize phone
    phone = phone.replace("-","").replace(" ","").replace("(","").replace(")","").replace(".","")
    if not phone.startswith("+"):
        phone = "+1" + phone

    try:
        sns = boto3.client("sns", region_name="us-east-1")
        sns.publish(PhoneNumber=phone, Message=message)
        print(f"[OTP] SMS sent to {phone}")
    except Exception as e:
        print(f"[OTP SMS ERROR] {e} — code: {code}")
