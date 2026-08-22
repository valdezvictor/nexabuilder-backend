"""
capi_dispatcher.py — NexaBuilder Server-Side Conversion API
Fires enriched conversion events to Meta CAPI and Google Enhanced Conversions
when a lead submits or reaches a milestone status.

Usage:
    from app.capi_dispatcher import fire_lead_event
    bg.add_task(fire_lead_event, lead_id=lead.id, event_name="Lead", revenue=0)
"""
import os
import hashlib
import logging
import time as _time
from typing import Optional

import httpx

log = logging.getLogger("capi")

META_PIXEL_ID        = os.getenv("META_PIXEL_ID", "")
META_ACCESS_TOKEN    = os.getenv("META_ACCESS_TOKEN", "")
META_TEST_EVENT_CODE = os.getenv("META_TEST_EVENT_CODE", "")
GOOGLE_ADS_CONV_ID   = os.getenv("GOOGLE_ADS_CONVERSION_ID", "")
GOOGLE_ADS_CONV_LABEL= os.getenv("GOOGLE_ADS_CONVERSION_LABEL", "")
CAPI_ENABLED         = bool(META_PIXEL_ID and META_PIXEL_ID != "REPLACE_WITH_PIXEL_ID")


def _hash(value: Optional[str]) -> str:
    """SHA-256 hash of PII — required by Meta CAPI spec."""
    if not value:
        return ""
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg", "postgresql+psycopg2"),
        pool_pre_ping=True
    )
    return sessionmaker(bind=engine)()


def _fetch_lead_attribution(lead_id: int) -> dict:
    """Pull lead + attribution session data for CAPI payload construction."""
    db = _db()
    try:
        from sqlalchemy import text as sqlt
        row = db.execute(sqlt("""
            SELECT
                l.id, l.email, l.phone, l.name, l.city, l.state,
                l.vertical, l.job_amount, l.lead_status,
                s.session_id, s.nexa_cid, s.fbclid, s.gclid, s.ttclid,
                s.utm_source, s.utm_medium, s.utm_campaign,
                s.landing_page, s.ip_hash, s.user_agent
            FROM leads l
            LEFT JOIN attribution_sessions s ON s.lead_id = l.id
            WHERE l.id = :lid
            LIMIT 1
        """), {"lid": lead_id}).fetchone()
        return dict(row._mapping) if row else {}
    finally:
        db.close()


def _fire_meta_capi(lead: dict, event_name: str, revenue: float = 0.0) -> str:
    """Send event to Meta Conversions API (Graph API v19)."""
    if not CAPI_ENABLED:
        log.info(f"CAPI disabled (no pixel configured) — skipping Meta event {event_name}")
        return "disabled"

    payload = {
        "data": [{
            "event_name": event_name,
            "event_time": int(_time.time()),
            "event_source_url": f"https://www.nexabuilder.com{lead.get('landing_page','/')}",
            "action_source": "website",
            "user_data": {
                "em":  [_hash(lead.get("email"))],
                "ph":  [_hash(lead.get("phone"))],
                "fn":  [_hash((lead.get("name") or "").split()[0])],
                "ln":  [_hash(" ".join((lead.get("name") or "").split()[1:]))],
                "ct":  [_hash(lead.get("city"))],
                "st":  [_hash(lead.get("state"))],
                "fbc": lead.get("fbclid") or "",
                "external_id": [str(lead.get("id",""))],
                "client_ip_address": lead.get("ip_hash",""),
                "client_user_agent": lead.get("user_agent",""),
            },
            "custom_data": {
                "currency": "USD",
                "value": float(revenue or 0),
                "content_category": lead.get("vertical","home_improvement"),
                "content_name": f"Lead - {lead.get('vertical','').title()}",
            }
        }]
    }
    if META_TEST_EVENT_CODE:
        payload["test_event_code"] = META_TEST_EVENT_CODE

    url = f"https://graph.facebook.com/v19.0/{META_PIXEL_ID}/events"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, params={"access_token": META_ACCESS_TOKEN}, json=payload)
            resp.raise_for_status()
            result = resp.json()
            events_received = result.get("events_received", 0)
            log.info(f"Meta CAPI {event_name} lead={lead_id} events_received={events_received}")
            return f"ok:{events_received}"
    except httpx.HTTPStatusError as e:
        log.error(f"Meta CAPI HTTP error {e.response.status_code}: {e.response.text[:200]}")
        return f"error:{e.response.status_code}"
    except Exception as e:
        log.error(f"Meta CAPI exception: {e}")
        return f"error:{e}"


def _fire_google_conversion(lead: dict, revenue: float = 0.0) -> str:
    """Send offline conversion to Google Ads via Measurement Protocol."""
    if not GOOGLE_ADS_CONV_ID:
        return "disabled"

    # Google Enhanced Conversions — requires gclid
    gclid = lead.get("gclid")
    if not gclid:
        log.info(f"No gclid for lead {lead.get('id')} — skipping Google conversion")
        return "no_gclid"

    payload = {
        "client_id": lead.get("nexa_cid", ""),
        "events": [{
            "name": "conversion",
            "params": {
                "send_to": f"{GOOGLE_ADS_CONV_ID}/{GOOGLE_ADS_CONV_LABEL}",
                "value": float(revenue or 0),
                "currency": "USD",
                "transaction_id": str(lead.get("id","")),
                "gclid": gclid,
            }
        }],
        "user_data": {
            "email": _hash(lead.get("email")),
            "phone": _hash(lead.get("phone")),
        }
    }

    url = f"https://www.google-analytics.com/mp/collect"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload)
            log.info(f"Google conversion lead={lead.get('id')} status={resp.status_code}")
            return f"ok:{resp.status_code}"
    except Exception as e:
        log.error(f"Google conversion exception: {e}")
        return f"error:{e}"


def fire_lead_event(lead_id: int, event_name: str = "Lead", revenue: float = 0.0):
    """
    Main entry point — call via BackgroundTasks.
    Fires Meta CAPI + Google Enhanced Conversions for a given lead.

    Args:
        lead_id:    Primary key from leads table
        event_name: Meta event name (Lead, CompleteRegistration, Purchase)
        revenue:    Optional revenue value in USD
    """
    if not lead_id:
        return

    lead = _fetch_lead_attribution(lead_id)
    if not lead:
        log.warning(f"CAPI: lead {lead_id} not found")
        return

    # Fire both platforms — log results, never raise (background task)
    meta_result   = _fire_meta_capi(lead, event_name, revenue)
    google_result = _fire_google_conversion(lead, revenue)

    # Log attribution event for internal audit
    try:
        db = _db()
        from sqlalchemy import text as sqlt
        db.execute(sqlt("""
            INSERT INTO attribution_events
            (session_id, lead_id, tenant_id, event_type, event_data, revenue, vertical, page_path)
            VALUES (
                (SELECT session_id FROM attribution_sessions WHERE lead_id=:lid LIMIT 1),
                :lid, 'nexabuilder', :etype,
                :edata::jsonb, :rev, :vert, :path
            )
        """), {
            "lid":   lead_id,
            "etype": event_name,
        "edata": '{"meta":"' + str(meta_result) + '","google":"' + str(google_result) + '"}',
            "rev":   revenue,
            "vert":  lead.get("vertical",""),
            "path":  lead.get("landing_page",""),
        })
        db.commit()
        db.close()
    except Exception as e:
        log.error(f"CAPI audit log failed: {e}")

    log.info(f"CAPI fired: lead={lead_id} event={event_name} meta={meta_result} google={google_result}")
