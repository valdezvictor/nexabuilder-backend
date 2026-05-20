# app/routers/api/contractor_outreach.py
# Call center outreach flow:
#   1. Agent searches CSLB DB by vertical/ZIP
#   2. Agent sends SMS bid invitation to contractor
#   3. Contractor replies with email
#   4. Agent records email, creates portal account, sends magic link
#   5. Contractor signs agreement, views bid, accepts

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text, select, update
from datetime import datetime, timezone, timedelta
from typing import Optional
import uuid

from app.core.auth import get_current_user
from app.db import get_sessionmaker
from app.models.user import User
from app.models.user_tenant import UserTenant
from app.models.contractor_account import ContractorAccount
from app.models.routing_event import RoutingEvent
from app.services.sms import send_sms

router = APIRouter(prefix="/api/outreach", tags=["Contractor Outreach"])


# ── 1. Search CSLB DB ─────────────────────────────────────────────────────────

@router.get("/contractors/search")
async def search_contractors_for_outreach(
    vertical:       Optional[str] = None,
    classification: Optional[str] = None,
    zip_code:       Optional[str] = None,
    city:           Optional[str] = None,
    county:         Optional[str] = None,
    limit:          int = 25,
    offset:         int = 0,
    identity: dict = Depends(get_current_user),
):
    """
    Search CSLB contractor DB for outreach.
    Returns contractors with portal status (none/invited/registered/active).
    """
    # Primary classification map
    CLASS_MAP = {
        "pool": "C-53", "roofing": "C-39", "electrical": "C-10",
        "plumbing": "C-36", "hvac": "C-20", "framing": "C-5",
        "concrete": "C-8", "landscaping": "C-27", "solar": "C-46",
        "general": "B", "remodel": "B",
    }

    cls = classification or (CLASS_MAP.get((vertical or "").lower()) if vertical else None)

    conditions = ["c.primary_status = 'CLEAR'",
                  "(c.expiration_date IS NULL OR c.expiration_date > NOW())"]
    params: dict = {"limit": limit, "offset": offset}

    if cls:
        conditions.append("c.classifications ILIKE :cls")
        params["cls"] = f"%{cls}%"
    if zip_code:
        conditions.append("c.zip_code = :zip"); params["zip"] = zip_code
    if city:
        conditions.append("UPPER(c.city) ILIKE :city"); params["city"] = f"%{city.upper()}%"
    if county:
        conditions.append("UPPER(c.county) ILIKE :county"); params["county"] = f"%{county.upper()}%"

    where = " AND ".join(conditions)

    query = f"""
        SELECT
            c.id, c.license_no, c.business_name, c.full_business_name,
            c.city, c.county, c.zip_code, c.phone, c.classifications,
            c.primary_status, c.expiration_date, c.email,
            -- Portal status
            ca.id         AS account_id,
            ca.cslb_verified,
            u.email       AS portal_email,
            u.created_at  AS portal_joined,
            -- Last outreach SMS
            (SELECT re.created_at FROM routing_events re
             WHERE re.contractor_id = c.id AND re.event_type = 'sms_sent'
             ORDER BY re.created_at DESC LIMIT 1) AS last_sms_at,
            (SELECT re.created_at FROM routing_events re
             WHERE re.contractor_id = c.id AND re.event_type = 'email_captured'
             ORDER BY re.created_at DESC LIMIT 1) AS email_captured_at
        FROM contractors c
        LEFT JOIN contractor_accounts ca ON ca.contractor_db_id = c.id
        LEFT JOIN users u ON u.id::text = ca.user_id::text
        WHERE {where}
        ORDER BY
            CASE WHEN ca.id IS NOT NULL THEN 0 ELSE 1 END,
            c.license_no DESC
        LIMIT :limit OFFSET :offset
    """

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = (await db.execute(text(query), params)).fetchall()
        total_r = await db.execute(text(
            f"SELECT COUNT(*) FROM contractors c WHERE {where}"), params)
        total = total_r.scalar()

    contractors = []
    for r in rows:
        # Determine portal status
        if r[14]:  # portal_email
            status = "active"
        elif r[12]:  # account_id
            status = "registered"
        elif r[17]:  # email_captured_at
            status = "email_captured"
        elif r[16]:  # last_sms_at
            status = "sms_sent"
        else:
            status = "not_contacted"

        contractors.append({
            "id":              r[0],
            "license_number":  r[1],
            "business_name":   r[2] or r[3],
            "city":            r[4],
            "county":          r[5],
            "zip_code":        r[6],
            "phone":           r[7],
            "classifications": r[8],
            "expiration_date": r[10].isoformat() if r[10] else None,
            "email_on_file":   r[11],  # from CSLB record (often empty)
            "portal_email":    r[14],  # from portal account
            "portal_joined":   r[15].isoformat() if r[15] else None,
            "last_sms_at":     r[16].isoformat() if r[16] else None,
            "email_captured_at": r[17].isoformat() if r[17] else None,
            "portal_status":   status,
            # status: not_contacted | sms_sent | email_captured | registered | active
        })

    return {
        "total":       total,
        "limit":       limit,
        "offset":      offset,
        "contractors": contractors,
        "filters": {"vertical": vertical, "classification": cls,
                    "zip_code": zip_code, "city": city, "county": county},
    }


# ── 2. Send outreach SMS ──────────────────────────────────────────────────────

@router.post("/contractors/{contractor_id}/sms")
async def send_outreach_sms(
    contractor_id: int,
    body: dict,
    identity: dict = Depends(get_current_user),
):
    """
    Send initial outreach SMS to a CSLB contractor.
    Records event in routing_events for tracking.
    Message template can be customized per vertical.
    """
    lead_id    = body.get("lead_id")       # optional — tie to a specific lead
    message    = body.get("message", "").strip()
    vertical   = body.get("vertical", "home improvement")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Get contractor
        r = await db.execute(text(
            "SELECT license_no, business_name, phone, city, county, classifications "
            "FROM contractors WHERE id = :id"
        ), {"id": contractor_id})
        contractor = r.fetchone()
        if not contractor:
            raise HTTPException(status_code=404, detail="Contractor not found")

        phone = contractor[2]
        if not phone:
            raise HTTPException(status_code=422,
                detail="No phone number on file for this contractor")

        # Build default message if not provided
        if not message:
            city_str = contractor[3] or "your area"
            message = (
                f"Hi, this is NexaBuilder. We have a {vertical} project in {city_str} "
                f"matching your license #{contractor[0]}. "
                f"Reply with your email to receive project details and a portal invitation. "
                f"Reply STOP to opt out."
            )

        # Normalize phone
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) == 10:
            digits = "1" + digits
        e164 = "+" + digits

        # Send SMS
        sent = send_sms(e164, message)

        # Log event
        agent_user = identity["user"]
        db.add(RoutingEvent(
            lead_id=lead_id,
            contractor_id=contractor_id,
            event_type="sms_sent",
            payload={
                "phone":     e164,
                "message":   message,
                "sent":      sent,
                "agent_id":  str(agent_user.id),
                "agent_email": agent_user.email,
            }
        ))
        await db.commit()

    return {
        "success":    sent,
        "phone":      e164,
        "contractor": contractor[1],
        "message":    message,
        "note": "SMS sent via AWS SNS" if sent else "SMS failed — check phone format"
    }


# ── 3. Capture email reply ────────────────────────────────────────────────────

@router.post("/contractors/{contractor_id}/capture-email")
async def capture_contractor_email(
    contractor_id: int,
    body: dict,
    identity: dict = Depends(get_current_user),
):
    """
    Record contractor email (from SMS reply or manual entry).
    Creates or updates portal User account.
    Does NOT send portal invite yet — use /send-invite for that.
    """
    email    = (body.get("email") or "").strip().lower()
    lead_id  = body.get("lead_id")
    notes    = body.get("notes", "")

    if "@" not in email:
        raise HTTPException(status_code=422, detail="Valid email required")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Verify contractor exists
        r = await db.execute(text(
            "SELECT license_no, business_name, phone, classifications "
            "FROM contractors WHERE id = :id"
        ), {"id": contractor_id})
        contractor = r.fetchone()
        if not contractor:
            raise HTTPException(status_code=404, detail="Contractor not found")

        # Check if user already exists with this email
        existing_user = (await db.execute(
            select(User).where(User.email == email)
        )).scalar_one_or_none()

        if not existing_user:
            # Create contractor user
            new_user = User(
                id=uuid.uuid4(),
                email=email,
                role="contractor",
                is_active=True,
            )
            db.add(new_user)
            await db.flush()
            user_id = new_user.id

            # Get default tenant
            tenant_r = await db.execute(text(
                "SELECT id FROM tenants LIMIT 1"
            ))
            tenant_row = tenant_r.fetchone()
            if tenant_row:
                db.add(UserTenant(user_id=user_id, tenant_id=tenant_row[0]))
        else:
            user_id = existing_user.id

        # Create or update contractor_account
        acct = (await db.execute(
            select(ContractorAccount)
            .where(ContractorAccount.contractor_db_id == contractor_id)
        )).scalar_one_or_none()

        if not acct:
            acct = ContractorAccount(
                user_id=str(user_id),
                license_number=contractor[0],
                contractor_db_id=contractor_id,
                company_name=contractor[1],
                cslb_verified=True,  # we know they're in CSLB DB
                challenge_status="passed",
            )
            db.add(acct)

        # Also update the contractors.email field
        await db.execute(text(
            "UPDATE contractors SET email = :email WHERE id = :id"
        ), {"email": email, "id": contractor_id})

        # Log event
        agent_user = identity["user"]
        db.add(RoutingEvent(
            lead_id=lead_id,
            contractor_id=contractor_id,
            event_type="email_captured",
            payload={
                "email":      email,
                "agent_id":   str(agent_user.id),
                "notes":      notes,
                "user_created": not existing_user,
            }
        ))
        await db.commit()

    return {
        "success":       True,
        "email":         email,
        "contractor":    contractor[1],
        "license":       contractor[0],
        "user_created":  not existing_user,
        "next_step":     f"POST /api/outreach/contractors/{contractor_id}/send-invite",
    }


# ── 4. Send portal invite (magic link) ────────────────────────────────────────

@router.post("/contractors/{contractor_id}/send-invite")
async def send_portal_invite(
    contractor_id: int,
    body: dict,
    identity: dict = Depends(get_current_user),
):
    """
    Send portal magic link to contractor.
    Requires email already captured via /capture-email.
    Optionally ties to a specific lead for the bid.
    """
    lead_id = body.get("lead_id")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Get contractor + email
        r = await db.execute(text(
            "SELECT c.license_no, c.business_name, c.email, "
            "u.id as user_id, u.email as portal_email "
            "FROM contractors c "
            "LEFT JOIN contractor_accounts ca ON ca.contractor_db_id = c.id "
            "LEFT JOIN users u ON u.id::text = ca.user_id::text "
            "WHERE c.id = :id"
        ), {"id": contractor_id})
        row = r.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Contractor not found")

        email = row[4] or row[2]  # portal email or CSLB email
        if not email:
            raise HTTPException(status_code=422,
                detail="No email on file. Capture email first via /capture-email")

        # Get the user object to send magic link
        if row[3]:  # user_id from portal
            user = await db.get(User, row[3])
        else:
            user = (await db.execute(
                select(User).where(User.email == email)
            )).scalar_one_or_none()

        if not user:
            raise HTTPException(status_code=404,
                detail="User account not found. Run /capture-email first.")

        # Import and use the magic link sender
        from app.routers.api.magic_link import _create_magic_token, _send_magic_link_email
        token = _create_magic_token(str(user.id), user.email)
        await _send_magic_link_email(email, token, role="contractor")

        # Log event
        agent_user = identity["user"]
        db.add(RoutingEvent(
            lead_id=lead_id,
            contractor_id=contractor_id,
            event_type="portal_invite_sent",
            payload={
                "email":     email,
                "agent_id":  str(agent_user.id),
                "lead_id":   lead_id,
            }
        ))
        await db.commit()

    return {
        "success":  True,
        "email":    email,
        "message":  f"Portal invite sent to {email}",
        "next_step": "Contractor will receive email with link to contractor.nexabuilder.com"
    }


# ── 5. Outreach stats / queue ──────────────────────────────────────────────────

@router.get("/stats")
async def outreach_stats(identity: dict = Depends(get_current_user)):
    """Summary stats for the outreach dashboard."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        r = await db.execute(text("""
            SELECT
                COUNT(DISTINCT CASE WHEN re.event_type = 'sms_sent'        THEN re.contractor_id END) as sms_sent,
                COUNT(DISTINCT CASE WHEN re.event_type = 'email_captured'  THEN re.contractor_id END) as emails_captured,
                COUNT(DISTINCT CASE WHEN re.event_type = 'portal_invite_sent' THEN re.contractor_id END) as invites_sent,
                COUNT(DISTINCT CASE WHEN re.event_type = 'bid_accepted'    THEN re.contractor_id END) as bids_accepted,
                COUNT(DISTINCT ca.id) as portal_accounts,
                COUNT(DISTINCT CASE WHEN ca.cslb_verified THEN ca.id END) as verified_accounts
            FROM routing_events re
            FULL OUTER JOIN contractor_accounts ca ON true
        """))
        stats = r.fetchone()

    return {
        "sms_sent":        stats[0] or 0,
        "emails_captured": stats[1] or 0,
        "invites_sent":    stats[2] or 0,
        "bids_accepted":   stats[3] or 0,
        "portal_accounts": stats[4] or 0,
        "verified_accounts": stats[5] or 0,
        "conversion_rate": f"{round((stats[3] or 0) / max(stats[0] or 1, 1) * 100, 1)}%",
    }


# ── 6. Outreach history for a contractor ──────────────────────────────────────

@router.get("/contractors/{contractor_id}/history")
async def contractor_outreach_history(
    contractor_id: int,
    identity: dict = Depends(get_current_user),
):
    """Full outreach history for a specific contractor."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        r = await db.execute(text("""
            SELECT re.event_type, re.payload, re.created_at,
                   l.project_type, l.vertical, l.postal_code
            FROM routing_events re
            LEFT JOIN leads l ON l.id = re.lead_id
            WHERE re.contractor_id = :cid
            ORDER BY re.created_at DESC
        """), {"cid": contractor_id})

        history = [{
            "event_type":  row[0],
            "payload":     row[1],
            "created_at":  row[2].isoformat() if row[2] else None,
            "project_type": row[3],
            "vertical":    row[4],
            "postal_code": row[5],
        } for row in r.fetchall()]

    return {"contractor_id": contractor_id, "events": history}
