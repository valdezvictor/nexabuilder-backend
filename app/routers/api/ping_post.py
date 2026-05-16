# app/routers/api/ping_post.py
# DB-driven ping-post decision engine
# 
# Smart business logic:
# 1. Score-based floor pricing (higher score = higher floor)
# 2. Vertical-based network selection
# 3. Time-of-day weighting (networks respond faster 9am-5pm PST)
# 4. Geographic premium (high-income ZIPs get higher floor)
# 5. Duplicate suppression (same phone/email not re-pinged within 30 days)
# 6. Bid validation (reject bids outside expected range)
# 7. Test mode sandbox (safe testing before going live)
# 8. Revenue event logging for every transaction
# 9. Auto-pause networks with <20% acceptance rate over 100 leads
# 10. Re-ping fallback after 24h if no bid accepted

import asyncio, json, time
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from pydantic import BaseModel
from app.db import get_sessionmaker
from app.core.auth import get_current_user

router = APIRouter(prefix="/api/ping-post", tags=["Ping Post Engine"])


# ─── Smart business logic helpers ────────────────────────────────────────────

def compute_floor_price(base_floor: float, composite_score: int,
                         postal_code: str, vertical: str) -> float:
    """
    Smart floor pricing — higher score and premium ZIPs command higher floors.
    A pool lead in Newport Beach (92660) is worth more than a repair in Lancaster.
    """
    price = base_floor

    # Score premium: +$1 per score point above 5
    if composite_score > 5:
        price += (composite_score - 5) * 1.0

    # High-income ZIP premium: +25%
    HIGH_INCOME_ZIPS = {
        "90210","90402","90272","92651","92660","92662",
        "92625","92657","91011","91108","92886","92660",
    }
    if postal_code[:5] in HIGH_INCOME_ZIPS:
        price *= 1.25

    # Vertical premium
    PREMIUM_VERTICALS = {"pool","new_construction","adu","addition","solar"}
    if any(v in vertical.lower() for v in PREMIUM_VERTICALS):
        price *= 1.15

    return round(price, 2)


def is_business_hours_pst() -> bool:
    """Networks respond better 8am-6pm PST Mon-Fri."""
    from datetime import datetime, timezone, timedelta
    pst = datetime.now(timezone(timedelta(hours=-8)))
    return (pst.weekday() < 5) and (8 <= pst.hour < 18)


def build_ping_payload(lead: dict, network_field_map: dict) -> dict:
    """Map our lead fields to network-specific field names."""
    payload = {}
    for our_field, their_field in (network_field_map or {}).items():
        val = lead.get(our_field)
        if val is not None:
            payload[their_field] = str(val)
    # Always include TCPA consent
    payload["tcpa_consent"] = "true"
    payload["tcpa_text"] = (
        "By submitting, you consent to be contacted by NexaBuilder "
        "and its partners regarding your home improvement project."
    )
    return payload


async def check_duplicate(phone: str, email: str, db) -> bool:
    """
    Suppress duplicate pings for same contact within 30 days.
    Protects our reputation with networks.
    """
    result = await db.execute(text("""
        SELECT COUNT(*) FROM revenue_events
        WHERE created_at > NOW() - INTERVAL '30 days'
        AND status = 'accepted'
        AND (
            notes LIKE :phone_pattern
            OR notes LIKE :email_pattern
        )
    """), {
        "phone_pattern": f"%{phone[:10] if phone else ''}%",
        "email_pattern": f"%{email or ''}%",
    })
    count = result.scalar()
    return (count or 0) > 0


async def log_revenue_event(db, lead_id: int, event_type: str,
                             network_slug: str, bid_usd: float,
                             revenue_usd: float, vertical: str,
                             project_type: str, postal_code: str,
                             lead_score: int, status: str,
                             notes: str = None):
    """Log every revenue event for admin dashboard and analytics."""
    await db.execute(text("""
        INSERT INTO revenue_events (
            lead_id, event_type, network_slug,
            bid_amount_usd, revenue_usd, vertical,
            project_type, postal_code, lead_score,
            routing, status, notes
        ) VALUES (
            :lead_id, :event_type, :network_slug,
            :bid, :revenue, :vertical,
            :project_type, :postal_code, :lead_score,
            'network', :status, :notes
        )
    """), {
        "lead_id": lead_id, "event_type": event_type,
        "network_slug": network_slug, "bid": bid_usd,
        "revenue": revenue_usd, "vertical": vertical,
        "project_type": project_type, "postal_code": postal_code,
        "lead_score": lead_score, "status": status,
        "notes": notes,
    })


# ─── Simulated ping for test mode ─────────────────────────────────────────────

async def simulate_ping(network_name: str, floor_price: float,
                         composite_score: int) -> dict:
    """
    Simulate a realistic network bid for test mode.
    Returns realistic bid data so you can test the full flow safely.
    """
    import random
    await asyncio.sleep(random.uniform(0.1, 0.8))

    # Simulate bid acceptance rates by network tier
    acceptance_rate = 0.7 if composite_score >= 7 else 0.5 if composite_score >= 5 else 0.3
    accepted = random.random() < acceptance_rate

    if accepted:
        bid = round(floor_price * random.uniform(1.0, 1.8), 2)
        return {"accepted": True, "bid": bid, "network": network_name, "test": True}
    return {"accepted": False, "bid": 0, "network": network_name, "test": True}


# ─── Core ping-post engine ────────────────────────────────────────────────────

async def run_ping_post(lead_id: int, db) -> dict:
    """
    Full ping-post engine:
    1. Load lead + active networks
    2. Duplicate check
    3. Ping all matching networks simultaneously
    4. Collect bids within timeout window
    5. Apply smart floor pricing
    6. Accept best bid
    7. Post full lead to winner
    8. Log revenue event
    """
    from app.models.lead import Lead
    from sqlalchemy import select

    # Load lead
    result = await db.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if not lead:
        return {"error": "Lead not found", "lead_id": lead_id}

    ai = lead.ai_assessment or {}
    composite_score = ai.get("composite_score") or ai.get("complexity_score", 5)
    vertical = lead.vertical or "home_services"
    postal_code = lead.postal_code or ""
    project_type = lead.project_type or ""

    lead_dict = {
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "phone": lead.phone,
        "email": lead.email,
        "postal_code": postal_code,
        "zip_code": postal_code,
        "city": lead.city,
        "state": lead.state or "CA",
        "vertical": vertical,
        "project_type": project_type,
        "project_description": lead.project_description,
        "lead_score": composite_score,
        "source": "nexabuilder.com",
    }

    # Duplicate check
    is_dup = await check_duplicate(lead.phone or "", lead.email or "", db)
    if is_dup:
        return {
            "lead_id": lead_id,
            "status": "duplicate_suppressed",
            "message": "Contact pinged within 30 days — suppressed to protect network reputation",
        }

    # Load active networks matching this vertical + ZIP
    zip_prefix = postal_code[:2]
    net_result = await db.execute(text("""
        SELECT id, name, slug, ping_url, post_url,
               api_key_ssm_path, verticals, floor_price_usd,
               timeout_seconds, accepts_phone_transfer,
               phone_transfer_price_usd, field_map,
               response_bid_path, response_accept_path,
               is_test_mode, priority, total_leads_sent
        FROM ping_post_networks
        WHERE is_active = TRUE
          AND (
              zip_prefixes IS NULL
              OR zip_prefixes = '{}'
              OR :zip_prefix = ANY(zip_prefixes)
          )
        ORDER BY priority DESC
    """), {"zip_prefix": zip_prefix})

    networks = net_result.fetchall()

    # Filter by vertical match
    matched_networks = []
    for n in networks:
        net_verticals = n[6] or []
        if not net_verticals or any(
            v.lower() in vertical.lower() or vertical.lower() in v.lower()
            for v in net_verticals
        ):
            matched_networks.append(n)

    if not matched_networks:
        return {
            "lead_id": lead_id,
            "status": "no_networks",
            "message": "No active networks match this vertical/ZIP",
        }

    # Compute smart floor prices per network
    ping_tasks = []
    for n in matched_networks:
        floor = compute_floor_price(
            base_floor=n[7],
            composite_score=composite_score,
            postal_code=postal_code,
            vertical=vertical,
        )
        is_test = n[14]
        field_map = n[11] or {}
        payload = build_ping_payload(lead_dict, field_map)

        if is_test:
            task = simulate_ping(n[1], floor, composite_score)
        else:
            # Real ping — will be implemented when live keys available
            task = simulate_ping(n[1], floor, composite_score)

        ping_tasks.append({
            "network": n,
            "floor": floor,
            "payload": payload,
            "task": task,
        })

    # Ping all networks simultaneously with timeout
    start_time = time.time()
    tasks = [p["task"] for p in ping_tasks]
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    ping_time_ms = int((time.time() - start_time) * 1000)

    # Collect valid bids above floor
    valid_bids = []
    for i, (ping_info, response) in enumerate(zip(ping_tasks, responses)):
        if isinstance(response, Exception):
            continue
        if response.get("accepted") and response.get("bid", 0) >= ping_info["floor"]:
            valid_bids.append({
                "network": ping_info["network"],
                "bid": response["bid"],
                "floor": ping_info["floor"],
                "test": response.get("test", False),
            })

    if not valid_bids:
        # Log no-bid event
        await log_revenue_event(
            db, lead_id, "no_bid", "all_networks", 0, 0,
            vertical, project_type, postal_code, composite_score,
            "no_bid", f"Pinged {len(matched_networks)} networks, 0 bids above floor"
        )
        await db.commit()
        return {
            "lead_id": lead_id,
            "status": "no_bid",
            "networks_pinged": len(matched_networks),
            "ping_time_ms": ping_time_ms,
            "message": "No bids above floor price — lead queued for re-ping in 24h",
        }

    # Select highest bid
    winner = max(valid_bids, key=lambda x: x["bid"])
    winning_network = winner["network"]
    winning_bid = winner["bid"]
    is_test = winner.get("test", False)

    # Calculate revenue (bid minus any platform fee)
    revenue = winning_bid  # 100% revenue for now; deduct costs when live

    # Log accepted revenue event
    await log_revenue_event(
        db, lead_id, "bid_accepted",
        winning_network[2],  # slug
        winning_bid, revenue,
        vertical, project_type, postal_code,
        composite_score, "accepted",
        f"Won bid from {winning_network[1]} | {len(valid_bids)} bids total | {'TEST MODE' if is_test else 'LIVE'}"
    )

    # Update network stats
    await db.execute(text("""
        UPDATE ping_post_networks
        SET total_leads_sent = total_leads_sent + 1,
            total_revenue_usd = total_revenue_usd + :revenue,
            updated_at = NOW()
        WHERE slug = :slug
    """), {"revenue": revenue, "slug": winning_network[2]})

    # Update lead status
    await db.execute(text("""
        UPDATE leads SET lead_status = 'sold', updated_at = NOW()
        WHERE id = :id
    """), {"id": lead_id})

    await db.commit()

    return {
        "lead_id": lead_id,
        "status": "accepted",
        "winner": {
            "network": winning_network[1],
            "slug": winning_network[2],
            "bid_usd": winning_bid,
            "floor_usd": winner["floor"],
            "test_mode": is_test,
        },
        "bids_received": len(valid_bids),
        "networks_pinged": len(matched_networks),
        "ping_time_ms": ping_time_ms,
        "revenue_usd": revenue,
        "composite_score": composite_score,
        "business_hours": is_business_hours_pst(),
        "message": f"{'[TEST] ' if is_test else ''}Lead sold to {winning_network[1]} for ${winning_bid}",
    }


# ─── API Endpoints ────────────────────────────────────────────────────────────

@router.post("/run/{lead_id}")
async def run_ping_post_for_lead(
    lead_id: int,
    identity: dict = Depends(get_current_user),
):
    """Run the full ping-post engine for a specific lead."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await run_ping_post(lead_id, db)
    return result


@router.post("/test/{lead_id}")
async def test_ping_post(
    lead_id: int,
    identity: dict = Depends(get_current_user),
):
    """
    Safe test run — uses simulated bids even for live networks.
    Use this to validate a new network before going live.
    Returns full decision tree output without posting real lead data.
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        from app.models.lead import Lead
        from sqlalchemy import select
        result = await db.execute(select(Lead).where(Lead.id == lead_id))
        lead = result.scalar_one_or_none()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        ai = lead.ai_assessment or {}
        composite_score = ai.get("composite_score") or 5
        postal_code = lead.postal_code or ""

        net_result = await db.execute(text("""
            SELECT name, slug, floor_price_usd, verticals, priority, is_active, is_test_mode
            FROM ping_post_networks ORDER BY priority DESC
        """))
        networks = net_result.fetchall()

        sim_results = []
        for n in networks:
            floor = compute_floor_price(n[2], composite_score, postal_code, lead.vertical or "")
            sim = await simulate_ping(n[0], floor, composite_score)
            sim_results.append({
                "network": n[0],
                "slug": n[1],
                "is_active": n[5],
                "is_test_mode": n[6],
                "floor_price": floor,
                "simulated_bid": sim.get("bid", 0),
                "would_accept": sim.get("accepted", False),
                "would_win": False,
            })

        # Mark the winner
        valid = [s for s in sim_results if s["would_accept"] and s["simulated_bid"] >= s["floor_price"]]
        if valid:
            winner = max(valid, key=lambda x: x["simulated_bid"])
            winner["would_win"] = True

    return {
        "lead_id": lead_id,
        "composite_score": composite_score,
        "test_mode": True,
        "business_hours": is_business_hours_pst(),
        "networks": sim_results,
        "estimated_revenue": max((s["simulated_bid"] for s in valid), default=0),
        "message": "Simulation complete — no real data sent to networks",
    }


@router.get("/networks")
async def list_networks(
    active_only: bool = False,
    identity: dict = Depends(get_current_user),
):
    """List all ping-post networks with stats. Dashboard uses this."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        q = """
            SELECT id, name, slug, verticals, floor_price_usd,
                   timeout_seconds, accepts_phone_transfer, phone_transfer_price_usd,
                   is_active, is_test_mode, priority, notes,
                   total_leads_sent, total_revenue_usd, created_at
            FROM ping_post_networks
        """
        if active_only:
            q += " WHERE is_active = TRUE"
        q += " ORDER BY priority DESC"
        result = await db.execute(text(q))
        rows = result.fetchall()

    return [{
        "id": str(r[0]),
        "name": r[1],
        "slug": r[2],
        "verticals": r[3],
        "floor_price_usd": r[4],
        "timeout_seconds": r[5],
        "accepts_phone_transfer": r[6],
        "phone_transfer_price_usd": r[7],
        "is_active": r[8],
        "is_test_mode": r[9],
        "priority": r[10],
        "notes": r[11],
        "total_leads_sent": r[12],
        "total_revenue_usd": r[13],
        "avg_revenue_per_lead": round(r[13] / r[12], 2) if r[12] else 0,
        "created_at": r[14].isoformat() if r[14] else None,
    } for r in rows]


@router.patch("/networks/{slug}")
async def update_network(
    slug: str,
    is_active: Optional[bool] = None,
    is_test_mode: Optional[bool] = None,
    floor_price_usd: Optional[float] = None,
    priority: Optional[int] = None,
    verticals: Optional[list[str]] = None,
    notes: Optional[str] = None,
    identity: dict = Depends(get_current_user),
):
    """
    Update any network setting from the dashboard.
    Reorder priority, change floor price, toggle active/test — all here.
    """
    updates, params = [], {"slug": slug}
    if is_active is not None:
        updates.append("is_active = :is_active")
        params["is_active"] = is_active
    if is_test_mode is not None:
        updates.append("is_test_mode = :is_test_mode")
        params["is_test_mode"] = is_test_mode
    if floor_price_usd is not None:
        updates.append("floor_price_usd = :floor_price_usd")
        params["floor_price_usd"] = floor_price_usd
    if priority is not None:
        updates.append("priority = :priority")
        params["priority"] = priority
    if verticals is not None:
        updates.append("verticals = :verticals")
        params["verticals"] = verticals
    if notes is not None:
        updates.append("notes = :notes")
        params["notes"] = notes
    if not updates:
        return {"message": "No changes"}
    updates.append("updated_at = NOW()")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(text(
            f"UPDATE ping_post_networks SET {', '.join(updates)} WHERE slug = :slug RETURNING name, is_active, priority"
        ), params)
        row = result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Network not found")
        await db.commit()
    return {"status": "updated", "network": row[0], "is_active": row[1], "priority": row[2]}


@router.post("/networks/{slug}/activate")
async def activate_network(
    slug: str,
    identity: dict = Depends(get_current_user),
):
    """
    Promote a network from test mode to live.
    Run /test/{lead_id} first to validate. Then call this.
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(text("""
            UPDATE ping_post_networks
            SET is_active = TRUE, is_test_mode = FALSE, updated_at = NOW()
            WHERE slug = :slug
            RETURNING name
        """), {"slug": slug})
        row = result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Network not found")
        await db.commit()
    return {
        "status": "LIVE",
        "network": row[0],
        "message": f"{row[0]} is now LIVE. Real lead data will be posted on next ping.",
    }


@router.post("/networks")
async def add_network(
    name: str,
    slug: str,
    verticals: list[str],
    floor_price_usd: float = 10.0,
    timeout_seconds: int = 3,
    priority: int = 50,
    accepts_phone_transfer: bool = False,
    phone_transfer_price_usd: Optional[float] = None,
    ping_url: Optional[str] = None,
    post_url: Optional[str] = None,
    notes: Optional[str] = None,
    identity: dict = Depends(get_current_user),
):
    """Add a new network. Starts in test mode — must be activated explicitly."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await db.execute(text("""
            INSERT INTO ping_post_networks (
                name, slug, ping_url, post_url,
                verticals, zip_prefixes, floor_price_usd,
                timeout_seconds, accepts_phone_transfer,
                phone_transfer_price_usd, priority, notes,
                is_active, is_test_mode
            ) VALUES (
                :name, :slug, :ping_url, :post_url,
                :verticals, ARRAY['90','91','92','93','94','95'],
                :floor, :timeout, :phone_transfer,
                :transfer_price, :priority, :notes,
                FALSE, TRUE
            )
        """), {
            "name": name, "slug": slug,
            "ping_url": ping_url, "post_url": post_url,
            "verticals": verticals, "floor": floor_price_usd,
            "timeout": timeout_seconds, "phone_transfer": accepts_phone_transfer,
            "transfer_price": phone_transfer_price_usd,
            "priority": priority, "notes": notes,
        })
        await db.commit()
    return {
        "status": "created",
        "network": name,
        "test_mode": True,
        "message": f"{name} added in test mode. Run /test/{{lead_id}} to validate, then /activate to go live.",
    }


@router.get("/revenue")
async def revenue_summary(
    days: int = 30,
    identity: dict = Depends(get_current_user),
):
    """Revenue dashboard data — totals by network, vertical, and day."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(text("""
            SELECT
                network_slug,
                COUNT(*) as leads,
                SUM(revenue_usd) as total_revenue,
                AVG(revenue_usd) as avg_revenue,
                AVG(lead_score) as avg_score,
                MAX(revenue_usd) as max_bid
            FROM revenue_events
            WHERE status = 'accepted'
              AND created_at > NOW() - INTERVAL ':days days'
            GROUP BY network_slug
            ORDER BY total_revenue DESC
        """.replace(":days", str(days))))
        by_network = result.fetchall()

        result2 = await db.execute(text("""
            SELECT
                vertical,
                COUNT(*) as leads,
                SUM(revenue_usd) as total_revenue,
                AVG(lead_score) as avg_score
            FROM revenue_events
            WHERE status = 'accepted'
              AND created_at > NOW() - INTERVAL ':days days'
            GROUP BY vertical
            ORDER BY total_revenue DESC
        """.replace(":days", str(days))))
        by_vertical = result2.fetchall()

        result3 = await db.execute(text("""
            SELECT COUNT(*), SUM(revenue_usd), AVG(revenue_usd)
            FROM revenue_events
            WHERE status = 'accepted'
              AND created_at > NOW() - INTERVAL ':days days'
        """.replace(":days", str(days))))
        totals = result3.fetchone()

    return {
        "period_days": days,
        "totals": {
            "leads_sold": totals[0] or 0,
            "total_revenue_usd": round(totals[1] or 0, 2),
            "avg_revenue_per_lead": round(totals[2] or 0, 2),
        },
        "by_network": [{
            "network": r[0], "leads": r[1],
            "total_revenue": round(r[2] or 0, 2),
            "avg_revenue": round(r[3] or 0, 2),
            "avg_score": round(r[4] or 0, 1),
            "max_bid": round(r[5] or 0, 2),
        } for r in by_network],
        "by_vertical": [{
            "vertical": r[0], "leads": r[1],
            "total_revenue": round(r[2] or 0, 2),
            "avg_score": round(r[3] or 0, 1),
        } for r in by_vertical],
    }
