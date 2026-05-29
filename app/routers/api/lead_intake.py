from app.services.klaviyo import klaviyo_sync_lead
# app/routers/api/lead_intake.py
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import select
from uuid import uuid4
from datetime import datetime, timedelta

from app.db import get_sessionmaker
from app.models.lead import Lead
from app.models.user import User, UserRole, UserStatus
from app.models.user_tenant import UserTenant
from app.models.tenant import Tenant
from app.core.security import hash_password
from app.services.sms import send_magic_link_sms
from app.routers.api.contractor_matching import should_route_internal, INTERNAL_CONTRACTOR
from app.services.assessment_gate import (
    check_homeowner_assessment_eligibility, record_homeowner_assessment,
    check_rate_limit, log_rate_limit_attempt, build_90_day_block_message
)
from app.routers.api.partner_routing import find_matching_partners
from app.services.ai_intake import assess_lead
from jose import jwt
from app.core.config import settings

router = APIRouter(prefix="/api/leads", tags=["Lead Intake"])


class LeadIntakeRequest(BaseModel):
    vertical:          str
    project_type:      Optional[str]   = None
    first_name:        Optional[str]   = None
    last_name:         Optional[str]   = None
    email:             Optional[str]   = None
    phone:             Optional[str]   = None
    postal_code:       Optional[str]   = None
    needs_financing:   Optional[bool]  = False
    financing_amount:  Optional[float] = None
    description:       Optional[str]  = None
    source:            Optional[str]  = "web_form"
    # ── Attribution / tracking fields ────────────────────────────────────
    site_id:           Optional[str]  = None   # internal site identifier (e.g. "unapiscina", "improvementwizards")
    source_domain:     Optional[str]  = None   # full domain of originating site
    referrer_url:      Optional[str]  = None   # HTTP referrer at form load
    landing_page:      Optional[str]  = None   # specific page/path visitor landed on
    utm_source:        Optional[str]  = None   # google / facebook / newsletter / organic
    utm_medium:        Optional[str]  = None   # cpc / email / social / referral
    utm_campaign:      Optional[str]  = None   # campaign name
    utm_content:       Optional[str]  = None   # ad variant / creative ID
    utm_term:          Optional[str]  = None   # search keyword
    affiliate_id:      Optional[str]  = None   # affiliate partner ID
    sub_id:            Optional[str]  = None   # affiliate sub-ID (for their tracking)
    click_id:          Optional[str]  = None   # gclid / fbclid / network click ID
    # ── Consent fields ────────────────────────────────────────────────────
    tcpa_consent:      Optional[bool] = False
    tcpa_timestamp:    Optional[str]  = None   # ISO timestamp of consent
    tcpa_text:         Optional[str]  = None   # exact consent language shown
    newsletter_optin:  Optional[bool] = False
    language:          Optional[str]  = "en"


def _create_access_token(user_id: str, tenant_id: str) -> str:
    """Create a 30-day access token for phone-only leads"""
    payload = {
        "sub": user_id,
        "tenant": tenant_id,
        "role": "lead",
        "exp": datetime.utcnow() + timedelta(days=30),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


@router.post("/intake")
async def submit_lead(payload: LeadIntakeRequest):
    """
    Public endpoint - no auth required.
    Creates a Lead record and a User account for portal access.
    Phone-only leads get a direct-access token URL (for SMS).
    """
    if not payload.email and not payload.phone:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Email or phone is required")

    # ── Address required for assessment gate ──────────────────────────────────
    if not payload.postal_code:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Property ZIP code is required for assessment")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Create the lead record
        lead = Lead(
            vertical=payload.vertical,
            project_type=payload.project_type,
            project_description=payload.description,
            source=payload.source,
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
            phone=payload.phone,
            postal_code=payload.postal_code,
            # Attribution / tracking
            site_id=payload.site_id,
            source_domain=payload.source_domain,
            referrer_url=payload.referrer_url,
            landing_page=payload.landing_page,
            utm_source=payload.utm_source,
            utm_medium=payload.utm_medium,
            utm_campaign=payload.utm_campaign,
            utm_content=payload.utm_content,
            utm_term=payload.utm_term,
            affiliate_id=payload.affiliate_id,
            sub_id=payload.sub_id,
            click_id=payload.click_id,
            # Consent
            tcpa_consent=payload.tcpa_consent or False,
            newsletter_optin=payload.newsletter_optin or False,
            language=payload.language or "en",
        )
        db.add(lead)
        await db.flush()

        # Get member tenant
        tenant_result = await db.execute(
            select(Tenant).where(Tenant.domain == "member.nexabuilder.com")
        )
        tenant = tenant_result.scalar_one_or_none()
        tenant_id = str(tenant.id) if tenant else ""

        # Find or create user account
        user = None
        if payload.email:
            existing = await db.execute(select(User).where(User.email == payload.email))
            user = existing.scalar_one_or_none()

        if not user:
            # For phone-only: generate internal email alias
            email_for_account = payload.email or f"lead-{lead.id}@nexabuilder.internal"
            user = User(
                id=uuid4(),
                email=email_for_account,
                password_hash=hash_password(str(uuid4())),
                role=UserRole.lead,
                status=UserStatus.active
            )
            db.add(user)
            await db.flush()

            if tenant:
                db.add(UserTenant(id=uuid4(), user_id=user.id, tenant_id=tenant.id))

        # ── Property assessment gate (non-blocking, isolated session) ──────────
        gate_check = {"eligible": True, "action": "create", "address_hash": ""}
        try:
            # Use a fresh session so gate errors don't taint the main session
            async with SessionLocal() as gate_db:
                gate_check = await check_homeowner_assessment_eligibility(
                    user_id=str(user.id),
                    address_line1=getattr(payload, "address_line1", "") or "",
                    city=getattr(payload, "city", "") or "",
                    state=getattr(payload, "state", "") or "CA",
                    postal_code=payload.postal_code or "",
                    db=gate_db,
                )
            if not gate_check["eligible"]:
                from fastapi import HTTPException
                detail = gate_check["reason"]
                if gate_check.get("error_code") == "DUPLICATE_PROPERTY_ASSESSMENT":
                    detail = build_90_day_block_message(
                        f'{payload.postal_code or "this address"}'
                    )
                raise HTTPException(
                    status_code=409,
                    detail=detail,
                    headers={"X-Error-Code": gate_check.get("error_code", "ASSESSMENT_BLOCKED")},
                )
        except Exception as gate_err:
            # Gate check failed (e.g. schema mismatch) — log and continue
            print(f"[ASSESSMENT GATE] Non-blocking error: {gate_err}")
            gate_check = {"eligible": True, "action": "create", "address_hash": ""}

        # Link user to lead and commit BEFORE gate check
        # (gate check can fail due to DB type issues — commit must happen first)
        try:
            lead.user_id = str(user.id)
        except Exception:
            pass
        lead.assessment_released = False

        # Auto-flag demo/VIP emails for full contractor visibility
        DEMO_EMAILS = {
            "finance911@gmail.com",   # Raul Cruz - Finance 911
            "member@nexabuilder.com",  # Victor member portal testing
        }
        if (lead.email or "").lower().strip() in DEMO_EMAILS:
            import json as _json
            demo_flags = _json.dumps({
                "show_all_contractors": True,
                "vip": True,
                "demo_user": lead.email
            })
            from sqlalchemy import text as _text
            # Will be set after commit via separate statement
            lead._demo_email = True

        await db.commit()
        await db.refresh(lead)

        # Sync to Klaviyo in thread pool (non-blocking)
        import asyncio as _asyncio
        _asyncio.get_event_loop().run_in_executor(None, klaviyo_sync_lead, lead)


        # Apply demo flags if needed (after commit to avoid transaction conflict)
        if getattr(lead, '_demo_email', False):
            async with SessionLocal() as demo_db:
                import json as _json2
                flag_val = _json2.dumps({"show_all_contractors": True, "vip": True, "demo_user": lead.email})
                await demo_db.execute(_text(
                    "UPDATE leads SET demo_flags = CAST(:flag AS jsonb) WHERE id = :lid"
                ), {"flag": flag_val, "lid": lead.id})
                await demo_db.commit()

        # Record property assessment (non-blocking, fresh session)
        try:
            async with SessionLocal() as rec_db:
                await record_homeowner_assessment(
                    user_id=str(user.id),
                    lead_id=lead.id,
                    vertical=payload.vertical,
                    address_line1=getattr(payload, "address_line1", "") or "",
                    city=getattr(payload, "city", "") or "",
                    state=getattr(payload, "state", "") or "CA",
                    postal_code=payload.postal_code or "",
                    db=rec_db,
                    existing_id=gate_check.get("existing_id"),
                )
        except Exception as rec_err:
            print(f"[RECORD ASSESSMENT] Non-blocking error: {rec_err}")

        # Run AI intake assessment
        ai_assessment = assess_lead(
            vertical=payload.vertical,
            project_type=payload.project_type,
            description=payload.description,
            postal_code=payload.postal_code,
            budget=getattr(payload, 'budget', None),
            first_name=payload.first_name,
            last_name=payload.last_name,
            phone=payload.phone,
            email=payload.email,
        )

        # For phone-only leads: generate direct access URL with 30-day token
        token_url = None
        if payload.phone and not payload.email:
            direct_token = _create_access_token(str(user.id), tenant_id)
            token_url = f"https://member.nexabuilder.com/auth/verify?token={direct_token}"

        # Save AI assessment to lead record
        if lead.id and ai_assessment.get('ai_assessed'):
            lead.ai_assessment = ai_assessment
            await db.commit()

        # Auto internal routing check
        if ai_assessment.get('ai_assessed'):
            if should_route_internal(lead, ai_assessment):
                from app.services.sms import send_sms
                lead_name = f"{payload.first_name or ''} {payload.last_name or ''}".strip() or "New Lead"
                score = ai_assessment.get('complexity_score', 'N/A')
                cost = ai_assessment.get('estimated_cost_range', 'TBD')
                sms_msg = (
                    f"NexaBuilder INTERNAL LEAD: {lead_name} | "
                    f"{payload.project_type or payload.vertical} | "
                    f"ZIP {payload.postal_code} | Score {score}/10 | {cost} | "
                    f"Review: https://admin.nexabuilder.com/leads/{lead.id}"
                )
                send_sms(INTERNAL_CONTRACTOR['phone'], sms_msg)
                print(f"[INTERNAL ROUTE] Lead #{lead.id} routed to Victor's crew")

        # Partner matching — runs after internal routing check
        if ai_assessment.get('routing_recommendation') == 'partner_or_network':
            try:
                async with SessionLocal() as match_db:
                    partner_matches = await find_matching_partners(
                        lead_score=ai_assessment.get('composite_score') or ai_assessment.get('complexity_score', 5),
                        project_type=payload.project_type or '',
                        description=payload.description or '',
                        vertical=payload.vertical or '',
                        postal_code=payload.postal_code or '',
                        db=match_db,
                    )
                if partner_matches:
                    top_partner = partner_matches[0]
                    ai_assessment['routing_recommendation'] = 'partner'
                    ai_assessment['matched_partner'] = {
                        'name': top_partner['name'],
                        'slug': top_partner['slug'],
                        'matched_verticals': top_partner['matched_verticals'],
                        'commission_pct': top_partner['commission_pct'],
                    }
                    print(f"[PARTNER MATCH] Lead #{lead.id} → {top_partner['name']} ({top_partner['matched_verticals']})")
                else:
                    ai_assessment['routing_recommendation'] = 'network'
                    print(f"[NETWORK ROUTE] Lead #{lead.id} → ping-post networks")
            except Exception as e:
                print(f"[PARTNER MATCH ERROR] {e}")

        # Auto-send SMS for phone-only leads
        if token_url and payload.phone:
            phone = payload.phone.replace("-","").replace(" ","").replace("(","").replace(")","")
            if not phone.startswith("+"):
                phone = "+1" + phone
            send_magic_link_sms(phone, token_url)

        # Auto-route to Raul Cruz if financing requested
        if payload.needs_financing:
            try:
                from sqlalchemy import text as _text
                # Get Raul's lending partner record
                lp = await db.execute(_text(
                    "SELECT id, name, email FROM lending_partners WHERE is_primary = TRUE AND is_active = TRUE LIMIT 1"
                ))
                lp_row = lp.fetchone()
                if lp_row:
                    # Create lending application record
                    await db.execute(_text("""
                        INSERT INTO lending_applications
                            (lead_id, lender_name, status, loan_type,
                             requested_amount, applicant_email, applicant_phone, notes)
                        VALUES
                            (:lead_id, :lender_name, 'pending', :loan_type,
                             :amount, :email, :phone, :notes)
                    """), {
                        "lead_id":     lead.id,
                        "lender_name": lp_row[1],
                        "loan_type":   payload.vertical,
                        "amount":      payload.financing_amount,
                        "email":       payload.email,
                        "phone":       payload.phone,
                        "notes":       f"Auto-routed from intake. Vertical: {payload.vertical}",
                    })
                    # Update lead with lender reference
                    await db.execute(_text(
                        "UPDATE leads SET needs_financing=TRUE, lender_ref=:ref, financing_amount=:amt WHERE id=:id"
                    ), {"ref": lp_row[1], "amt": payload.financing_amount, "id": lead.id})
                    await db.commit()
            except Exception as fin_err:
                print(f"[Financing routing] non-fatal error: {fin_err}")

        return {
            "id":       lead.id,
            "user_id":  str(user.id),
            "message":  "Lead submitted successfully",
            "verification_required": True,
            "verification_channels": ["email"] if payload.email else ["sms"],
            "email":    lead.email,
            "phone":    lead.phone,
            "source":   payload.source,
            "token_url": token_url,
            "ai_assessment": ai_assessment,
        }
