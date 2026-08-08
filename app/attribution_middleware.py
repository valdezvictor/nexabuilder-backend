"""
attribution_middleware.py — NexaBuilder First-Party Attribution Layer

Runs on every inbound API request. Responsibilities:
1. Generate or read nexa_cid (first-party click ID)
2. Set HttpOnly cookie (SameSite=Lax, Secure)
3. Parse UTMs + ad platform click IDs (fbclid, gclid, ttclid, etc.)
4. Write/update attribution_sessions in PostgreSQL
5. Inject nexa_cid into request state so downstream handlers can use it
"""
import os
import uuid
import hashlib
import logging
from typing import Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from sqlalchemy import text as sqlt

log = logging.getLogger(__name__)

COOKIE_NAME   = "nexa_cid"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year
SECURE_COOKIE  = os.getenv("ENV", "production") == "production"

# Ad platform click ID param names
CLICK_ID_PARAMS = {
    "fbclid":    "fbclid",   # Meta / Facebook
    "gclid":     "gclid",    # Google Ads
    "ttclid":    "ttclid",   # TikTok
    "msclkid":   "msclkid",  # Microsoft / Bing
    "twclid":    "twclid",   # Twitter / X
    "li_fat_id": "li_fat_id" # LinkedIn
}

# Paths that need attribution tracking (skip health checks, static, admin)
TRACK_PREFIXES = ["/api/leads/", "/api/utm/", "/services/", "/materials/", "/blog/"]
SKIP_PATHS     = ["/health", "/metrics", "/openapi", "/docs", "/redoc"]


def _db_sync():
    """Synchronous DB session for middleware (Starlette middleware is sync by default)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg", "postgresql+psycopg2"),
        pool_pre_ping=True, pool_size=2, max_overflow=3
    )
    return sessionmaker(bind=engine)()


def _hash_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()[:16] if ip else ""


def _detect_device(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if any(x in ua for x in ["mobile", "android", "iphone", "ipod"]):
        return "mobile"
    if any(x in ua for x in ["tablet", "ipad"]):
        return "tablet"
    return "desktop"


class AttributionMiddleware(BaseHTTPMiddleware):
    """
    Intercepts every inbound request. Generates or reads the nexa_cid,
    writes an attribution_sessions row, and injects session data into
    request.state for downstream handlers.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Skip non-tracked paths
        if any(path.startswith(s) for s in SKIP_PATHS):
            return await call_next(request)

        # Only track relevant paths
        should_track = (
            any(path.startswith(p) for p in TRACK_PREFIXES)
            or "utm_source" in str(request.url.query)
            or any(c in str(request.url.query) for c in CLICK_ID_PARAMS)
        )

        if not should_track:
            return await call_next(request)

        try:
            # ── 1. Read or generate nexa_cid ──────────────────────────────
            existing_cid = request.cookies.get(COOKIE_NAME)
            is_new_session = not existing_cid
            nexa_cid = existing_cid or f"ncid_{uuid.uuid4().hex}"

            # ── 2. Parse query params ──────────────────────────────────────
            params = dict(request.query_params)
            utm_source   = params.get("utm_source")
            utm_medium   = params.get("utm_medium")
            utm_campaign = params.get("utm_campaign")
            utm_content  = params.get("utm_content")
            utm_term     = params.get("utm_term")
            landing_page = str(request.url)[:500]
            referrer     = request.headers.get("referer", "")[:500]
            user_agent   = request.headers.get("user-agent", "")
            ip           = (request.headers.get("x-forwarded-for", "") or
                           request.headers.get("x-real-ip", "") or
                           (request.client.host if request.client else "")).split(",")[0].strip()

            click_ids = {k: params.get(v) for k, v in CLICK_ID_PARAMS.items() if params.get(v)}

            # ── 3. Inject into request.state ───────────────────────────────
            request.state.nexa_cid    = nexa_cid
            request.state.session_data = {
                "nexa_cid": nexa_cid,
                "utm_source": utm_source,
                "utm_medium": utm_medium,
                "utm_campaign": utm_campaign,
                "utm_content": utm_content,
                "utm_term": utm_term,
                "landing_page": landing_page,
                "referrer": referrer,
                **click_ids
            }

            # ── 4. Write to attribution_sessions ───────────────────────────
            # Only if there are UTMs or click IDs, or it's a new session with lead path
            has_signals = bool(
                utm_source or utm_medium or click_ids or
                (is_new_session and any(path.startswith(p) for p in ["/api/leads/"]))
            )

            if has_signals or is_new_session:
                try:
                    db = _db_sync()
                    try:
                        if is_new_session:
                            db.execute(sqlt("""
                                INSERT INTO attribution_sessions
                                  (nexa_cid, utm_source, utm_medium, utm_campaign,
                                   utm_content, utm_term, landing_page, referrer,
                                   ip_hash, user_agent, device_type,
                                   fbclid, gclid, ttclid, msclkid, raw_payload)
                                VALUES
                                  (:cid, :src, :med, :camp,
                                   :cont, :term, :lp, :ref,
                                   :ip, :ua, :dev,
                                   :fbclid, :gclid, :ttclid, :msclkid, :raw::jsonb)
                                ON CONFLICT (nexa_cid) DO UPDATE SET
                                  last_touch_at = NOW(),
                                  touch_count   = attribution_sessions.touch_count + 1,
                                  updated_at    = NOW()
                            """), {
                                "cid":    nexa_cid,
                                "src":    utm_source,
                                "med":    utm_medium,
                                "camp":   utm_campaign,
                                "cont":   utm_content,
                                "term":   utm_term,
                                "lp":     landing_page[:500],
                                "ref":    referrer[:500],
                                "ip":     _hash_ip(ip),
                                "ua":     user_agent[:500],
                                "dev":    _detect_device(user_agent),
                                "fbclid": click_ids.get("fbclid"),
                                "gclid":  click_ids.get("gclid"),
                                "ttclid": click_ids.get("ttclid"),
                                "msclkid":click_ids.get("msclkid"),
                                "raw":    '{"source":"middleware"}'
                            })
                        else:
                            # Returning visitor — update last touch and preserve click IDs
                            update_parts = ["last_touch_at=NOW()", "touch_count=attribution_sessions.touch_count+1",
                                           "updated_at=NOW()"]
                            update_params = {"cid": nexa_cid}
                            if utm_source:
                                update_parts.append("utm_source=:src"); update_params["src"] = utm_source
                            if utm_campaign:
                                update_parts.append("utm_campaign=:camp"); update_params["camp"] = utm_campaign
                            for cid_key in ["fbclid","gclid","ttclid"]:
                                if click_ids.get(cid_key):
                                    update_parts.append(f"{cid_key}=:{cid_key}")
                                    update_params[cid_key] = click_ids[cid_key]
                            db.execute(sqlt(
                                f"UPDATE attribution_sessions SET {','.join(update_parts)} WHERE nexa_cid=:cid"
                            ), update_params)

                        db.commit()
                    except Exception as e:
                        log.warning(f"Attribution session write error: {e}")
                        db.rollback()
                    finally:
                        db.close()
                except Exception as e:
                    log.warning(f"Attribution DB connection error: {e}")

        except Exception as e:
            log.warning(f"Attribution middleware error: {e}")

        # ── 5. Call next handler ───────────────────────────────────────────
        response = await call_next(request)

        # ── 6. Set nexa_cid cookie on response ────────────────────────────
        if hasattr(request.state, "nexa_cid"):
            response.set_cookie(
                key=COOKIE_NAME,
                value=request.state.nexa_cid,
                max_age=COOKIE_MAX_AGE,
                httponly=True,
                secure=SECURE_COOKIE,
                samesite="lax",
                path="/"
            )

        return response
