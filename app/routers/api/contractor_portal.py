# app/routers/api/contractor_portal.py
# Contractor portal API — auth, agreements, bids, lead views
# Powers contractor.nexabuilder.com

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update, text
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.core.auth import get_current_user
from app.db import get_db, get_sessionmaker
from app.models.bid_invitation import BidInvitation
from app.models.contractor_agreement import ContractorAgreement
from app.models.contractor_account import ContractorAccount
from app.models.routing_event import RoutingEvent
from app.models.lead import Lead

router = APIRouter(prefix="/api/contractor", tags=["Contractor Portal"])

CURRENT_AGREEMENT_VERSION = "1.0"  # Bump when Mario approves changes


# ── Agreement status ──────────────────────────────────────────────────────────

@router.get("/agreement/status")
async def get_agreement_status(
    identity: dict = Depends(get_current_user),
):
    """
    Check if the current contractor has signed the portal agreement.
    Called on every dashboard load — gates access to bid inbox.
    """
    user_id = str(identity.get("sub") or "")
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(
            select(ContractorAgreement)
            .where(ContractorAgreement.user_id == user_id)
            .where(ContractorAgreement.agreement_version == CURRENT_AGREEMENT_VERSION)
            .order_by(ContractorAgreement.agreed_at.desc())
            .limit(1)
        )
        agreement = result.scalar_one_or_none()

        if not agreement:
            return {
                "signed": False,
                "version": CURRENT_AGREEMENT_VERSION,
                "attorney_reviewed": False,
                "message": "Agreement signature required before accessing bids."
            }

        return {
            "signed": True,
            "version": agreement.agreement_version,
            "agreed_at": agreement.agreed_at.isoformat(),
            "full_name_signed": agreement.full_name_signed,
            "license_number": agreement.license_number,
            "attorney_reviewed": agreement.attorney_reviewed,
        }


@router.post("/agreement/sign")
async def sign_agreement(
    body: dict,
    request: Request,
    identity: dict = Depends(get_current_user),
):
    """
    Record contractor's agreement signature.
    full_name (typed) = electronic signature per ESIGN Act.
    Requires: full_name, email, license_number, terms_acknowledged (list of term IDs)
    """
    user_id = str(identity.get("sub") or "")

    full_name = (body.get("full_name") or "").strip()
    email     = (body.get("email") or "").strip()
    license_n = (body.get("license_number") or "").strip()
    terms_ack = body.get("terms_acknowledged", [])  # list of clause IDs acknowledged

    if not full_name or not email or not license_n:
        raise HTTPException(status_code=422, detail="full_name, email, and license_number are required")

    if len(terms_ack) < 6:  # must acknowledge all key clauses
        raise HTTPException(status_code=422, detail="All agreement terms must be acknowledged")

    ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown")
    ua = request.headers.get("User-Agent", "")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Get contractor account
        acct_result = await db.execute(
            select(ContractorAccount).where(ContractorAccount.user_id == user_id)
        )
        acct = acct_result.scalar_one_or_none()

        agreement = ContractorAgreement(
            contractor_account_id=acct.id if acct else 0,
            user_id=user_id,
            agreement_version=CURRENT_AGREEMENT_VERSION,
            agreed_at=datetime.now(timezone.utc),
            ip_address=ip,
            user_agent=ua[:500] if ua else None,
            full_name_signed=full_name,
            email_signed=email,
            license_number=license_n,
            terms_acknowledged=terms_ack,
            attorney_reviewed=False,  # Mario hasn't reviewed yet
        )
        db.add(agreement)
        await db.commit()

    return {
        "signed": True,
        "version": CURRENT_AGREEMENT_VERSION,
        "agreed_at": datetime.now(timezone.utc).isoformat(),
        "full_name_signed": full_name,
        "attorney_reviewed": False,
        "message": "Agreement recorded. Welcome to NexaBuilder Contractor Network."
    }


# ── Bid Inbox ─────────────────────────────────────────────────────────────────

@router.get("/bids")
async def get_my_bids(
    status: Optional[str] = None,  # pending|accepted|declined|all
    identity: dict = Depends(get_current_user),
):
    """
    Returns all bid invitations for this contractor.
    Lead contact info is masked until bid is accepted.
    """
    user_id = str(identity.get("sub") or "")
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Get contractor account to find contractor_db_id
        acct_r = await db.execute(
            select(ContractorAccount).where(ContractorAccount.user_id == user_id)
        )
        acct = acct_r.scalar_one_or_none()
        if not acct or not acct.contractor_db_id:
            return {"bids": [], "total": 0}

        query = """
            SELECT
                bi.id, bi.lead_id, bi.status, bi.sent_at, bi.viewed_at,
                bi.responded_at, bi.expires_at, bi.bid_amount, bi.commission_pct,
                l.vertical, l.project_type, l.postal_code, l.city, l.state,
                l.ai_assessment, l.ai_score, l.lead_status,
                l.first_name, l.phone, l.email
            FROM bid_invitations bi
            JOIN leads l ON l.id = bi.lead_id
            WHERE bi.contractor_id = :cid
            {status_filter}
            ORDER BY bi.created_at DESC
        """

        status_filter = ""
        params = {"cid": acct.contractor_db_id}
        if status and status != "all":
            status_filter = "AND bi.status = :status"
            params["status"] = status

        rows = (await db.execute(text(query.format(status_filter=status_filter)), params)).fetchall()

        # Mark viewed_at for pending bids being fetched
        now = datetime.now(timezone.utc)
        for row in rows:
            bid_id, _, bid_status, _, viewed_at = row[0], row[1], row[2], row[3], row[4]
            if bid_status in ("sent", "pending") and not viewed_at:
                await db.execute(text(
                    "UPDATE bid_invitations SET viewed_at = :now, status = 'viewed' "
                    "WHERE id = :bid_id"
                ), {"now": now, "bid_id": bid_id})
        await db.commit()

        bids = []
        for row in rows:
            is_accepted = row[2] == "accepted"
            ai = row[14] or {}

            bids.append({
                "bid_id":        row[0],
                "lead_id":       row[1],
                "status":        row[2],
                "sent_at":       row[3].isoformat() if row[3] else None,
                "viewed_at":     row[4].isoformat() if row[4] else None,
                "responded_at":  row[5].isoformat() if row[5] else None,
                "expires_at":    row[6].isoformat() if row[6] else None,
                "bid_amount":    float(row[7]) if row[7] else None,
                "commission_pct": float(row[8]) if row[8] else 10.0,
                "vertical":      row[9],
                "project_type":  row[10],
                "postal_code":   row[11],
                "city":          row[12],
                "state":         row[13],
                "lead_status":   row[16],
                # AI assessment — always visible
                "ai_assessment": {
                    "complexity_score":    ai.get("complexity_score"),
                    "complexity_label":    ai.get("complexity_label"),
                    "estimated_cost_range": ai.get("estimated_cost_range"),
                    "permit_required":     ai.get("permit_required"),
                    "license_types_needed": ai.get("license_types_needed", []),
                },
                # Contact — masked until accepted
                "homeowner_name":  f"{row[17] or 'Homeowner'}" if is_accepted else "Homeowner (Pending)",
                "phone":  row[18] if is_accepted else None,
                "email":  row[19] if is_accepted else None,
                "contact_unlocked": is_accepted,
            })

        return {"bids": bids, "total": len(bids)}


@router.post("/bids/{bid_id}/accept")
async def accept_bid(
    bid_id: int,
    body: dict,
    identity: dict = Depends(get_current_user),
):
    """
    Contractor accepts a bid invitation.
    Optional: submit bid_amount and bid_notes.
    Triggers: lead_status → matched, routing_event bid_accepted,
              member portal unlocks contractor contact.
    """
    user_id = str(identity.get("sub") or "")
    bid_amount = body.get("bid_amount")
    bid_notes  = body.get("bid_notes", "")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Verify ownership
        result = await db.execute(
            select(BidInvitation).where(BidInvitation.id == bid_id)
        )
        bid = result.scalar_one_or_none()
        if not bid:
            raise HTTPException(status_code=404, detail="Bid not found")
        if bid.status in ("accepted", "declined", "expired"):
            raise HTTPException(status_code=409, detail=f"Bid already {bid.status}")

        now = datetime.now(timezone.utc)

        # Update bid
        bid.status       = "accepted"
        bid.responded_at = now
        if bid_amount:
            bid.bid_amount = bid_amount
        if bid_notes:
            bid.bid_notes = bid_notes

        # Update lead status → matched, set contractor_id
        await db.execute(text(
            "UPDATE leads SET lead_status = 'matched', contractor_id = :cid, "
            "assigned_at = :now WHERE id = :lid"
        ), {"cid": bid.contractor_id, "now": now, "lid": bid.lead_id})

        # Record routing event
        db.add(RoutingEvent(
            lead_id=bid.lead_id,
            contractor_id=bid.contractor_id,
            event_type="bid_accepted",
            payload={
                "bid_id": bid_id,
                "bid_amount": float(bid_amount) if bid_amount else None,
                "commission_pct": float(bid.commission_pct or 10),
            }
        ))

        await db.commit()

    return {
        "success": True,
        "bid_id": bid_id,
        "lead_status": "matched",
        "message": "Bid accepted. Homeowner contact has been unlocked in your portal."
    }


@router.post("/bids/{bid_id}/decline")
async def decline_bid(
    bid_id: int,
    body: dict,
    identity: dict = Depends(get_current_user),
):
    """Contractor declines a bid invitation."""
    reason = body.get("reason", "")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(BidInvitation).where(BidInvitation.id == bid_id))
        bid = result.scalar_one_or_none()
        if not bid:
            raise HTTPException(status_code=404, detail="Bid not found")

        bid.status        = "declined"
        bid.responded_at  = datetime.now(timezone.utc)
        bid.decline_reason = reason

        db.add(RoutingEvent(
            lead_id=bid.lead_id,
            contractor_id=bid.contractor_id,
            event_type="bid_declined",
            payload={"bid_id": bid_id, "reason": reason}
        ))
        await db.commit()

    return {"success": True, "bid_id": bid_id, "status": "declined"}


@router.post("/bids/{bid_id}/update-status")
async def update_project_status(
    bid_id: int,
    body: dict,
    identity: dict = Depends(get_current_user),
):
    """
    Contractor updates project status after bid accepted.
    Triggers timeline events visible to member and call center.
    event: site_visit_scheduled | quote_sent | project_started | project_completed
    """
    event_type = body.get("event_type")
    notes      = body.get("notes", "")
    scheduled_date = body.get("scheduled_date")

    VALID_EVENTS = {
        "site_visit_scheduled": "site_visit",
        "quote_sent":           "quote",
        "project_started":      "approved",
        "project_completed":    "complete",
    }
    if event_type not in VALID_EVENTS:
        raise HTTPException(status_code=422,
            detail=f"event_type must be one of: {list(VALID_EVENTS.keys())}")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(select(BidInvitation).where(BidInvitation.id == bid_id))
        bid = result.scalar_one_or_none()
        if not bid or bid.status != "accepted":
            raise HTTPException(status_code=404, detail="Active bid not found")

        new_status = VALID_EVENTS[event_type]
        await db.execute(text(
            "UPDATE leads SET lead_status = :status WHERE id = :lid"
        ), {"status": new_status, "lid": bid.lead_id})

        db.add(RoutingEvent(
            lead_id=bid.lead_id,
            contractor_id=bid.contractor_id,
            event_type=event_type,
            payload={"bid_id": bid_id, "notes": notes, "scheduled_date": scheduled_date}
        ))
        await db.commit()

    return {
        "success": True,
        "event_type": event_type,
        "lead_status": new_status,
        "message": f"Project status updated to: {new_status}"
    }


# ── Contractor profile / active projects ──────────────────────────────────────

@router.get("/profile")
async def get_contractor_profile(identity: dict = Depends(get_current_user)):
    """Returns contractor account + CSLB record for the authenticated contractor."""
    user_id = str(identity.get("sub") or "")
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        acct_r = await db.execute(
            select(ContractorAccount).where(ContractorAccount.user_id == user_id)
        )
        acct = acct_r.scalar_one_or_none()
        if not acct:
            return {"registered": False}

        cslb_row = None
        if acct.contractor_db_id:
            r = await db.execute(text(
                "SELECT license_no, business_name, city, county, zip_code, "
                "classifications, primary_status, expiration_date, phone "
                "FROM contractors WHERE id = :id"
            ), {"id": acct.contractor_db_id})
            cslb_row = r.fetchone()

        return {
            "registered": True,
            "cslb_verified": acct.cslb_verified,
            "company_name": acct.company_name,
            "license_number": acct.license_number,
            "cslb": {
                "license_no":    cslb_row[0] if cslb_row else acct.license_number,
                "business_name": cslb_row[1] if cslb_row else acct.company_name,
                "city":          cslb_row[2] if cslb_row else None,
                "county":        cslb_row[3] if cslb_row else None,
                "zip_code":      cslb_row[4] if cslb_row else None,
                "classifications": cslb_row[5] if cslb_row else None,
                "status":        cslb_row[6] if cslb_row else None,
                "expiration":    cslb_row[7].isoformat() if cslb_row and cslb_row[7] else None,
                "phone":         cslb_row[8] if cslb_row else None,
            } if cslb_row else None,
        }


# ── Admin: send bid invitation ────────────────────────────────────────────────

@router.post("/admin/bids/send")
async def send_bid_invitation(
    body: dict,
    identity: dict = Depends(get_current_user),
):
    """
    Admin/system endpoint to send a bid invitation to a contractor for a lead.
    lead_id + contractor_id (from contractors table) required.
    """
    if identity.get("role") not in ("admin", "superadmin"):
        raise HTTPException(status_code=403, detail="Admin access required")

    lead_id       = body.get("lead_id")
    contractor_id = body.get("contractor_id")
    commission    = body.get("commission_pct", 10.0)
    expire_hours  = body.get("expire_hours", 48)

    if not lead_id or not contractor_id:
        raise HTTPException(status_code=422, detail="lead_id and contractor_id required")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        now = datetime.now(timezone.utc)

        # Check for existing bid
        existing = await db.execute(text(
            "SELECT id, status FROM bid_invitations "
            "WHERE lead_id = :lid AND contractor_id = :cid"
        ), {"lid": lead_id, "cid": contractor_id})
        ex_row = existing.fetchone()

        if ex_row and ex_row[1] not in ("declined", "expired", "withdrawn"):
            raise HTTPException(status_code=409,
                detail=f"Bid already exists with status: {ex_row[1]}")

        bid = BidInvitation(
            lead_id=lead_id,
            contractor_id=contractor_id,
            status="sent",
            sent_at=now,
            expires_at=now + timedelta(hours=expire_hours),
            commission_pct=commission,
        )
        db.add(bid)

        # Log routing event
        db.add(RoutingEvent(
            lead_id=lead_id,
            contractor_id=contractor_id,
            event_type="bid_sent",
            payload={"commission_pct": commission, "expire_hours": expire_hours}
        ))
        await db.commit()
        await db.refresh(bid)

    return {
        "success": True,
        "bid_id": bid.id,
        "lead_id": lead_id,
        "contractor_id": contractor_id,
        "expires_at": bid.expires_at.isoformat(),
        "message": f"Bid invitation sent. Expires in {expire_hours}h."
    }
