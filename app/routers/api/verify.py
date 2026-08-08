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
from app.core.auth import get_current_user

def require_admin(identity: dict = Depends(get_current_user)) -> bool:
    if identity.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return True
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
from app.services.assessment_gate import check_contractor_assessment_eligibility
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
    # Query contractors table (243K+ CSLB records)
    from sqlalchemy import text as sqlt2
    lic_clean = license_number.strip().upper().replace("-", "")
    lic_raw   = license_number.strip().upper()

    result = await db.execute(sqlt2(
        "SELECT id, business_name, license_no, zip_code, city, "
        "bond_amount, primary_status, classifications "
        "FROM contractors "
        "WHERE REPLACE(license_no, '-', '') = :clean OR license_no = :raw "
        "LIMIT 1"
    ), {"clean": lic_clean, "raw": lic_raw})
    row = result.fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="License not found in CSLB database. Check the number and try again."
        )

    name = row[1] or ""
    masked_name = name[:3] + "***" if len(name) > 3 else name

    # Check if license already has an active contractor_account
    already = await db.execute(sqlt2(
        "SELECT u.email FROM contractor_accounts ca "
        "JOIN users u ON u.id=ca.user_id "
        "WHERE ca.license_number=:raw OR ca.license_number=:clean "
        "LIMIT 1"
    ), {"raw": lic_raw, "clean": lic_clean})
    already_row = already.fetchone()
    already_registered = already_row is not None

    return {
        "found":              True,
        "license_number":     lic_raw,
        "masked_name":        masked_name,
        "classification":     row[7] or "",
        "status":             row[6] or "CLEAR",
        "already_registered": already_registered,
        "message":            (
            "License already registered. Use your email or phone to sign in."
            if already_registered else
            "License found. Proceed to register your contractor account."
        ),
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
    from sqlalchemy import text as sqlt3
    lic_raw   = payload.license_number.strip().upper()
    lic_clean = lic_raw.replace("-", "")
    lic_result = await db.execute(sqlt3(
        "SELECT id, business_name, license_no, zip_code, city, "
        "bond_amount, primary_status, classifications "
        "FROM contractors "
        "WHERE REPLACE(license_no, '-', '') = :clean OR license_no = :raw "
        "LIMIT 1"
    ), {"clean": lic_clean, "raw": lic_raw})
    lic_row = lic_result.fetchone()
    # Create a simple namespace object for compatibility
    class _Lic:
        def __init__(self, r):
            self.license_number  = r[2]
            self.contractor_name = r[1]
            self.zip_code        = r[3]
            self.city            = r[4]
            self.bond_amount     = r[5]
            self.status          = r[6] or "CLEAR"
            self.classification  = r[7] or ""
    lic = _Lic(lic_row) if lic_row else None

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


# ── ADMIN: Create active project (link contractor to a project manually) ──────

class ActiveProjectCreate(BaseModel):
    license_number: str
    address_line1:  str
    city:           str
    state:          str = "CA"
    postal_code:    str
    lead_id:        Optional[int] = None
    vertical:       Optional[str] = None
    source:         str = "contractor_added"


@router.post(
    "/admin/active-project",
    summary="Manually create an active project (admin or contractor self-add)"
)
async def create_active_project(
    payload: ActiveProjectCreate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """
    Creates an ActiveProject record linking a contractor to a property.
    This grants the contractor the right to run assessments at that address.

    Called by:
    - Admin when manually assigning a contractor to a project
    - Contractor portal when they self-report a new project they're working on
      (requires cslb_verified=True on their account)
    """
    from app.services.address_service import address_hash as calc_hash, address_raw
    from app.models.active_project import ActiveProject

    ahash = calc_hash(payload.address_line1, payload.city, payload.state, payload.postal_code)
    raw   = address_raw(payload.address_line1, payload.city, payload.state, payload.postal_code)

    # Check if already exists
    existing = await db.execute(
        select(ActiveProject).where(
            ActiveProject.license_number == payload.license_number.upper(),
            ActiveProject.address_hash == ahash,
        )
    )
    project = existing.scalars().first()

    if project:
        project.project_status = "active"
        if payload.lead_id:
            project.lead_id = payload.lead_id
        await db.commit()
        await db.refresh(project)
        return {"created": False, "updated": True, "project": {
            "id": project.id, "address_hash": ahash, "license": payload.license_number.upper()
        }}

    project = ActiveProject(
        license_number=payload.license_number.upper(),
        address_hash=ahash,
        address_line1=payload.address_line1,
        city=payload.city,
        state=payload.state,
        postal_code=payload.postal_code,
        lead_id=payload.lead_id,
        vertical=payload.vertical,
        source=payload.source,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    return {"created": True, "project": {
        "id": project.id,
        "address_hash": ahash,
        "address": raw,
        "license_number": payload.license_number.upper(),
        "source": payload.source,
    }}


@router.get(
    "/admin/active-projects/{license_number}",
    summary="List active projects for a contractor license"
)
async def list_active_projects(
    license_number: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    from app.models.active_project import ActiveProject
    result = await db.execute(
        select(ActiveProject).where(
            ActiveProject.license_number == license_number.upper()
        ).order_by(ActiveProject.created_at.desc())
    )
    projects = result.scalars().all()
    return {
        "license_number": license_number.upper(),
        "total": len(projects),
        "projects": [
            {
                "id": p.id,
                "address_line1": p.address_line1,
                "city": p.city,
                "state": p.state,
                "postal_code": p.postal_code,
                "project_status": p.project_status,
                "vertical": p.vertical,
                "assessment_count": p.assessment_count,
                "source": p.source,
                "lead_id": p.lead_id,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in projects
        ]
    }



@router.get("/contractor/public-lookup/{license_number}", summary="Public CSLB lookup for registration")
async def public_lookup(license_number: str, db: AsyncSession = Depends(get_db)):
    """Public endpoint - no auth required. Used by registration form."""
    from sqlalchemy import text as sqlt4
    lic_raw   = license_number.strip().upper()
    lic_clean = lic_raw.replace("-", "").replace(" ", "")

    sql = (
        "SELECT id, business_name, license_no, city, zip_code, phone, "
        "primary_status, classifications, bond_amount "
        "FROM contractors "
        "WHERE REPLACE(license_no, '-', '') = :clean "
        "   OR license_no = :raw "
        "LIMIT 1"
    )
    result = await db.execute(sqlt4(sql), {"clean": lic_clean, "raw": lic_raw})
    row = result.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="License not found in CSLB database.")

    name    = row[1] or ""
    city    = row[3] or ""
    status  = row[6] or "UNKNOWN"
    is_act  = status.upper() in ("CLEAR", "ACTIVE")
    ph      = row[5] or ""

    return {
        "found":          True,
        "contractor_id":  row[0],
        "license_number": row[2],
        "masked_name":    name[:3] + ("*" * max(3, len(name)-3)) if len(name) > 3 else name,
        "masked_city":    city[:2] + ("*" * max(2, len(city)-2)) if len(city) > 2 else city,
        "classification": row[7] or "",
        "status":         status,
        "is_active":      is_act,
        "city_hint":      city,
        "zip_hint":       row[4] or "",
        "phone_hint":     ph[:3] + "****" + ph[-2:] if len(ph) >= 5 else "",
        "message": "License verified. Please complete your registration." if is_act
                   else "License found but status is " + status + ". Contact support.",
    }

async def close_active_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    from app.models.active_project import ActiveProject
    project = await db.get(ActiveProject, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project.project_status = "completed"
    await db.commit()
    return {"closed": True, "project_id": project_id}


@router.post("/contractor/public-register",
             summary="Public contractor self-registration — no admin key required")
async def public_register(
    payload: dict,
    db: AsyncSession = Depends(get_db),
):
    """Full contractor self-registration. Validates CSLB, smart-validates contact info, creates account, sends magic link."""
    import uuid as _uuid, re, secrets, boto3
    from datetime import datetime, timedelta
    from sqlalchemy import text as _t

    lic_raw    = (payload.get("license_number") or "").strip().upper()
    lic_clean  = lic_raw.replace("-", "")
    email      = (payload.get("email") or "").strip().lower()
    phone_raw  = (payload.get("phone") or "").strip()
    first_name = (payload.get("first_name") or "").strip()
    last_name  = (payload.get("last_name") or "").strip()
    city       = (payload.get("city") or "").strip()
    zip_code   = (payload.get("zip_code") or "").strip()

    if not lic_raw or not email:
        raise HTTPException(status_code=422, detail="License number and email are required.")

    # 1. CSLB lookup against contractors table
    res = await db.execute(_t(
        "SELECT id, business_name, license_no, zip_code, city, phone,"
        " primary_status, classifications"
        " FROM contractors"
        " WHERE REPLACE(license_no,'-','')=:clean OR license_no=:raw"
        " LIMIT 1"
    ), {"clean": lic_clean, "raw": lic_raw})
    cslb = res.fetchone()

    if not cslb:
        raise HTTPException(status_code=404,
            detail="License not found in CSLB database.")

    cslb_status = (cslb[6] or "").upper()
    if cslb_status and cslb_status not in ("CLEAR", "ACTIVE"):
        raise HTTPException(status_code=422,
            detail="License status is '" + (cslb[6] or "") + "'. Only CLEAR licenses may register.")

    warnings = []

    # 2. Smart validation against CSLB record
    cslb_zip = (cslb[3] or "").strip()
    if cslb_zip and zip_code and zip_code != cslb_zip:
        warnings.append("ZIP " + zip_code + " differs from CSLB record (" + cslb_zip + ").")

    cslb_city_l = (cslb[4] or "").strip().lower()
    sub_city_l  = city.lower()
    if cslb_city_l and sub_city_l and cslb_city_l[:4] not in sub_city_l and sub_city_l[:4] not in cslb_city_l:
        warnings.append("City '" + city + "' differs from CSLB record ('" + (cslb[4] or "") + "').")

    phone_digits = re.sub(r"\D", "", phone_raw)
    if phone_digits and len(phone_digits) != 10:
        raise HTTPException(status_code=422, detail="Phone must be a valid 10-digit US number.")
    if phone_digits and phone_digits[:3] in ("000", "911"):
        raise HTTPException(status_code=422, detail="Phone area code is not valid.")

    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=422, detail="Email format is invalid.")
    if email.split("@")[-1] in {"mailinator.com","guerrillamail.com","throwaway.email"}:
        raise HTTPException(status_code=422, detail="Please use a valid business email.")

    # 3. Create or reactivate user
    ex = await db.execute(_t(
        "SELECT id FROM users WHERE email=:em LIMIT 1"
    ), {"em": email})
    ex_row = ex.fetchone()

    if ex_row:
        user_id = str(ex_row[0])
        await db.execute(_t(
            "UPDATE users SET status='active', first_name=:fn, last_name=:ln,"
            " updated_at=NOW() WHERE id=:uid"
        ), {"fn": first_name or cslb[1], "ln": last_name or "", "uid": user_id})
    else:
        user_id = str(_uuid.uuid4())
        # Check if phone already taken by another user
        ph_to_use = phone_digits or None
        if ph_to_use:
            ph_check = await db.execute(_t(
                "SELECT id FROM users WHERE phone=:ph LIMIT 1"
            ), {"ph": ph_to_use})
            if ph_check.fetchone():
                ph_to_use = None  # Skip phone if already used
        await db.execute(_t(
            "INSERT INTO users"
            " (id, email, phone, role, status, first_name, last_name, created_at, updated_at)"
            " VALUES (:uid, :em, :ph, 'contractor', 'active', :fn, :ln, NOW(), NOW())"
        ), {"uid": user_id, "em": email, "ph": ph_to_use,
            "fn": first_name or cslb[1], "ln": last_name or ""})

    # 4. Create or update contractor_account
    ca = await db.execute(_t(
        "SELECT id FROM contractor_accounts WHERE user_id=:uid LIMIT 1"
    ), {"uid": user_id})
    if ca.fetchone():
        await db.execute(_t(
            "UPDATE contractor_accounts SET license_number=:lic, cslb_verified=TRUE,"
            " contractor_db_id=:cid, company_name=:nm, updated_at=NOW() WHERE user_id=:uid"
        ), {"lic": lic_raw, "cid": cslb[0], "nm": cslb[1], "uid": user_id})
    else:
        await db.execute(_t(
            "INSERT INTO contractor_accounts"
            " (user_id, license_number, state_code, cslb_verified, challenge_status,"
            "  challenge_passed_at, contractor_db_id, company_name, created_at, updated_at)"
            " VALUES (:uid, :lic, 'CA', TRUE, 'passed', NOW(), :cid, :nm, NOW(), NOW())"
        ), {"uid": user_id, "lic": lic_raw, "cid": cslb[0], "nm": cslb[1]})

    await db.commit()

    # 5. Issue magic link
    token  = secrets.token_urlsafe(32)
    exp_at = datetime.utcnow() + timedelta(minutes=30)
    import uuid as _uuid2
    tok_id = str(_uuid2.uuid4())
    await db.execute(_t(
        "INSERT INTO auth_tokens (id, user_id, token, type, expires_at, created_at)"
        " VALUES (:tid, :uid, :tok, 'email_magic_link', :exp, NOW())"
    ), {"tid": tok_id, "uid": user_id, "tok": token, "exp": exp_at})
    await db.commit()

    magic_url  = "https://contractor.nexabuilder.com/auth/verify?token=" + token
    email_sent = False
    try:
        ses = boto3.client("ses", region_name="us-east-1")
        ses.send_email(
            Source="NexaBuilder <noreply@nexabuilder.com>",
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": "Activate Your NexaBuilder Contractor Account"},
                "Body": {"Html": {"Data": (
                    "<div style='font-family:sans-serif;max-width:560px;margin:0 auto;padding:32px'>"
                    "<img src='https://www.nexabuilder.com/images/NexaBuilder_logo.png' height='36' alt='NexaBuilder'/>"
                    "<h2 style='color:#0d1e35;margin-top:24px'>Welcome, " + (first_name or cslb[1] or "Contractor") + "!</h2>"
                    "<p>Your <strong>" + (cslb[1] or "") + "</strong> contractor account is ready.</p>"
                    "<a href='" + magic_url + "' style='display:inline-block;margin:24px 0;padding:14px 28px;"
                    "background:#C8922A;color:#fff;text-decoration:none;border-radius:8px;font-weight:700'>"
                    "Activate My Dashboard &rarr;</a>"
                    "<p style='color:#888;font-size:12px'>Link expires in 30 minutes. NexaBuilder &middot; CSLB #1127866</p>"
                    "</div>"
                )}},
            }
        )
        email_sent = True
    except Exception as e:
        print("WARNING: Magic link email failed " + email + ": " + str(e))

    return {
        "success":             True,
        "user_id":             user_id,
        "email_sent":          email_sent,
        "magic_url":           magic_url if not email_sent else None,
        "cslb_match": {
            "business_name":  cslb[1],
            "license_no":     cslb[2],
            "city":           cslb[4],
            "status":         cslb[6],
            "classification": cslb[7],
        },
        "validation_warnings": warnings,
        "message": (
            "Account created! Check " + email + " for your activation link."
            if email_sent else
            "Account created. Use this link to activate: " + magic_url
        ),
    }

# ── CONTRACTOR: Account Recovery (public) ────────────────────────────────────

class ContractorRecovery(BaseModel):
    license_number: str
    email:          Optional[str] = None
    phone:          Optional[str] = None

@router.post("/contractor/recovery",
             summary="Send sign-in link to already-registered contractor")
async def contractor_recovery(
    payload: ContractorRecovery,
    db: AsyncSession = Depends(get_db),
):
    import uuid as _uuid2, secrets, boto3
    from datetime import datetime, timedelta
    from sqlalchemy import text as _t2

    lic_raw   = payload.license_number.strip().upper()
    lic_clean = lic_raw.replace("-","")
    email     = (payload.email or "").strip().lower() or None
    phone_raw = (payload.phone or "").strip()
    import re as _re
    phone = _re.sub(r"\D","",phone_raw) if phone_raw else None

    # Find user via license + email or phone
    if email:
        res = await db.execute(_t2(
            "SELECT u.id, u.email FROM users u "
            "JOIN contractor_accounts ca ON ca.user_id=u.id "
            "WHERE (ca.license_number=:raw OR ca.license_number=:clean) "
            "AND u.email=:em LIMIT 1"
        ), {"raw":lic_raw,"clean":lic_clean,"em":email})
    elif phone:
        res = await db.execute(_t2(
            "SELECT u.id, u.email FROM users u "
            "JOIN contractor_accounts ca ON ca.user_id=u.id "
            "WHERE (ca.license_number=:raw OR ca.license_number=:clean) "
            "AND u.phone=:ph LIMIT 1"
        ), {"raw":lic_raw,"clean":lic_clean,"ph":phone})
    else:
        raise HTTPException(status_code=422,
            detail="Provide either email or phone number.")

    row = res.fetchone()
    if not row:
        raise HTTPException(status_code=404,
            detail="No account found matching that license and contact info.")

    user_id    = str(row[0])
    user_email = row[1]

    # Issue new magic link
    token  = secrets.token_urlsafe(32)
    exp_at = datetime.utcnow() + timedelta(minutes=30)
    tok_id = str(_uuid2.uuid4())
    await db.execute(_t2(
        "INSERT INTO auth_tokens(id,user_id,token,type,expires_at,created_at) "
        "VALUES(:tid,:uid,:tok,'email_magic_link',:exp,NOW())"
    ), {"tid":tok_id,"uid":user_id,"tok":token,"exp":exp_at})
    await db.commit()

    magic_url = "https://contractor.nexabuilder.com/auth/verify?token="+token

    email_sent = False
    try:
        ses = boto3.client("ses", region_name="us-east-1")
        ses.send_email(
            Source="NexaBuilder <noreply@nexabuilder.com>",
            Destination={"ToAddresses":[user_email]},
            Message={
                "Subject":{"Data":"Your NexaBuilder Sign-In Link"},
                "Body":{"Html":{"Data":(
                    "<div style='font-family:sans-serif;max-width:520px;margin:0 auto;padding:32px'>"
                    "<img src='https://www.nexabuilder.com/images/NexaBuilder_logo.png' height='36' alt='NexaBuilder'/>"
                    "<h2 style='color:#0d1e35;margin-top:24px'>Sign in to NexaBuilder</h2>"
                    "<p>Click the button below to access your contractor dashboard.</p>"
                    "<a href='"+magic_url+"' style='display:inline-block;margin:24px 0;padding:14px 28px;"
                    "background:#C8922A;color:#fff;text-decoration:none;border-radius:8px;font-weight:700'>"
                    "Sign In to My Dashboard &rarr;</a>"
                    "<p style='color:#888;font-size:12px'>Link expires in 30 minutes. "
                    "NexaBuilder &middot; CSLB #1127866</p></div>"
                )}},
            }
        )
        email_sent = True
    except Exception as e:
        print("WARNING: Recovery email failed "+user_email+": "+str(e))

    return {
        "success":    True,
        "email_sent": email_sent,
        "magic_url":  magic_url if not email_sent else None,
        "message":    "Sign-in link sent to "+user_email+"." if email_sent else
                      "Account found. Use this link: "+magic_url,
    }
