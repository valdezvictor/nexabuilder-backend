"""
app/routers/api/verify.py
==========================
Verification gate for the free assessment.

HOMEOWNER FLOW:
  POST /api/verify/request  → sends 6-digit OTP to email or phone
  POST /api/verify/confirm  → submits code → user verified → assessment released

CONTRACTOR FLOW:
  POST /api/verify/contractor/register  → license lookup → account created → CSLB challenge issued
  POST /api/verify/contractor/challenge → submits answers → CSLB verified → portal access granted

ASSESSMENT GATE:
  GET  /api/verify/assessment/{lead_id} → requires verified JWT → returns full assessment

Business rules:
  - Assessment runs immediately on lead intake (no change to existing flow)
  - Results are stored in lead.ai_assessment but lead.assessment_released = False
  - After email/phone verification → assessment_released = True for that user's leads
  - Contractor access to assessments requires cslb_verified = True
  - Max 3 CSLB challenge attempts before account locked (prevents fishing)
"""

import random
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from typing import Optional
from uuid import UUID

from app.db import get_db
from app.models.user import User, UserRole, UserStatus
from app.models.lead import Lead
from app.models.otp_code import OTPCode
from app.models.contractor_account import ContractorAccount
from app.models.contractor import Contractor
from app.models.license import License
from app.services.otp_service import generate_otp, verify_otp, send_email_otp, send_sms_otp
from app.core.security import create_access_token, create_refresh_token
from app.core.deps import get_current_user

router = APIRouter(prefix="/api/verify", tags=["Verification"])

MAX_CSLB_ATTEMPTS = 3


# ── Schemas ───────────────────────────────────────────────────────────────────

class OTPRequest(BaseModel):
    user_id:  str
    channel:  str           # email | sms
    email:    Optional[str] = None
    phone:    Optional[str] = None


class OTPConfirm(BaseModel):
    user_id: str
    code:    str
    channel: str            # email | sms


class ContractorRegister(BaseModel):
    license_number: str
    state_code:     str = "CA"
    user_id:        str


class ContractorChallenge(BaseModel):
    user_id:  str
    answers:  dict          # {"q1": "answer1", "q2": "answer2"}


# ── HOMEOWNER: Request OTP ────────────────────────────────────────────────────

@router.post("/request", summary="Send OTP to email or phone")
async def request_otp(
    payload: OTPRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Called right after lead intake form submission.
    Sends a 6-digit OTP to email or phone.
    Returns generic response (no user enumeration).
    """
    user = await db.get(User, UUID(payload.user_id))
    if not user:
        # Still return 200 — don't expose which user IDs exist
        return {"message": "If this account exists, a code has been sent."}

    code = await generate_otp(db, payload.user_id, payload.channel, "verification")

    if payload.channel == "email" and payload.email:
        await send_email_otp(payload.email, code)
    elif payload.channel == "sms" and payload.phone:
        await send_sms_otp(payload.phone, code)
    else:
        raise HTTPException(status_code=400, detail="Provide email for email channel or phone for sms channel")

    return {"message": "Verification code sent.", "expires_in_minutes": 10}


# ── HOMEOWNER: Confirm OTP ────────────────────────────────────────────────────

@router.post("/confirm", summary="Verify OTP and release assessment")
async def confirm_otp(
    payload: OTPConfirm,
    db: AsyncSession = Depends(get_db),
):
    """
    Verifies the OTP code.
    On success:
      - Marks user is_email_verified or is_phone_verified
      - Sets assessment_released = True on all of this user's pending leads
      - Returns a full access token for the member portal
    """
    result = await verify_otp(db, payload.user_id, payload.code, payload.channel)

    if not result["valid"]:
        raise HTTPException(status_code=400, detail=result["reason"])

    # Mark user as verified
    user = await db.get(User, UUID(payload.user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.channel == "email":
        user.is_email_verified = True
        user.verification_method = "email_otp"
    else:
        user.is_phone_verified = True
        user.verification_method = "sms_otp"

    # Release assessments for all leads linked to this user
    await db.execute(
        update(Lead)
        .where(Lead.user_id == payload.user_id, Lead.assessment_released == False)
        .values(assessment_released=True)
    )

    await db.commit()

    # Issue full portal access token
    token_data = {"sub": payload.user_id, "role": user.role.value}
    return {
        "message": "Verified. Your assessment is ready.",
        "access_token": create_access_token(token_data),
        "token_type": "bearer",
        "assessment_released": True,
    }


# ── ASSESSMENT GATE ───────────────────────────────────────────────────────────

@router.get("/assessment/{lead_id}", summary="Get full AI assessment (verified users only)")
async def get_assessment(
    lead_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Returns the full AI assessment for a lead.
    Requires:
      - Valid JWT (issued after OTP verification)
      - Lead must belong to this user (lead.user_id == current_user.id)
      - lead.assessment_released == True

    Returns 402 (not 401) if user is authenticated but not yet verified —
    so the frontend can show the verification UI rather than a login wall.
    """
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    if str(lead.user_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not your lead")

    if not lead.assessment_released:
        raise HTTPException(
            status_code=402,
            detail="Assessment pending verification. Please verify your email or phone.",
            headers={"X-Verify-Required": "true"},
        )

    return {
        "lead_id": lead.id,
        "ai_assessment": lead.ai_assessment,
        "estimate": lead.estimate,
        "assessment_released": True,
    }


# ── CONTRACTOR: License lookup ────────────────────────────────────────────────

@router.get("/contractor/lookup/{license_number}", summary="Look up a CSLB license")
async def lookup_license(
    license_number: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Public endpoint. Checks if a license number exists in our CSLB database.
    Returns masked company info — enough to confirm it's them, not enough to abuse.
    Does NOT reveal address details (those are used for the challenge).
    """
    result = await db.execute(
        select(License).where(
            License.license_number == license_number.strip().upper(),
            License.state_code == "CA",
        )
    )
    lic = result.scalars().first()

    if not lic:
        raise HTTPException(
            status_code=404,
            detail="License not found in CSLB database. Check the number and try again."
        )

    # Mask company name — show first 3 chars + *** (confirms it's them without exposing all)
    name = lic.contractor_name or ""
    masked_name = name[:3] + "***" if len(name) > 3 else name

    return {
        "found": True,
        "license_number": license_number.upper(),
        "masked_name": masked_name,
        "classification": lic.classification,
        "status": lic.status,
        "message": "License found. Proceed to register your contractor account.",
    }


# ── CONTRACTOR: Register account ─────────────────────────────────────────────

@router.post("/contractor/register", summary="Register contractor account + issue CSLB challenge")
async def register_contractor(
    payload: ContractorRegister,
    db: AsyncSession = Depends(get_db),
):
    """
    Creates (or retrieves) a ContractorAccount and issues a CSLB identity challenge.

    Challenge design:
    - We pick 2 questions from fields only the real licensee would know:
        Q1: "What ZIP code is your license registered to?" (postal_code)
        Q2: "What year does your workers comp expire?" (workers_comp_expiration year)
        Q3: "What is your bond amount on file?" (bond_amount, rounded to nearest $1k)
    - We ask 2 of 3, randomly selected each attempt (prevents pattern memorization)
    - Correct answer = exact match (case/space-insensitive for strings)
    - Max 3 attempts before lockout
    - On pass: cslb_verified = True, challenge_status = "passed"
    """
    # Lookup license in CSLB DB
    lic_result = await db.execute(
        select(License).where(
            License.license_number == payload.license_number.strip().upper(),
            License.state_code == payload.state_code,
        )
    )
    lic = lic_result.scalars().first()

    if not lic:
        raise HTTPException(status_code=404, detail="License not found in CSLB database.")

    # Check if already registered with a DIFFERENT user
    existing = await db.execute(
        select(ContractorAccount).where(
            ContractorAccount.license_number == payload.license_number.upper(),
            ContractorAccount.cslb_verified == True,
        )
    )
    already_claimed = existing.scalars().first()
    if already_claimed and str(already_claimed.user_id) != payload.user_id:
        raise HTTPException(
            status_code=409,
            detail="This license is already registered to another account. Contact support@nexabuilder.com."
        )

    # Create or retrieve contractor account
    ca_result = await db.execute(
        select(ContractorAccount).where(ContractorAccount.user_id == payload.user_id)
    )
    ca = ca_result.scalars().first()

    if not ca:
        # Get contractor record for company name
        contractor = await db.get(Contractor, lic.contractor_id)
        ca = ContractorAccount(
            user_id=payload.user_id,
            license_number=payload.license_number.upper(),
            state_code=payload.state_code,
            contractor_db_id=lic.contractor_id,
            company_name=contractor.legal_name or contractor.name if contractor else None,
        )
        db.add(ca)
        await db.commit()
        await db.refresh(ca)

    if ca.challenge_status == "locked":
        raise HTTPException(
            status_code=423,
            detail="Account locked after too many failed attempts. Contact support@nexabuilder.com."
        )

    if ca.cslb_verified:
        return {"message": "Already verified.", "cslb_verified": True}

    # Build challenge questions from CSLB data
    questions = _build_challenge_questions(lic)
    if not questions:
        raise HTTPException(
            status_code=503,
            detail="Insufficient CSLB data to generate challenge. Contact support@nexabuilder.com."
        )

    # Pick 2 random questions for this attempt
    selected = random.sample(questions, min(2, len(questions)))

    return {
        "contractor_account_id": ca.id,
        "user_id": payload.user_id,
        "masked_name": (lic.contractor_name or "")[:3] + "***",
        "challenge_questions": [{"id": q["id"], "question": q["question"]} for q in selected],
        "questions_ids": [q["id"] for q in selected],
        "instructions": (
            "Answer both questions exactly as they appear on your CSLB license record. "
            "You have 3 attempts. After 3 failures the account is locked."
        ),
    }


def _build_challenge_questions(lic: License) -> list:
    """
    Build challenge Q&A pairs from CSLB license data.
    Only includes questions where we have the data.
    """
    questions = []

    if lic.postal_code:
        questions.append({
            "id": "q_zip",
            "question": "What is the ZIP code on your CSLB license record?",
            "answer": str(lic.postal_code).strip(),
        })

    if lic.workers_comp_expiration:
        questions.append({
            "id": "q_wc_year",
            "question": "What year does your workers' compensation coverage expire?",
            "answer": str(lic.workers_comp_expiration.year),
        })

    if lic.bond_amount and lic.bond_amount > 0:
        # Round to nearest thousand for readability
        rounded = round(lic.bond_amount / 1000) * 1000
        questions.append({
            "id": "q_bond",
            "question": f"What is your bond amount on file with CSLB (in dollars)?",
            "answer": str(rounded),
        })

    if lic.expiration_date:
        questions.append({
            "id": "q_exp_year",
            "question": "What year does your contractor license expire?",
            "answer": str(lic.expiration_date.year),
        })

    return questions


# ── CONTRACTOR: Submit challenge answers ──────────────────────────────────────

@router.post("/contractor/challenge", summary="Submit CSLB challenge answers")
async def submit_challenge(
    payload: ContractorChallenge,
    db: AsyncSession = Depends(get_db),
):
    """
    Validates the contractor's CSLB challenge answers.

    On pass:
      - ContractorAccount.cslb_verified = True
      - ContractorAccount.challenge_status = "passed"
      - User.verification_method = "cslb_challenge"
      - Returns full contractor portal access token

    On fail:
      - Increments challenge_attempts
      - Locks account at MAX_CSLB_ATTEMPTS
    """
    ca_result = await db.execute(
        select(ContractorAccount).where(ContractorAccount.user_id == payload.user_id)
    )
    ca = ca_result.scalars().first()
    if not ca:
        raise HTTPException(status_code=404, detail="Contractor account not found")

    if ca.challenge_status == "locked":
        raise HTTPException(status_code=423, detail="Account locked. Contact support@nexabuilder.com.")

    if ca.cslb_verified:
        raise HTTPException(status_code=400, detail="Already verified.")

    # Re-fetch the license to validate answers
    lic_result = await db.execute(
        select(License).where(
            License.license_number == ca.license_number,
            License.state_code == ca.state_code,
        )
    )
    lic = lic_result.scalars().first()
    if not lic:
        raise HTTPException(status_code=404, detail="License record not found.")

    all_questions = _build_challenge_questions(lic)
    q_lookup = {q["id"]: q["answer"] for q in all_questions}

    # Validate submitted answers
    wrong = []
    for q_id, submitted in payload.answers.items():
        correct = q_lookup.get(q_id)
        if not correct:
            continue
        if submitted.strip() != correct.strip():
            wrong.append(q_id)

    if wrong:
        ca.challenge_attempts += 1
        if ca.challenge_attempts >= MAX_CSLB_ATTEMPTS:
            ca.challenge_status = "locked"
            await db.commit()
            raise HTTPException(
                status_code=423,
                detail="Account locked after 3 failed attempts. Contact support@nexabuilder.com."
            )
        await db.commit()
        remaining = MAX_CSLB_ATTEMPTS - ca.challenge_attempts
        raise HTTPException(
            status_code=400,
            detail=f"One or more answers are incorrect. {remaining} attempt(s) remaining."
        )

    # ✓ All correct — verify the account
    ca.cslb_verified = True
    ca.challenge_status = "passed"
    ca.challenge_passed_at = datetime.now(timezone.utc)

    # Update user role to contractor + mark verified
    user = await db.get(User, UUID(payload.user_id))
    if user:
        user.role = UserRole.contractor
        user.is_email_verified = True
        user.verification_method = "cslb_challenge"

    await db.commit()

    # Issue contractor portal token
    token_data = {
        "sub": payload.user_id,
        "role": "contractor",
        "license": ca.license_number,
        "cslb_verified": True,
    }
    return {
        "message": "CSLB identity verified. Welcome to NexaBuilder.",
        "cslb_verified": True,
        "license_number": ca.license_number,
        "company_name": ca.company_name,
        "access_token": create_access_token(token_data),
        "token_type": "bearer",
        "portal_url": "https://contractor.nexabuilder.com",
    }
