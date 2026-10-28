# app/routers/api/documents.py
# S3 document management for leads
# Upload/download with signed URLs, access control by role

import boto3
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.core.auth import get_current_user
from app.db import get_sessionmaker
from app.models.lead import Lead

router = APIRouter(prefix="/api/leads", tags=["Documents"])

S3_BUCKET = "nexabuilder-lead-documents"
S3_REGION = "us-west-1"

ALLOWED_CATEGORIES = [
    "blueprints", "permits", "photos", "contracts",
    "loan_docs", "insurance", "estimates", "other"
]


def get_s3_client():
    return boto3.client("s3", region_name=S3_REGION)


def _key(lead_id: int, category: str, filename: str) -> str:
    return f"leads/{lead_id}/{category}/{filename}"


def _check_lead_access(lead, identity: dict) -> bool:
    """Verify the user can access this lead's documents."""
    role = identity["role"]
    user = identity["user"]

    if role == "admin":
        return True
    if role == "lead":
        # Lead can only access their own documents
        # Matched by email or internal alias
        email = getattr(user, "email", "")
        return (email == lead.email or
                email == f"lead-{lead.id}@nexabuilder.internal")
    if role in ("contractor", "agent", "partner"):
        return True  # Can access leads in their tenant
    return False


@router.get("/{lead_id}/documents")
async def list_documents(
    lead_id: int,
    category: Optional[str] = Query(None),
    identity: dict = Depends(get_current_user),
):
    """List all documents for a lead."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        if not _check_lead_access(lead, identity):
            raise HTTPException(status_code=403, detail="Access denied")

    s3 = get_s3_client()
    prefix = f"leads/{lead_id}/"
    if category:
        if category not in ALLOWED_CATEGORIES:
            raise HTTPException(status_code=400, detail=f"Invalid category. Use: {ALLOWED_CATEGORIES}")
        prefix = f"leads/{lead_id}/{category}/"

    try:
        response = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix)
        objects = response.get("Contents", [])
    except Exception as e:
        # Bucket may not exist yet
        objects = []

    documents = []
    for obj in objects:
        key = obj["Key"]
        parts = key.split("/")
        if len(parts) >= 4:
            doc_category = parts[2]
            filename = "/".join(parts[3:])
            documents.append({
                "key": key,
                "category": doc_category,
                "filename": filename,
                "size_bytes": obj.get("Size", 0),
                "uploaded_at": obj.get("LastModified", "").isoformat() if obj.get("LastModified") else None,
            })

    return {
        "lead_id": lead_id,
        "document_count": len(documents),
        "documents": documents,
    }


class UploadRequest(BaseModel):
    filename: str
    category: str
    content_type: str = "application/octet-stream"


@router.post("/{lead_id}/documents/upload-url")
async def get_upload_url(
    lead_id: int,
    payload: UploadRequest,
    identity: dict = Depends(get_current_user),
):
    """
    Get a pre-signed S3 URL for direct upload.
    Client uploads directly to S3 — no file passes through the backend.
    """
    if payload.category not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Invalid category. Use: {ALLOWED_CATEGORIES}")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        if not _check_lead_access(lead, identity):
            raise HTTPException(status_code=403, detail="Access denied")

    # Ensure bucket exists
    s3 = get_s3_client()
    try:
        s3.head_bucket(Bucket=S3_BUCKET)
    except Exception:
        try:
            s3.create_bucket(
                Bucket=S3_BUCKET,
                CreateBucketConfiguration={"LocationConstraint": S3_REGION}
            )
            # Block public access
            s3.put_public_access_block(
                Bucket=S3_BUCKET,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                }
            )
        except Exception as e:
            print(f"[S3] Bucket create error: {e}")

    key = _key(lead_id, payload.category, payload.filename)

    # Generate pre-signed upload URL (valid 15 minutes)
    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": S3_BUCKET,
            "Key": key,
            "ContentType": payload.content_type,
        },
        ExpiresIn=900,
    )

    return {
        "upload_url": upload_url,
        "key": key,
        "bucket": S3_BUCKET,
        "expires_in_seconds": 900,
        "instructions": "PUT the file directly to upload_url with Content-Type header matching content_type",
    }


@router.get("/{lead_id}/documents/download-url")
async def get_download_url(
    lead_id: int,
    key: str = Query(..., description="S3 object key from list_documents"),
    identity: dict = Depends(get_current_user),
):
    """Get a pre-signed S3 URL for secure download."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        if not _check_lead_access(lead, identity):
            raise HTTPException(status_code=403, detail="Access denied")

    # Verify the key belongs to this lead
    if not key.startswith(f"leads/{lead_id}/"):
        raise HTTPException(status_code=403, detail="Key does not belong to this lead")

    s3 = get_s3_client()
    download_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=3600,  # 1 hour
    )

    filename = key.split("/")[-1]
    return {
        "download_url": download_url,
        "filename": filename,
        "expires_in_seconds": 3600,
    }


class MarketingOptIn(BaseModel):
    opt_in: bool
    consent_text: Optional[str] = None


@router.post("/{lead_id}/documents/marketing-consent")
async def update_marketing_consent(
    lead_id: int,
    payload: MarketingOptIn,
    identity: dict = Depends(get_current_user),
):
    """
    Lead opts in/out of allowing NexaBuilder to use their project documents
    as portfolio/marketing samples on the website.
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        if not _check_lead_access(lead, identity):
            raise HTTPException(status_code=403, detail="Access denied")

        # Store consent in lead's ai_assessment field (reuse JSONB)
        assessment = lead.ai_assessment or {}
        assessment["marketing_consent"] = {
            "opted_in": payload.opt_in,
            "consent_text": payload.consent_text or (
                "I consent to NexaBuilder using photos and documents from my project "
                "as portfolio samples and marketing materials on nexabuilder.com"
            ),
            "consented_at": datetime.utcnow().isoformat(),
        }
        lead.ai_assessment = assessment
        await db.commit()

        return {
            "lead_id": lead_id,
            "marketing_consent": payload.opt_in,
            "message": "Consent recorded. Thank you!" if payload.opt_in else "Opt-out recorded.",
        }
