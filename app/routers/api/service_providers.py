# app/routers/api/service_providers.py
# Service provider management + auto-match + job offer flow

import secrets
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, and_
from pydantic import BaseModel
from uuid import UUID

from app.core.auth import get_current_user
from app.db import get_sessionmaker
from app.models.service_provider import ServiceProvider, ServiceType, ServiceProviderStatus
from app.models.service_job import ServiceJob, JobStatus
from app.models.lead import Lead
from app.services.sms import send_sms

router = APIRouter(prefix="/api/service-providers", tags=["Service Providers"])


class ServiceProviderCreate(BaseModel):
    email: str
    phone: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company_name: Optional[str] = None
    service_type: str
    base_postal_code: Optional[str] = None
    base_city: Optional[str] = None
    base_state: Optional[str] = None
    max_radius_miles: int = 25
    flat_rate: Optional[float] = None
    commission_pct: Optional[float] = None
    payment_model: str = "flat_rate"
    license_number: Optional[str] = None


class JobOfferRequest(BaseModel):
    lead_id: int
    service_type: str
    description: Optional[str] = None
    flat_rate: Optional[float] = None
    commission_pct: Optional[float] = None
    payment_model: str = "flat_rate"
    contract_amount: Optional[float] = None


@router.post("")
async def create_provider(
    payload: ServiceProviderCreate,
    identity: dict = Depends(get_current_user),
):
    """Admin creates a new service provider."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        provider = ServiceProvider(
            email=payload.email,
            phone=payload.phone,
            first_name=payload.first_name,
            last_name=payload.last_name,
            company_name=payload.company_name,
            service_type=ServiceType(payload.service_type),
            base_postal_code=payload.base_postal_code,
            base_city=payload.base_city,
            base_state=payload.base_state,
            max_radius_miles=payload.max_radius_miles,
            flat_rate=payload.flat_rate,
            commission_pct=payload.commission_pct,
            payment_model=payload.payment_model,
            license_number=payload.license_number,
        )
        db.add(provider)
        await db.commit()
        await db.refresh(provider)
        return {"id": str(provider.id), "email": provider.email, "service_type": payload.service_type}


@router.get("")
async def list_providers(
    service_type: Optional[str] = Query(None),
    postal_code: Optional[str] = Query(None),
    available: Optional[bool] = Query(None),
    identity: dict = Depends(get_current_user),
):
    """List service providers with optional filters."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        stmt = select(ServiceProvider).where(
            ServiceProvider.status == ServiceProviderStatus.active
        )
        if service_type:
            stmt = stmt.where(ServiceProvider.service_type == ServiceType(service_type))
        if available is not None:
            stmt = stmt.where(ServiceProvider.available == available)

        result = await db.execute(stmt)
        providers = result.scalars().all()

        return [{
            "id": str(p.id),
            "email": p.email,
            "phone": p.phone,
            "name": f"{p.first_name or ''} {p.last_name or ''}".strip() or p.company_name,
            "service_type": p.service_type.value,
            "base_city": p.base_city,
            "base_state": p.base_state,
            "base_postal_code": p.base_postal_code,
            "flat_rate": p.flat_rate,
            "commission_pct": p.commission_pct,
            "payment_model": p.payment_model,
            "available": p.available,
            "jobs_completed": p.jobs_completed,
            "rating": p.rating,
        } for p in providers]


@router.post("/match-and-offer")
async def match_and_offer(
    payload: JobOfferRequest,
    identity: dict = Depends(get_current_user),
):
    """
    Auto-match nearest available service provider to a lead and send job offer.
    Sends SMS + email with accept/decline link.
    First provider to accept gets the job.
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Get the lead
        lead_result = await db.execute(select(Lead).where(Lead.id == payload.lead_id))
        lead = lead_result.scalar_one_or_none()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        # Find available providers of this type
        stmt = select(ServiceProvider).where(
            and_(
                ServiceProvider.service_type == ServiceType(payload.service_type),
                ServiceProvider.status == ServiceProviderStatus.active,
                ServiceProvider.available == True,
            )
        ).order_by(ServiceProvider.jobs_completed.desc())  # Prefer experienced providers

        result = await db.execute(stmt)
        providers = result.scalars().all()

        if not providers:
            raise HTTPException(
                status_code=404,
                detail=f"No available {payload.service_type} providers found"
            )

        # Calculate payment amount
        payment_amount = None
        if payload.payment_model == "flat_rate" and payload.flat_rate:
            payment_amount = payload.flat_rate
        elif payload.payment_model == "commission" and payload.commission_pct and payload.contract_amount:
            payment_amount = payload.contract_amount * (payload.commission_pct / 100)

        jobs_created = []

        # Send offer to top 3 available providers (first to accept wins)
        for provider in providers[:3]:
            # Generate unique accept token
            accept_token = secrets.token_urlsafe(32)

            # Create job record
            job = ServiceJob(
                lead_id=payload.lead_id,
                provider_id=provider.id,
                service_type=payload.service_type,
                status=JobStatus.offered,
                description=payload.description,
                payment_model=payload.payment_model,
                flat_rate=payload.flat_rate,
                commission_pct=payload.commission_pct,
                contract_amount=payload.contract_amount,
                payment_amount=payment_amount,
                accept_token=accept_token,
                offer_sent_at=datetime.utcnow(),
            )
            db.add(job)
            await db.flush()

            # Build accept/decline URLs
            accept_url = f"https://api.nexabuilder.com/api/service-jobs/{job.id}/accept?token={accept_token}"
            decline_url = f"https://api.nexabuilder.com/api/service-jobs/{job.id}/decline?token={accept_token}"

            # Build job offer message
            lead_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "A client"
            location = f"{lead.city or ''} {lead.postal_code or ''}".strip()
            service_label = payload.service_type.replace("_", " ").title()

            if payment_amount:
                payment_str = f"${payment_amount:,.0f}" if payload.payment_model == "flat_rate" else f"{payload.commission_pct}% of contract"
            else:
                payment_str = "TBD"

            sms_message = (
                f"NexaBuilder Job Offer: {service_label} needed for {lead_name} in {location}. "
                f"Pay: {payment_str}. "
                f"Accept: {accept_url}"
            )

            # Send SMS if phone available
            if provider.phone:
                phone = provider.phone.replace("-","").replace(" ","").replace("(","").replace(")","")
                if not phone.startswith("+"):
                    phone = "+1" + phone
                send_sms(phone, sms_message)

            # Send email via SES
            try:
                import boto3
                ses = boto3.client("ses", region_name="us-east-1")
                provider_name = f"{provider.first_name or ''} {provider.last_name or ''}".strip() or "Provider"
                ses.send_email(
                    Source="NexaBuilder <service@nexabuilder.com>",
                    Destination={"ToAddresses": [provider.email]},
                    Message={
                        "Subject": {"Data": f"New Job Offer: {service_label} in {location}"},
                        "Body": {
                            "Html": {"Data": f"""
                            <div style="font-family: Arial, sans-serif; max-width: 600px;">
                              <h2 style="color: #2563eb;">New Job Available</h2>
                              <p>Hi {provider_name},</p>
                              <p>A new {service_label} job is available near you:</p>
                              <table style="border-collapse: collapse; width: 100%; margin: 16px 0;">
                                <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Client</strong></td><td style="padding: 8px; border: 1px solid #ddd;">{lead_name}</td></tr>
                                <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Location</strong></td><td style="padding: 8px; border: 1px solid #ddd;">{location}</td></tr>
                                <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Service</strong></td><td style="padding: 8px; border: 1px solid #ddd;">{service_label}</td></tr>
                                <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Payment</strong></td><td style="padding: 8px; border: 1px solid #ddd;">{payment_str}</td></tr>
                                <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Details</strong></td><td style="padding: 8px; border: 1px solid #ddd;">{payload.description or 'See portal for details'}</td></tr>
                              </table>
                              <div style="margin: 24px 0;">
                                <a href="{accept_url}" style="background: #059669; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; margin-right: 12px; font-size: 16px;">Accept Job</a>
                                <a href="{decline_url}" style="background: #dc2626; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-size: 16px;">Decline</a>
                              </div>
                              <p style="color: #666; font-size: 13px;">This offer expires in 24 hours. First provider to accept gets the job.</p>
                            </div>
                            """, "Charset": "UTF-8"},
                            "Text": {"Data": sms_message, "Charset": "UTF-8"}
                        }
                    }
                )
                print(f"[SERVICE JOB] Email sent to {provider.email}")
            except Exception as e:
                print(f"[SERVICE JOB EMAIL ERROR] {e}")

            jobs_created.append({
                "job_id": str(job.id),
                "provider_email": provider.email,
                "provider_name": f"{provider.first_name or ''} {provider.last_name or ''}".strip(),
                "accept_url": accept_url,
            })

        await db.commit()

        return {
            "message": f"Job offer sent to {len(jobs_created)} provider(s)",
            "service_type": payload.service_type,
            "lead_id": payload.lead_id,
            "payment_amount": payment_amount,
            "offers_sent": jobs_created,
        }


# Job accept/decline endpoints (no auth - accessed via SMS/email link)
job_router = APIRouter(prefix="/api/service-jobs", tags=["Service Jobs"])


@job_router.get("/{job_id}/accept")
async def accept_job(job_id: UUID, token: str = Query(...)):
    """Provider accepts a job via link in SMS/email."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(
            select(ServiceJob).where(
                and_(ServiceJob.id == job_id, ServiceJob.accept_token == token)
            )
        )
        job = result.scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found or invalid token")
        if job.status not in (JobStatus.offered,):
            return {"message": f"This job has already been {job.status.value}"}

        job.status = JobStatus.accepted
        job.offer_accepted_at = datetime.utcnow()

        # Cancel other offers for same lead + service type
        other_offers = await db.execute(
            select(ServiceJob).where(
                and_(
                    ServiceJob.lead_id == job.lead_id,
                    ServiceJob.service_type == job.service_type,
                    ServiceJob.id != job.id,
                    ServiceJob.status == JobStatus.offered,
                )
            )
        )
        for other in other_offers.scalars().all():
            other.status = JobStatus.cancelled

        await db.commit()

        return {
            "message": "Job accepted! You will receive the client details shortly.",
            "job_id": str(job_id),
            "next_step": "Log in to your portal to view documents and client information.",
            "portal_url": "https://service.nexabuilder.com"
        }


@job_router.get("/{job_id}/decline")
async def decline_job(job_id: UUID, token: str = Query(...)):
    """Provider declines a job."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(
            select(ServiceJob).where(
                and_(ServiceJob.id == job_id, ServiceJob.accept_token == token)
            )
        )
        job = result.scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="Invalid link")

        job.status = JobStatus.declined
        job.offer_declined_at = datetime.utcnow()
        await db.commit()

        return {"message": "Job declined. Thank you for your response."}
