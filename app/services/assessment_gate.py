"""
app/services/assessment_gate.py
================================
Enforces the one-assessment-per-property and contractor-project-required rules.

HOMEOWNER GATE:
  check_homeowner_assessment_eligibility(user_id, address_fields, db)
  → {"eligible": True}                         — first assessment at this address
  → {"eligible": False, "reason": "..."}        — duplicate detected

  Rules:
    1. If the same user submits for the same address again:
       → Update existing record (count++, date updated). Always eligible.
       → Homeowner can re-assess their own property (scope changed, etc.)
    2. If a DIFFERENT user assessed the same address within 90 days:
       → Return ineligible. Someone already ran an assessment here recently.
       → This catches a contractor pretending to be the homeowner.
    3. After 90 days: eligible again (project scope may have changed).

  Note: We don't block homeowners from re-assessing their own property.
  The restriction is cross-user — you can't assess someone else's address.

CONTRACTOR GATE:
  check_contractor_assessment_eligibility(license_number, address_hash, db)
  → {"eligible": True, "project": {...}}        — active project found
  → {"eligible": False, "reason": "..."}        — no active project

  Rules:
    1. Must have ContractorAccount with cslb_verified=True
    2. Must have an ActiveProject row matching license_number + address_hash
       with project_status == 'active'
    3. No active project → ineligible (prevents fishing for estimates)

ACTIVE PROJECT CREATION:
  create_active_project_from_lead(lead, license_number, db)
  Called automatically when a lead is matched to a contractor.
  This is what gives the contractor the right to run assessments on that property.
"""
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func

from app.models.property_assessment import PropertyAssessment
from app.models.active_project import ActiveProject
from app.models.contractor_account import ContractorAccount
from app.services.address_service import address_hash, address_raw, normalize_address

ASSESSMENT_WINDOW_DAYS = 90


async def check_homeowner_assessment_eligibility(
    user_id: str,
    address_line1: str,
    city: str,
    state: str,
    postal_code: str,
    db: AsyncSession,
) -> dict:
    """
    Checks if a homeowner is eligible to run an assessment at this address.
    Returns {"eligible": True} or {"eligible": False, "reason": "..."}.
    """
    ahash = address_hash(address_line1, city, state, postal_code)
    cutoff = datetime.now(timezone.utc) - timedelta(days=ASSESSMENT_WINDOW_DAYS)

    # Check 1: Has this user already assessed this address?
    own_result = await db.execute(
        select(PropertyAssessment).where(
            PropertyAssessment.address_hash == ahash,
            PropertyAssessment.user_id == user_id,
        )
    )
    own_record = own_result.scalars().first()
    if own_record:
        # Same user re-assessing their own property — always allowed, just update
        return {"eligible": True, "action": "update", "existing_id": own_record.id, "address_hash": ahash}

    # Check 2: Has a DIFFERENT user assessed this address in the last 90 days?
    other_result = await db.execute(
        select(PropertyAssessment).where(
            PropertyAssessment.address_hash == ahash,
            PropertyAssessment.user_id != user_id,
            PropertyAssessment.last_assessed_at >= cutoff,
        )
    )
    other_record = other_result.scalars().first()
    if other_record:
        return {
            "eligible": False,
            "reason": (
                "An assessment for this property address was recently submitted. "
                "If you believe this is an error, contact support@nexabuilder.com."
            ),
            "error_code": "DUPLICATE_PROPERTY_ASSESSMENT",
        }

    return {"eligible": True, "action": "create", "address_hash": ahash}


async def record_homeowner_assessment(
    user_id: str,
    lead_id: int,
    vertical: str,
    address_line1: str,
    city: str,
    state: str,
    postal_code: str,
    db: AsyncSession,
    existing_id: int = None,
) -> PropertyAssessment:
    """
    Creates or updates the PropertyAssessment record after eligibility check passes.
    Call this after the lead is created and eligibility is confirmed.
    """
    ahash = address_hash(address_line1, city, state, postal_code)
    raw   = address_raw(address_line1, city, state, postal_code)

    if existing_id:
        # Update existing record (same user, same address)
        record = await db.get(PropertyAssessment, existing_id)
        if record:
            record.assessment_count += 1
            record.last_assessed_at = datetime.now(timezone.utc)
            record.lead_id = lead_id  # point to latest lead
            await db.commit()
            return record

    # Create new record
    record = PropertyAssessment(
        address_hash=ahash,
        address_raw=raw,
        address_line1=address_line1,
        city=city,
        state=state,
        postal_code=postal_code,
        user_id=user_id,
        lead_id=lead_id,
        vertical=vertical,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def check_contractor_assessment_eligibility(
    license_number: str,
    address_line1: str,
    city: str,
    state: str,
    postal_code: str,
    db: AsyncSession,
) -> dict:
    """
    Checks if a CSLB-verified contractor can run an assessment at this address.
    Requires an active ActiveProject record linking them to this property.
    """
    ahash = address_hash(address_line1, city, state, postal_code)

    # Check for active project at this address
    result = await db.execute(
        select(ActiveProject).where(
            ActiveProject.license_number == license_number.upper(),
            ActiveProject.address_hash == ahash,
            ActiveProject.project_status == "active",
        )
    )
    project = result.scalars().first()

    if not project:
        return {
            "eligible": False,
            "reason": (
                "No active project found for this address under your license. "
                "Assessments are only available for projects actively linked to your account. "
                "If this project was assigned through NexaBuilder, contact support@nexabuilder.com."
            ),
            "error_code": "NO_ACTIVE_PROJECT",
            "address_hash": ahash,
        }

    return {
        "eligible": True,
        "project_id": project.id,
        "project_status": project.project_status,
        "lead_id": project.lead_id,
        "vertical": project.vertical,
        "address_hash": ahash,
    }


async def record_contractor_assessment(
    project_id: int,
    db: AsyncSession,
) -> None:
    """Increment assessment counter on the active project record."""
    project = await db.get(ActiveProject, project_id)
    if project:
        project.assessment_count += 1
        project.last_assessment_at = datetime.now(timezone.utc)
        await db.commit()


async def create_active_project_from_lead(
    lead,
    license_number: str,
    db: AsyncSession,
    source: str = "nexabuilder_lead",
) -> ActiveProject:
    """
    Called when a lead is formally matched to a contractor.
    This is what gives the contractor the right to run assessments on that property.
    Idempotent — if a project already exists for this license+address, returns it.
    """
    ahash = address_hash(
        lead.address_line1 or "",
        lead.city or "",
        lead.state or "CA",
        lead.postal_code or "",
    )

    # Check if already exists
    existing = await db.execute(
        select(ActiveProject).where(
            ActiveProject.license_number == license_number.upper(),
            ActiveProject.address_hash == ahash,
        )
    )
    project = existing.scalars().first()
    if project:
        # Update status to active if it was previously completed/cancelled
        if project.project_status != "active":
            project.project_status = "active"
            project.lead_id = lead.id
            await db.commit()
        return project

    # Create new
    project = ActiveProject(
        license_number=license_number.upper(),
        address_hash=ahash,
        address_line1=lead.address_line1,
        city=lead.city,
        state=lead.state or "CA",
        postal_code=lead.postal_code,
        lead_id=lead.id,
        vertical=lead.vertical,
        source=source,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


# ─────────────────────────────────────────────────────────────────────────────
# RATE LIMITER  — known-address re-assessments only
# Max 2 per user per rolling 60-minute window.
# Only triggered when the address already has a property_assessment record.
# New addresses bypass this — the property gate handles those upstream.
# ─────────────────────────────────────────────────────────────────────────────
from app.models.assessment_rate_log import AssessmentRateLog

RATE_LIMIT_MAX        = 2
RATE_LIMIT_WINDOW_MIN = 60


async def check_rate_limit(
    user_id: str,
    address_hash: str,
    db: AsyncSession,
) -> dict:
    """
    Called ONLY when the address already exists in property_assessments
    (i.e. it already has a lead attached). Returns:
        {"allowed": True}
        {"allowed": False, "reason": "...", "retry_after_minutes": N}
    """
    from sqlalchemy import func as sqlfunc
    window_start = datetime.now(timezone.utc) - timedelta(minutes=RATE_LIMIT_WINDOW_MIN)

    result = await db.execute(
        select(sqlfunc.count(AssessmentRateLog.id)).where(
            AssessmentRateLog.user_id      == user_id,
            AssessmentRateLog.attempted_at >= window_start,
        )
    )
    count = result.scalar() or 0

    if count >= RATE_LIMIT_MAX:
        # Find oldest log entry in window to calculate retry time
        oldest_result = await db.execute(
            select(AssessmentRateLog.attempted_at).where(
                AssessmentRateLog.user_id      == user_id,
                AssessmentRateLog.attempted_at >= window_start,
            ).order_by(AssessmentRateLog.attempted_at.asc()).limit(1)
        )
        oldest = oldest_result.scalar()
        if oldest:
            reset_at = oldest + timedelta(minutes=RATE_LIMIT_WINDOW_MIN)
            retry_in = max(1, int((reset_at - datetime.now(timezone.utc)).total_seconds() / 60))
        else:
            retry_in = RATE_LIMIT_WINDOW_MIN

        return {
            "allowed": False,
            "reason": (
                f"You have submitted {RATE_LIMIT_MAX} assessments for known addresses "
                f"in the last hour. Please wait {retry_in} minute(s) before submitting another, "
                f"or contact support@nexabuilder.com if you need assistance."
            ),
            "retry_after_minutes": retry_in,
            "error_code": "RATE_LIMIT_EXCEEDED",
        }

    return {"allowed": True, "current_count": count}


async def log_rate_limit_attempt(
    user_id: str,
    address_hash: str,
    lead_id: int,
    db: AsyncSession,
) -> None:
    """Log a known-address assessment attempt for rate limiting."""
    entry = AssessmentRateLog(
        user_id=user_id,
        address_hash=address_hash,
        lead_id=lead_id,
    )
    db.add(entry)
    await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# 90-DAY MESSAGE — new homeowner hitting existing property assessment
# Returns a clear, friendly explanation with contact info.
# ─────────────────────────────────────────────────────────────────────────────

def build_90_day_block_message(
    address_display: str = "this address",
) -> str:
    """
    Friendly message shown when a different user tries to assess a property
    that already has an assessment within the 90-day window.
    """
    return (
        f"A NexaBuilder assessment for {address_display} was completed recently. "
        f"To protect homeowner privacy and prevent duplicate assessments, "
        f"each property address can only be assessed once per 90-day period. \n\n"
        f"If you are the new owner of this property or believe this is an error, "
        f"we're happy to lift this restriction for you — just reach out directly:\n"
        f"  Email: support@nexabuilder.com\n"
        f"  Phone: (213) 878-4536\n\n"
        f"Our team typically responds within 1 business day."
    )
