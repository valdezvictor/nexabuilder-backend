"""
call_tracking_router.py — #19 Call Tracking
Manages tracked phone numbers per UTM source.
When a user lands with ?utm_source=google, the JS swaps the displayed number.
All call events logged to attribution_events.
"""
import os, logging
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import text as sqlt

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/call-tracking", tags=["Call Tracking"])
ADMIN_KEY = os.getenv("CMS_ADMIN_KEY", "")

def _req(key):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        os.getenv("DATABASE_URL","").replace("postgresql+asyncpg","postgresql+psycopg2"),
        pool_pre_ping=True)
    return sessionmaker(bind=engine)()


# ── Schema (created inline) ───────────────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tracked_phone_numbers (
    id              SERIAL PRIMARY KEY,
    display_number  VARCHAR(20) NOT NULL,
    forwarding_to   VARCHAR(20) NOT NULL DEFAULT '+12138784536',
    utm_source      VARCHAR(100),
    utm_medium      VARCHAR(100),
    utm_campaign    VARCHAR(255),
    vertical        VARCHAR(50),
    label           VARCHAR(100),
    is_active       BOOLEAN DEFAULT TRUE,
    call_count      INT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS call_events (
    id              BIGSERIAL PRIMARY KEY,
    nexa_cid        VARCHAR(255),
    session_id      UUID,
    display_number  VARCHAR(20),
    utm_source      VARCHAR(100),
    utm_medium      VARCHAR(100),
    utm_campaign    VARCHAR(255),
    vertical        VARCHAR(50),
    page_path       TEXT,
    user_agent      TEXT,
    ip_hash         VARCHAR(64),
    occurred_at     TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO tracked_phone_numbers
  (display_number, forwarding_to, utm_source, label, vertical)
VALUES
  ('+12138784536', '+12138784536', NULL,       'Default (organic)', NULL),
  ('+12138784537', '+12138784536', 'google',   'Google Ads',        NULL),
  ('+12138784538', '+12138784536', 'facebook', 'Meta / Facebook',   NULL),
  ('+12138784539', '+12138784536', 'pinterest','Pinterest',         'pool'),
  ('+12138784540', '+12138784536', 'bing',     'Bing / Microsoft',  NULL)
ON CONFLICT DO NOTHING;
"""


@router.on_event("startup")
async def ensure_schema():
    """Create tables on startup if they don't exist."""
    try:
        db = _db()
        for stmt in SCHEMA_SQL.strip().split(';'):
            stmt = stmt.strip()
            if stmt:
                try: db.execute(sqlt(stmt)); db.commit()
                except: db.rollback()
        db.close()
    except Exception as e:
        log.warning(f"Call tracking schema init: {e}")


# ── 1. Get number for a UTM source (called by frontend JS) ───────────────────
@router.get("/number")
async def get_tracked_number(
    utm_source:   Optional[str] = None,
    utm_medium:   Optional[str] = None,
    utm_campaign: Optional[str] = None,
    vertical:     Optional[str] = None,
    request: Request = None
):
    """
    Returns the display number to show for the given UTM combo.
    Called by the call tracking JS snippet on every page load.
    Falls back to the default number if no match.
    """
    db = _db()
    try:
        # Try exact source match first
        row = None
        if utm_source:
            row = db.execute(sqlt("""
                SELECT display_number, label
                FROM tracked_phone_numbers
                WHERE is_active=TRUE
                  AND utm_source = :src
                  AND (vertical IS NULL OR vertical = :vert)
                ORDER BY vertical NULLS LAST
                LIMIT 1
            """), {"src": utm_source, "vert": vertical or ""}).fetchone()

        # Fall back to default
        if not row:
            row = db.execute(sqlt("""
                SELECT display_number, label
                FROM tracked_phone_numbers
                WHERE is_active=TRUE AND utm_source IS NULL LIMIT 1
            """)).fetchone()

        number = row[0] if row else "+12138784536"
        label  = row[1] if row else "Default"

        # Format for display
        digits = ''.join(c for c in number if c.isdigit())
        if len(digits) == 11 and digits[0]=='1':
            digits = digits[1:]
        formatted = f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits)==10 else number

        return {
            "number": number,
            "display": formatted,
            "label": label,
            "tel_href": f"tel:{number}"
        }
    finally:
        db.close()


# ── 2. Log a call event ───────────────────────────────────────────────────────
class CallEventPayload(BaseModel):
    nexa_cid:     Optional[str] = None
    display_number: Optional[str] = None
    utm_source:   Optional[str] = None
    utm_medium:   Optional[str] = None
    utm_campaign: Optional[str] = None
    vertical:     Optional[str] = None
    page_path:    Optional[str] = None


@router.post("/event")
async def log_call_event(payload: CallEventPayload, request: Request):
    """Log a call click to call_events and attribution_events."""
    db = _db()
    try:
        import hashlib
        ip = (request.headers.get("x-forwarded-for","") or
              (request.client.host if request.client else "")).split(",")[0].strip()
        ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]

        # Log to call_events
        db.execute(sqlt("""
            INSERT INTO call_events
              (nexa_cid, display_number, utm_source, utm_medium,
               utm_campaign, vertical, page_path, ip_hash)
            VALUES (:cid, :num, :src, :med, :camp, :vert, :path, :ip)
        """), {
            "cid": payload.nexa_cid, "num": payload.display_number,
            "src": payload.utm_source, "med": payload.utm_medium,
            "camp": payload.utm_campaign, "vert": payload.vertical,
            "path": payload.page_path, "ip": ip_hash
        })

        # Also emit to attribution_events (unified timeline)
        if payload.nexa_cid:
            db.execute(sqlt("""
                INSERT INTO attribution_events
                  (nexa_cid, event_type, vertical, page_path, event_data)
                VALUES (:cid, 'phone_call', :vert, :path, :data::jsonb)
            """), {
                "cid": payload.nexa_cid, "vert": payload.vertical,
                "path": payload.page_path,
                "data": f'{{"display_number":"{payload.display_number}","utm_source":"{payload.utm_source}"}}'
            })

        # Increment call count on the number
        if payload.display_number:
            db.execute(sqlt("""
                UPDATE tracked_phone_numbers
                SET call_count = call_count + 1
                WHERE display_number = :num
            """), {"num": payload.display_number})

        db.commit()
        return {"status": "logged"}
    finally:
        db.close()


# ── 3. Admin: list numbers + call stats ──────────────────────────────────────
@router.get("/numbers")
async def list_numbers(x_admin_key: str = Header(...)):
    _req(x_admin_key)
    db = _db()
    try:
        rows = db.execute(sqlt("""
            SELECT t.id, t.display_number, t.forwarding_to, t.utm_source,
                   t.utm_medium, t.label, t.vertical, t.is_active, t.call_count,
                   COUNT(c.id) as total_call_events
            FROM tracked_phone_numbers t
            LEFT JOIN call_events c ON c.display_number = t.display_number
            GROUP BY t.id
            ORDER BY t.call_count DESC
        """)).fetchall()
        return {"numbers": [dict(r._mapping) for r in rows]}
    finally:
        db.close()
