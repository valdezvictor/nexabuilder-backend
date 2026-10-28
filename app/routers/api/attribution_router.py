"""
attribution_router.py — Attribution API for NexaBuilder
Endpoints:
  GET  /api/attribution/conversions        — conversions view (utm→lead join)
  GET  /api/attribution/summary            — dashboard summary by source/vertical
  GET  /api/attribution/timeline/{lead_id} — full customer journey for one lead
  POST /api/attribution/event              — emit a pipeline event
  GET  /api/attribution/sessions           — raw session browser (admin debug)
"""
import os, logging
from typing import Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text as sqlt

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/attribution", tags=["Attribution"])

ADMIN_KEY = os.getenv("CMS_ADMIN_KEY", "")


def _req(key: str):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg", "postgresql+psycopg2"),
        pool_pre_ping=True
    )
    return sessionmaker(bind=engine)()


# ── 1. Conversions view — #17 ─────────────────────────────────────────────────

@router.get("/conversions")
async def get_conversions(
    days: int = 30,
    source: Optional[str] = None,
    vertical: Optional[str] = None,
    limit: int = 200,
    x_admin_key: str = Header(...)
):
    """
    The #17 conversion join — every lead with its attribution source.
    utm_visits → attribution_sessions → lead_intake → attribution_events.
    """
    _req(x_admin_key)
    db = _db()
    try:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        where = ["converted_at >= :since"]
        params = {"since": since, "limit": limit}
        if source:
            where.append("utm_source = :source"); params["source"] = source
        if vertical:
            where.append("vertical = :vertical"); params["vertical"] = vertical

        where_clause = " AND ".join(where)

        rows = db.execute(sqlt(f"""
            SELECT * FROM conversions
            WHERE {where_clause}
            ORDER BY converted_at DESC
            LIMIT :limit
        """), params).fetchall()

        # Summary stats
        stats = db.execute(sqlt(f"""
            SELECT
                COUNT(*)                                        AS total_leads,
                COUNT(CASE WHEN utm_source IS NOT NULL THEN 1 END) AS attributed,
                COUNT(CASE WHEN fbclid IS NOT NULL THEN 1 END)  AS from_meta,
                COUNT(CASE WHEN gclid IS NOT NULL THEN 1 END)   AS from_google,
                COUNT(DISTINCT utm_source)                       AS unique_sources,
                AVG(minutes_to_convert)::INT AS avg_minutes_to_convertnutes_to_convert
            FROM conversions
            WHERE {where_clause}
        """), params).fetchone()

        return {
            "period_days":  days,
            "stats":        dict(stats._mapping) if stats else {},
            "conversions":  [dict(r._mapping) for r in rows]
        }
    finally:
        db.close()


# ── 2. Summary by source/vertical ─────────────────────────────────────────────

@router.get("/summary")
async def get_summary(
    days: int = 30,
    group_by: str = "source",  # source | medium | campaign | vertical | day
    x_admin_key: str = Header(...)
):
    """Attribution summary for the Metrics dashboard widget (#18)."""
    _req(x_admin_key)
    db = _db()
    try:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        safe_group = {
            "source": "utm_source",
            "medium": "utm_medium",
            "campaign": "utm_campaign",
            "vertical": "vertical",
            "day": "TO_CHAR(DATE_TRUNC('day', converted_at), 'YYYY-MM-DD')"
        }.get(group_by, "utm_source")

        rows = db.execute(sqlt(f"""
            SELECT
                COALESCE({safe_group}, 'direct / none')        AS group_key,
                COUNT(*)                                        AS leads,
                COUNT(CASE WHEN ai_score >= 6 THEN 1 END)      AS qualified,
                AVG(ai_score)::NUMERIC(4,1)                    AS avg_ai_score,
                AVG(minutes_to_convert)::INT                   AS avg_minutes,
                COUNT(CASE WHEN fbclid IS NOT NULL THEN 1 END) AS from_meta,
                COUNT(CASE WHEN gclid  IS NOT NULL THEN 1 END) AS from_google,
                COUNT(CASE WHEN is_attributed THEN 1 END)      AS attributed
            FROM conversions
            WHERE converted_at >= :since
            GROUP BY 1
            ORDER BY leads DESC
            LIMIT 50
        """), {"since": since}).fetchall()

        return {
            "period_days": days,
            "group_by":    group_by,
            "rows":        [dict(r._mapping) for r in rows]
        }
    finally:
        db.close()


# ── 3. Full customer journey for one lead ─────────────────────────────────────

@router.get("/timeline/{lead_id}")
async def get_timeline(lead_id: int, x_admin_key: str = Header(...)):
    """Full multi-touch journey for a single lead — the Hyros pattern."""
    _req(x_admin_key)
    db = _db()
    try:
        # Lead details + attribution
        lead = db.execute(sqlt("""
            SELECT l.id, l.first_name, l.last_name, l.email, l.phone,
                   l.vertical, l.postal_code, l.created_at, l.ai_score, l.status,
                   l.nexa_cid, l.session_id,
                   l.utm_source, l.utm_medium, l.utm_campaign,
                   l.fbclid, l.gclid, l.ttclid, l.landing_page,
                   l.first_touch_at, l.minutes_to_convert
            FROM conversions l
            WHERE l.lead_id = :id
        """), {"id": lead_id}).fetchone()

        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        # Session touches
        session = None
        if lead._mapping.get("session_id"):
            s = db.execute(sqlt("""
                SELECT * FROM attribution_sessions WHERE session_id=:sid
            """), {"sid": lead._mapping["session_id"]}).fetchone()
            session = dict(s._mapping) if s else None

        # All events in timeline
        events = db.execute(sqlt("""
            SELECT event_id, event_type, event_data, revenue, vertical,
                   page_path, occurred_at
            FROM attribution_events
            WHERE lead_id=:id OR nexa_cid=:cid
            ORDER BY occurred_at ASC
        """), {"id": lead_id, "cid": lead._mapping.get("nexa_cid") or ""}).fetchall()

        return {
            "lead":    dict(lead._mapping),
            "session": session,
            "timeline": [dict(e._mapping) for e in events]
        }
    finally:
        db.close()


# ── 4. Emit pipeline event ────────────────────────────────────────────────────

class EventPayload(BaseModel):
    lead_id:    Optional[int]   = None
    nexa_cid:   Optional[str]   = None
    event_type: str
    event_data: Optional[dict]  = None
    revenue:    float           = 0.0
    vertical:   Optional[str]   = None
    page_path:  Optional[str]   = None


@router.post("/event")
async def emit_event(payload: EventPayload, x_admin_key: str = Header(...)):
    """
    Emit a funnel event to the unified timeline.
    Called by: lead intake hook, AI assessment, contractor match, etc.
    """
    _req(x_admin_key)
    import json as _json
    db = _db()
    try:
        # Resolve session_id from nexa_cid if provided
        session_id = None
        if payload.nexa_cid:
            row = db.execute(sqlt(
                "SELECT session_id FROM attribution_sessions WHERE nexa_cid=:cid"
            ), {"cid": payload.nexa_cid}).fetchone()
            if row:
                session_id = row[0]

        db.execute(sqlt("""
            INSERT INTO attribution_events
              (session_id, nexa_cid, lead_id, event_type,
               event_data, revenue, vertical, page_path)
            VALUES
              (:sid, :cid, :lid, :etype,
               :edata::jsonb, :rev, :vert, :path)
        """), {
            "sid":   session_id,
            "cid":   payload.nexa_cid,
            "lid":   payload.lead_id,
            "etype": payload.event_type,
            "edata": _json.dumps(payload.event_data or {}),
            "rev":   payload.revenue,
            "vert":  payload.vertical,
            "path":  payload.page_path
        })

        # Update session last_touch
        if session_id:
            db.execute(sqlt(
                "UPDATE attribution_sessions SET last_touch_at=NOW(), lead_id=:lid WHERE session_id=:sid"
            ), {"lid": payload.lead_id, "sid": session_id})

        db.commit()
        return {"status": "ok", "event_type": payload.event_type}
    finally:
        db.close()


# ── 5. Debug: raw sessions browser ────────────────────────────────────────────

@router.get("/sessions")
async def list_sessions(
    days: int = 7,
    has_lead: Optional[bool] = None,
    limit: int = 50,
    x_admin_key: str = Header(...)
):
    _req(x_admin_key)
    db = _db()
    try:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        where = ["first_touch_at >= :since"]
        if has_lead is True:  where.append("lead_id IS NOT NULL")
        if has_lead is False: where.append("lead_id IS NULL")
        rows = db.execute(sqlt(f"""
            SELECT nexa_cid, utm_source, utm_medium, utm_campaign,
                   device_type, touch_count, first_touch_at, last_touch_at,
                   lead_id,
                   CASE WHEN fbclid IS NOT NULL THEN 'Meta' END   AS meta_click,
                   CASE WHEN gclid  IS NOT NULL THEN 'Google' END AS google_click,
                   CASE WHEN ttclid IS NOT NULL THEN 'TikTok' END AS tiktok_click
            FROM attribution_sessions
            WHERE {' AND '.join(where)}
            ORDER BY first_touch_at DESC
            LIMIT :limit
        """), {"since": since, "limit": limit}).fetchall()
        return {"sessions": [dict(r._mapping) for r in rows], "total": len(rows)}
    finally:
        db.close()

# ── UTM Ping — called by browser pixel snippet ────────────────────────────────

class UTMPingPayload(BaseModel):
    nexa_cid: Optional[str] = None
    fbclid: Optional[str] = None
    gclid: Optional[str] = None
    ttclid: Optional[str] = None
    msclkid: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    utm_content: Optional[str] = None
    utm_term: Optional[str] = None
    landing_page: Optional[str] = None
    referrer: Optional[str] = None


@router.post("/utm/ping", include_in_schema=False)
async def utm_ping(payload: UTMPingPayload):
    """
    Called by the browser pixel snippet on page load when UTMs or ad click IDs
    are present in the URL. Writes or updates the attribution_sessions row.
    No auth required — public endpoint.
    """
    if not payload.nexa_cid:
        return {"ok": False, "reason": "no_cid"}
    db = _db()
    try:
        from sqlalchemy import text as sqlt2
        db.execute(sqlt2("""
            INSERT INTO attribution_sessions
              (nexa_cid, tenant_id, utm_source, utm_medium, utm_campaign,
               utm_content, utm_term, fbclid, gclid, ttclid, msclkid,
               landing_page, referrer, first_touch_at, last_touch_at, touch_count)
            VALUES
              (:cid, 'nexabuilder', :src, :med, :cmp,
               :cnt, :trm, :fbc, :gcl, :ttc, :msc,
               :lp, :ref, NOW(), NOW(), 1)
            ON CONFLICT (nexa_cid) DO UPDATE SET
              last_touch_at = NOW(),
              touch_count   = attribution_sessions.touch_count + 1,
              fbclid  = COALESCE(:fbc, attribution_sessions.fbclid),
              gclid   = COALESCE(:gcl, attribution_sessions.gclid),
              ttclid  = COALESCE(:ttc, attribution_sessions.ttclid)
        """), {
            "cid": payload.nexa_cid,
            "src": payload.utm_source, "med": payload.utm_medium,
            "cmp": payload.utm_campaign, "cnt": payload.utm_content,
            "trm": payload.utm_term, "fbc": payload.fbclid,
            "gcl": payload.gclid, "ttc": payload.ttclid,
            "msc": payload.msclkid, "lp": payload.landing_page,
            "ref": payload.referrer
        })
        db.commit()
        return {"ok": True, "nexa_cid": payload.nexa_cid}
    except Exception as e:
        log.warning(f"UTM ping failed: {e}")
        return {"ok": False, "reason": str(e)}
    finally:
        db.close()
