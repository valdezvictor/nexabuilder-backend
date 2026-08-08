"""
gsc_router.py - Google Search Console integration for NexaBuilder
Endpoints:
  GET  /api/gsc/authorize           - Start OAuth flow
  GET  /api/gsc/callback            - OAuth callback, stores token
  POST /api/gsc/sync                - Pull keyword data from GSC API → DB
  GET  /api/gsc/keywords            - Return cached keyword data from DB
  GET  /api/gsc/keywords/top        - Top queries by impressions
  POST /api/gsc/set-token           - Manual token storage (admin)
"""

import os, secrets, logging, json as json_mod
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Header, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import text as sqlt

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/gsc", tags=["Search Console"])

ADMIN_KEY = os.getenv("CMS_ADMIN_KEY", "")
GSC_SITE  = "sc-domain:nexabuilder.com"

# Google OAuth config — add to .env:
# GOOGLE_CLIENT_ID=your_client_id
# GOOGLE_CLIENT_SECRET=your_client_secret
# GSC_REDIRECT_URI=https://api.nexabuilder.com/api/gsc/callback
GSC_REDIRECT = os.getenv("GSC_REDIRECT_URI", "https://api.nexabuilder.com/api/gsc/callback")
SCOPES = "https://www.googleapis.com/auth/webmasters.readonly"

def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True

def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        os.getenv("DATABASE_URL","").replace("postgresql+asyncpg","postgresql+psycopg2"),
        echo=False, pool_pre_ping=True
    )
    return sessionmaker(bind=engine)()

def _get_gsc_token():
    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT config_value FROM app_configs WHERE config_key='gsc_oauth_token' LIMIT 1"
        )).fetchone()
        return row[0] if row and row[0] else None
    finally:
        db.close()

def _infer_vertical(page: str) -> str:
    mapping = {
        "pool": "pool", "piscina": "pool",
        "roof": "roofing", "rufero": "roofing",
        "remodel": "remodeling", "kitchen": "remodeling", "bathroom": "remodeling",
        "electric": "electrical", "electricista": "electrical",
        "plumb": "plumbing", "plomero": "plumbing",
        "hvac": "hvac", "tecnico": "hvac",
        "landscap": "landscaping", "jardinero": "landscaping",
        "material": "materials", "talavera": "materials", "stone": "materials",
        "location": "local", "anaheim": "local", "long-beach": "local",
    }
    p = (page or "").lower()
    for k, v in mapping.items():
        if k in p:
            return v
    return "general"


# ── OAuth flow ────────────────────────────────────────────────────────────────

@router.get("/authorize")
async def gsc_authorize():
    """Redirect admin browser to Google OAuth consent for Search Console access."""
    cid = os.getenv("GOOGLE_CLIENT_ID","")
    if not cid:
        raise HTTPException(status_code=503,
            detail="GOOGLE_CLIENT_ID not set. Add it to .env on EC2.")
    state = secrets.token_urlsafe(24)
    db = _db()
    try:
        db.execute(sqlt(
            "INSERT INTO pinterest_oauth_states (state) VALUES (:s)"
        ), {"s": state})
        db.commit()
    finally:
        db.close()

    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={cid}"
        f"&redirect_uri={GSC_REDIRECT}"
        f"&response_type=code"
        f"&scope={SCOPES}"
        f"&state={state}"
        "&access_type=offline"
        "&prompt=consent"
    )
    return RedirectResponse(url=auth_url)


@router.get("/callback")
async def gsc_callback(code: str = None, state: str = None, error: str = None):
    """Exchange auth code for tokens and store."""
    if error:
        return HTMLResponse(content=f"<h2>GSC Auth Failed: {error}</h2>", status_code=400)
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")

    import httpx
    cid = os.getenv("GOOGLE_CLIENT_ID","")
    cs  = os.getenv("GOOGLE_CLIENT_SECRET","")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://oauth2.googleapis.com/token", data={
            "client_id": cid, "client_secret": cs,
            "code": code, "redirect_uri": GSC_REDIRECT,
            "grant_type": "authorization_code"
        })
    if not r.is_success:
        return HTMLResponse(content=f"<h2>Token exchange failed: {r.text[:200]}</h2>", status_code=502)

    tokens = r.json()
    token_json = json_mod.dumps(tokens)
    db = _db()
    try:
        db.execute(sqlt("""
            INSERT INTO app_configs (config_key, config_value, updated_at)
            VALUES ('gsc_oauth_token', :t, NOW())
            ON CONFLICT (config_key) DO UPDATE SET config_value=:t, updated_at=NOW()
        """), {"t": token_json})
        db.commit()
    finally:
        db.close()

    return HTMLResponse(content="""
    <html><body style="font-family:sans-serif;background:#0D1117;color:#fff;display:flex;
      align-items:center;justify-content:center;height:100vh;margin:0">
    <div style="text-align:center;padding:48px;background:#1a1f2b;border-radius:16px;
      border:1px solid #D4A435;max-width:480px">
      <div style="font-size:48px;margin-bottom:16px">&#10003;</div>
      <h2 style="color:#D4A435">Google Search Console Connected!</h2>
      <p style="color:#9ca3af;margin-top:12px">
        Token stored. Use POST /api/gsc/sync to pull keyword data.<br>
        You can close this tab.
      </p>
    </div></body></html>""")


# ── Token management ──────────────────────────────────────────────────────────

class TokenPayload(BaseModel):
    token_json: str  # Full Google token JSON from manual OAuth flow

@router.post("/set-token")
async def set_gsc_token(payload: TokenPayload, _: bool = Depends(require_admin)):
    db = _db()
    try:
        db.execute(sqlt("""
            INSERT INTO app_configs (config_key, config_value, updated_at)
            VALUES ('gsc_oauth_token', :t, NOW())
            ON CONFLICT (config_key) DO UPDATE SET config_value=:t, updated_at=NOW()
        """), {"t": payload.token_json})
        db.commit()
        return {"status": "token_stored"}
    finally:
        db.close()


# ── Sync keyword data ─────────────────────────────────────────────────────────

@router.post("/sync")
async def sync_gsc_keywords(bg: BackgroundTasks, _: bool = Depends(require_admin)):
    """Trigger background sync of GSC keyword data into gsc_keywords table."""
    token_json = _get_gsc_token()
    if not token_json:
        raise HTTPException(status_code=503,
            detail="GSC not connected. Visit /api/gsc/authorize first.")
    bg.add_task(_run_gsc_sync, token_json)
    return {"status": "syncing", "message": "GSC keyword sync started in background"}


async def _run_gsc_sync(token_json: str):
    """Pull top 1000 queries from GSC and cache in DB."""
    import httpx
    db = _db()
    try:
        tokens = json_mod.loads(token_json)
        access_token = tokens.get("access_token","")

        # Refresh token if needed
        if tokens.get("refresh_token") and tokens.get("expires_in"):
            cid = os.getenv("GOOGLE_CLIENT_ID","")
            cs  = os.getenv("GOOGLE_CLIENT_SECRET","")
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post("https://oauth2.googleapis.com/token", data={
                    "client_id": cid, "client_secret": cs,
                    "refresh_token": tokens["refresh_token"],
                    "grant_type": "refresh_token"
                })
                if r.is_success:
                    new_tokens = r.json()
                    access_token = new_tokens.get("access_token", access_token)
                    tokens.update(new_tokens)
                    db.execute(sqlt("""
                        UPDATE app_configs SET config_value=:t, updated_at=NOW()
                        WHERE config_key='gsc_oauth_token'
                    """), {"t": json_mod.dumps(tokens)})

        end_date   = datetime.utcnow().strftime("%Y-%m-%d")
        start_date = (datetime.utcnow() - timedelta(days=28)).strftime("%Y-%m-%d")

        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"https://searchconsole.googleapis.com/webmasters/v3/sites/{GSC_SITE}/searchAnalytics/query",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "startDate": start_date,
                    "endDate": end_date,
                    "dimensions": ["query","page"],
                    "rowLimit": 1000,
                    "startRow": 0
                }
            )

        if not r.is_success:
            log.error(f"GSC API error: {r.status_code} {r.text[:200]}")
            return

        rows = r.json().get("rows", [])
        log.info(f"GSC sync: {len(rows)} rows received")

        inserted = 0
        for row in rows:
            keys  = row.get("keys", [])
            query = keys[0] if len(keys) > 0 else ""
            page  = keys[1] if len(keys) > 1 else ""
            vertical = _infer_vertical(page)
            try:
                db.execute(sqlt("""
                    INSERT INTO gsc_keywords
                      (query, page, clicks, impressions, ctr, position, date_range, vertical, synced_at)
                    VALUES (:q, :p, :c, :i, :ctr, :pos, :dr, :v, NOW())
                    ON CONFLICT (query, page, date_range)
                    DO UPDATE SET clicks=:c, impressions=:i, ctr=:ctr, position=:pos,
                                  vertical=:v, synced_at=NOW()
                """), {
                    "q": query[:500], "p": page[:500],
                    "c": int(row.get("clicks",0)),
                    "i": int(row.get("impressions",0)),
                    "ctr": float(row.get("ctr",0)),
                    "pos": float(row.get("position",0)),
                    "dr": "last_28_days",
                    "v": vertical
                })
                inserted += 1
            except Exception as e:
                log.warning(f"Row insert error: {e}")

        db.commit()
        log.info(f"GSC sync complete: {inserted} rows upserted")

    except Exception as e:
        log.error(f"GSC sync error: {e}")
        db.rollback()
    finally:
        db.close()


# ── Read keyword data ─────────────────────────────────────────────────────────

@router.get("/keywords")
async def get_keywords(
    vertical: Optional[str] = None,
    limit: int = 50,
    sort: str = "impressions",
    _: bool = Depends(require_admin)
):
    """Return cached GSC keyword data from DB."""
    db = _db()
    try:
        order_col = "impressions" if sort not in ("clicks","position","ctr") else sort
        order_dir = "ASC" if order_col == "position" else "DESC"
        where = "WHERE vertical=:v" if vertical else ""
        params = {"v": vertical, "lim": limit} if vertical else {"lim": limit}
        rows = db.execute(sqlt(f"""
            SELECT query, page, clicks, impressions, ctr, position, vertical, synced_at
            FROM gsc_keywords {where}
            ORDER BY {order_col} {order_dir}
            LIMIT :lim
        """), params).fetchall()
        synced = db.execute(sqlt(
            "SELECT MAX(synced_at) FROM gsc_keywords"
        )).scalar()
        return {
            "last_synced": synced.isoformat() if synced else None,
            "count": len(rows),
            "keywords": [dict(r._mapping) for r in rows]
        }
    finally:
        db.close()


@router.get("/keywords/top")
async def get_top_keywords(_: bool = Depends(require_admin)):
    """Quick overview — top queries by impressions, grouped by vertical."""
    db = _db()
    try:
        top = db.execute(sqlt("""
            SELECT query, SUM(clicks) as clicks, SUM(impressions) as impressions,
                   AVG(position)::numeric(6,1) as avg_position, vertical
            FROM gsc_keywords
            GROUP BY query, vertical
            ORDER BY impressions DESC
            LIMIT 25
        """)).fetchall()
        by_vertical = db.execute(sqlt("""
            SELECT vertical, SUM(clicks) as clicks, SUM(impressions) as impressions,
                   COUNT(DISTINCT query) as unique_queries
            FROM gsc_keywords
            GROUP BY vertical
            ORDER BY impressions DESC
        """)).fetchall()
        last_sync = db.execute(sqlt("SELECT MAX(synced_at) FROM gsc_keywords")).scalar()
        total_rows = db.execute(sqlt("SELECT COUNT(*) FROM gsc_keywords")).scalar()
        return {
            "last_synced":  last_sync.isoformat() if last_sync else None,
            "total_queries": total_rows,
            "top_queries":  [dict(r._mapping) for r in top],
            "by_vertical":  [dict(r._mapping) for r in by_vertical],
        }
    finally:
        db.close()
