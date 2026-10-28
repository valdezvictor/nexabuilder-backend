
# ── valdezvictor.com contact form ─────────────────────────────────────────────
from fastapi import APIRouter as _CR
from pydantic import BaseModel as _BM
import boto3 as _b3, os as _os

_contact_router = _CR(prefix="/api/contact", tags=["contact"])

class _ContactMsg(_BM):
    name:    str
    email:   str
    subject: str = ""
    message: str

@_contact_router.post("/valdezvictor")
async def send_contact(payload: _ContactMsg):
    """Contact form handler for valdezvictor.com — sends via SES."""
    ses = _b3.client("ses", region_name="us-east-1")
    body = (
        f"From: {payload.name} <{payload.email}>\n"
        f"Subject: {payload.subject or 'valdezvictor.com contact'}\n\n"
        f"{payload.message}"
    )
    try:
        ses.send_email(
            Source="noreply@nexabuilder.com",
            Destination={"ToAddresses": ["valdez.victor@gmail.com"]},
            Message={
                "Subject": {"Data": f"[valdezvictor.com] {payload.subject or payload.name}"},
                "Body": {"Text": {"Data": body}},
            },
            ReplyToAddresses=[payload.email],
        )
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
