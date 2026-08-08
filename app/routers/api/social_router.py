"""
social_router.py — NexaBuilder CMS Social Suite
Supports both:
  - Pinterest dashboard Access Tokens (pina_...)  → used directly as Bearer
  - OAuth2 refresh tokens → exchanged for access tokens
"""

import os, secrets, hashlib, logging
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Header, Request, BackgroundTasks
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import text as sqlt

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/social", tags=["social"])

ADMIN_KEY      = os.getenv("CMS_ADMIN_KEY", "")
PINTEREST_API  = "https://api.pinterest.com/v5"
SITE_BASE      = "https://www.nexabuilder.com"
API_BASE       = "https://api.nexabuilder.com"
REDIRECT_URI   = f"{API_BASE}/api/social/pinterest/callback"

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

def _get_stored_token():
    """Read whatever token is stored — access token or refresh token."""
    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT config_value FROM app_configs WHERE config_key='pinterest_refresh_token' LIMIT 1"
        )).fetchone()
        return row[0] if row else None
    finally:
        db.close()

async def _get_access_token() -> str:
    """
    Return a usable Pinterest Bearer token.

    Pinterest developer dashboard generates Access Tokens that start with 'pina_'.
    These are used DIRECTLY as bearer tokens — no refresh needed.

    OAuth2 refresh tokens (from the /authorize flow) start differently and need
    to be exchanged. We detect which we have and handle accordingly.
    """
    stored = _get_stored_token()
    if not stored:
        raise HTTPException(status_code=503,
            detail="Pinterest not connected. Run store_pinterest_token.py with your Access Token.")

    # Dashboard-generated Access Token — use directly
    if stored.startswith("pina_"):
        return stored

    # OAuth2 refresh token — exchange for access token
    import httpx, base64
    cid = os.getenv("PINTEREST_CLIENT_ID","")
    cs  = os.getenv("PINTEREST_CLIENT_SECRET","")
    if not cid:
        raise HTTPException(status_code=503, detail="PINTEREST_CLIENT_ID not set on server")
    auth = base64.b64encode(f"{cid}:{cs}".encode()).decode()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post("https://api.pinterest.com/v5/oauth/token",
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": stored})
    if not r.is_success:
        raise HTTPException(status_code=502, detail=f"Token refresh failed: {r.text[:200]}")
    return r.json()["access_token"]


# ── Pydantic models ───────────────────────────────────────────

class SocialFieldUpdate(BaseModel):
    pinterest_title:       Optional[str] = Field(None, max_length=100)
    pinterest_description: Optional[str] = Field(None, max_length=500)
    pinterest_board_id:    Optional[str] = None
    instagram_caption:     Optional[str] = Field(None, max_length=2200)
    instagram_hashtags:    Optional[str] = None
    publish_to_linkinbio:  Optional[bool] = None
    utm_campaign:          Optional[str] = None

class PinPublishRequest(BaseModel):
    page_slug:  str
    block_key:  str
    board_id:   str
    image_url:  str
    alt_text:   Optional[str] = ""
    tenant_id:  str = "nexabuilder"

class TokenRequest(BaseModel):
    refresh_token: str


# ══════════════════════════════════════════════════════════════
#  PINTEREST OAUTH FLOW (for future full OAuth — optional)
# ══════════════════════════════════════════════════════════════

@router.get("/pinterest/authorize")
async def pinterest_authorize():
    """
    Starts the Pinterest OAuth2 flow. Redirect URI to register in Pinterest portal:
      https://api.nexabuilder.com/api/social/pinterest/callback
    Note: if you already stored a dashboard Access Token via set-token,
    you don't need this flow — the boards and publish endpoints already work.
    """
    cid = os.getenv("PINTEREST_CLIENT_ID","")
    if not cid:
        raise HTTPException(status_code=503, detail="PINTEREST_CLIENT_ID env var not set")
    state = secrets.token_urlsafe(32)
    db = _db()
    try:
        db.execute(sqlt("INSERT INTO pinterest_oauth_states (state) VALUES (:s)"), {"s": state})
        db.commit()
    finally:
        db.close()
    auth_url = (
        f"https://www.pinterest.com/oauth/"
        f"?client_id={cid}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=boards:read,pins:read,pins:write"
        f"&state={state}"
    )
    return RedirectResponse(url=auth_url)


@router.get("/pinterest/callback")
async def pinterest_callback(code: str = None, state: str = None, error: str = None):
    """
    Pinterest redirects here after OAuth consent.
    Register this URL in Pinterest developer portal → your app → Redirect URIs:
      https://api.nexabuilder.com/api/social/pinterest/callback
    """
    if error:
        return HTMLResponse(content=f"""
        <html><body style="font-family:sans-serif;background:#0D1117;color:#fff;
          display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
        <div style="text-align:center;padding:40px;background:#1a1f2b;border-radius:12px;
          border:1px solid #333;max-width:480px">
          <h2 style="color:#ef4444">Authorization Failed</h2>
          <p style="color:#9ca3af">Pinterest returned: {error}</p>
          <p style="color:#6b7280;font-size:14px">Close this tab and try again.</p>
        </div></body></html>""", status_code=400)

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT state FROM pinterest_oauth_states WHERE state=:s "
            "AND created_at > NOW()-INTERVAL '10 minutes'"
        ), {"s": state}).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Invalid or expired state")
        db.execute(sqlt("DELETE FROM pinterest_oauth_states WHERE state=:s"), {"s": state})
        db.commit()
    finally:
        db.close()

    import httpx, base64
    cid = os.getenv("PINTEREST_CLIENT_ID","")
    cs  = os.getenv("PINTEREST_CLIENT_SECRET","")
    auth = base64.b64encode(f"{cid}:{cs}".encode()).decode()
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://api.pinterest.com/v5/oauth/token",
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type":"authorization_code","code":code,"redirect_uri":REDIRECT_URI})

    if not r.is_success:
        return HTMLResponse(content=f"""
        <html><body style="font-family:sans-serif;background:#0D1117;color:#fff;
          display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
        <div style="text-align:center;padding:40px;background:#1a1f2b;border-radius:12px;
          border:1px solid #333">
          <h2 style="color:#ef4444">Token Exchange Failed</h2>
          <p style="color:#9ca3af">{r.text[:200]}</p>
        </div></body></html>""", status_code=502)

    tokens = r.json()
    token = tokens.get("refresh_token") or tokens.get("access_token","")
    db2 = _db()
    try:
        db2.execute(sqlt("""
            INSERT INTO app_configs (config_key, config_value, updated_at)
            VALUES ('pinterest_refresh_token', :t, NOW())
            ON CONFLICT (config_key) DO UPDATE SET config_value=:t, updated_at=NOW()
        """), {"t": token})
        db2.commit()
    finally:
        db2.close()

    return HTMLResponse(content="""
    <html><body style="font-family:sans-serif;background:#0D1117;color:#fff;
      display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
    <div style="text-align:center;padding:48px;background:#1a1f2b;border-radius:16px;
      border:1px solid #D4A435;max-width:500px">
      <div style="font-size:48px;margin-bottom:16px">✅</div>
      <h2 style="color:#D4A435;margin-bottom:8px">Pinterest Connected!</h2>
      <p style="color:#9ca3af;margin-bottom:8px">Token stored. Board selector will populate in CMS.</p>
      <p style="color:#6b7280;font-size:13px">You can close this tab.</p>
    </div></body></html>""")


# ══════════════════════════════════════════════════════════════
#  SOCIAL COPY FIELDS
# ══════════════════════════════════════════════════════════════

@router.get("/fields/{page_slug:path}")
async def get_social_fields(page_slug: str, _: bool = Depends(require_admin)):
    db = _db()
    try:
        rows = db.execute(sqlt("""
            SELECT block_key, pinterest_title, pinterest_description, pinterest_board_id,
                   pinterest_pin_id, pinterest_pin_url, pinterest_published_at,
                   pin_impressions, pin_saves, pin_clicks,
                   instagram_caption, instagram_hashtags, publish_to_linkinbio, utm_campaign
            FROM social_distribution_fields
            WHERE tenant_id='nexabuilder' AND page_slug=:p ORDER BY block_key
        """), {"p": page_slug}).fetchall()
        return [dict(r._mapping) for r in rows]
    finally:
        db.close()

@router.put("/fields/{page_slug:path}/{block_key}")
async def save_social_fields(page_slug: str, block_key: str,
                              payload: SocialFieldUpdate, _: bool = Depends(require_admin)):
    db = _db()
    try:
        upd = {k: v for k, v in payload.dict().items() if v is not None}
        if not upd:
            return {"status": "no_change"}
        set_clause = ", ".join(f"{k}=:{k}" for k in upd)
        cols = ", ".join(upd.keys())
        vals = ", ".join(f":{k}" for k in upd)
        upd.update({"p": page_slug, "b": block_key, "now": datetime.utcnow()})
        db.execute(sqlt(f"""
            INSERT INTO social_distribution_fields
              (tenant_id, page_slug, block_key, {cols}, updated_at)
            VALUES ('nexabuilder', :p, :b, {vals}, :now)
            ON CONFLICT (tenant_id, page_slug, block_key)
            DO UPDATE SET {set_clause}, updated_at=:now
        """), upd)
        db.commit()
        return {"status": "saved", "block_key": block_key}
    except Exception as e:
        db.rollback(); raise HTTPException(status_code=500, detail=str(e)[:200])
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════
#  PINTEREST BOARDS + PUBLISH + METRICS
# ══════════════════════════════════════════════════════════════

@router.get("/pinterest/boards")
async def fetch_boards(_: bool = Depends(require_admin)):
    import httpx
    token = await _get_access_token()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{PINTEREST_API}/boards",
                        headers={"Authorization": f"Bearer {token}"})
    if not r.is_success:
        raise HTTPException(status_code=502, detail=f"Boards fetch failed: {r.text[:300]}")
    items = r.json().get("items", [])
    return {"boards": [{"id": b["id"], "name": b["name"]} for b in items]}

@router.post("/pinterest/set-token")
async def set_token(payload: TokenRequest, _: bool = Depends(require_admin)):
    db = _db()
    try:
        db.execute(sqlt("""
            INSERT INTO app_configs (config_key, config_value, updated_at)
            VALUES ('pinterest_refresh_token', :t, NOW())
            ON CONFLICT (config_key) DO UPDATE SET config_value=:t, updated_at=NOW()
        """), {"t": payload.refresh_token})
        db.commit()
        token_type = "dashboard Access Token" if payload.refresh_token.startswith("pina_") else "OAuth refresh token"
        return {"status": "token_stored", "token_type": token_type}
    finally:
        db.close()

@router.post("/pinterest/publish")
async def publish_pin(payload: PinPublishRequest, _: bool = Depends(require_admin)):
    import httpx
    db = _db()
    try:
        row = db.execute(sqlt("""
            SELECT pinterest_title, pinterest_description, pinterest_pin_id, utm_campaign
            FROM social_distribution_fields
            WHERE tenant_id=:t AND page_slug=:p AND block_key=:b
        """), {"t": payload.tenant_id, "p": payload.page_slug, "b": payload.block_key}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No social fields for this block")
        if row[2]:
            return {"status":"already_published","pin_id":row[2],
                    "pin_url":f"https://www.pinterest.com/pin/{row[2]}/"}
        utm   = row[3] or "nexabuilder"
        link  = (f"{SITE_BASE}/{payload.page_slug}/"
                 f"?utm_source=pinterest&utm_medium=social&utm_campaign={utm}")
        token = await _get_access_token()
        body  = {
            "board_id": payload.board_id,
            "link": link,
            "title": (row[0] or "")[:100],
            "description": (row[1] or "")[:500],
            "media_source": {"source_type":"image_url","url":payload.image_url,
                             "alt_text":(payload.alt_text or "")[:500]}
        }
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{PINTEREST_API}/pins", json=body,
                headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"})
        if r.status_code == 201:
            pid  = r.json().get("id","")
            purl = f"https://www.pinterest.com/pin/{pid}/"
            db.execute(sqlt("""
                UPDATE social_distribution_fields
                SET pinterest_pin_id=:pid, pinterest_pin_url=:purl, pinterest_published_at=NOW()
                WHERE tenant_id=:t AND page_slug=:p AND block_key=:b
            """), {"pid":pid,"purl":purl,"t":payload.tenant_id,"p":payload.page_slug,"b":payload.block_key})
            db.commit()
            return {"status":"published","pin_id":pid,"pin_url":purl}
        err = {}
        try: err = r.json()
        except: pass
        raise HTTPException(status_code=r.status_code,
                            detail=err.get("message") or f"Pinterest error {r.status_code}: {r.text[:200]}")
    finally:
        db.close()

@router.get("/pinterest/metrics/{page_slug:path}")
async def sync_metrics(page_slug: str, bg: BackgroundTasks, _: bool = Depends(require_admin)):
    bg.add_task(_run_metric_sync, page_slug)
    return {"status":"syncing"}

async def _run_metric_sync(page_slug: str):
    import httpx
    db = _db()
    try:
        rows = db.execute(sqlt("""
            SELECT block_key, pinterest_pin_id FROM social_distribution_fields
            WHERE tenant_id='nexabuilder' AND page_slug=:p AND pinterest_pin_id IS NOT NULL
        """), {"p": page_slug}).fetchall()
        if not rows: return
        token = await _get_access_token()
        end   = datetime.utcnow().strftime("%Y-%m-%d")
        start = (datetime.utcnow()-timedelta(days=30)).strftime("%Y-%m-%d")
        async with httpx.AsyncClient(timeout=20) as c:
            for row in rows:
                pid = row[1]
                r = await c.get(
                    f"{PINTEREST_API}/pins/{pid}/analytics"
                    f"?start_date={start}&end_date={end}&metric_types=IMPRESSION,SAVE,PIN_CLICK",
                    headers={"Authorization": f"Bearer {token}"})
                if not r.is_success: continue
                m = r.json().get("all",{}).get("summary_metrics",{})
                db.execute(sqlt("""
                    UPDATE social_distribution_fields
                    SET pin_impressions=:i, pin_saves=:s, pin_clicks=:c, pin_metrics_synced_at=NOW()
                    WHERE pinterest_pin_id=:pid
                """), {"i":m.get("IMPRESSION",0),"s":m.get("SAVE",0),
                       "c":m.get("PIN_CLICK",0),"pid":pid})
        db.commit()
    except Exception as e:
        log.error(f"Metric sync error: {e}")
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════
#  UTM STATS
# ══════════════════════════════════════════════════════════════

@router.get("/utm/stats")
async def utm_stats(_: bool = Depends(require_admin)):
    db = _db()
    try:
        sources = db.execute(sqlt("""
            SELECT COALESCE(utm_source,'(direct)') as source,
                   COALESCE(utm_medium,'(none)') as medium,
                   COUNT(*) as visits
            FROM utm_visits WHERE visited_at > NOW()-INTERVAL '30 days'
            GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20
        """)).fetchall()
        pages = db.execute(sqlt("""
            SELECT page_path, COUNT(*) as visits,
                   COUNT(DISTINCT ip_hash) as unique_visitors
            FROM utm_visits WHERE visited_at > NOW()-INTERVAL '30 days'
            GROUP BY 1 ORDER BY 2 DESC LIMIT 20
        """)).fetchall()
        campaigns = db.execute(sqlt("""
            SELECT COALESCE(utm_campaign,'(none)') as campaign, COUNT(*) as visits
            FROM utm_visits WHERE visited_at > NOW()-INTERVAL '30 days'
              AND utm_source IS NOT NULL
            GROUP BY 1 ORDER BY 2 DESC LIMIT 10
        """)).fetchall()
        total = db.execute(sqlt(
            "SELECT COUNT(*) FROM utm_visits WHERE visited_at > NOW()-INTERVAL '30 days'"
        )).scalar()
        return {
            "period": "last_30_days",
            "total_visits": total,
            "by_source":  [dict(r._mapping) for r in sources],
            "top_pages":  [dict(r._mapping) for r in pages],
            "campaigns":  [dict(r._mapping) for r in campaigns],
        }
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════
#  MIDDLEWARE — UTM + epik tracking
# ══════════════════════════════════════════════════════════════



@router.get("/pinterest/boards-local")
async def get_boards_local(_: bool = Depends(require_admin)):
    """
    Returns Pinterest boards from local DB (app_configs).
    Does not call Pinterest API. Works regardless of token state.
    Board IDs stored when boards were created/verified.
    """
    db = _db()
    try:
        rows = db.execute(sqlt(
            "SELECT config_key, config_value FROM app_configs "
            "WHERE config_key LIKE 'pinterest_board_%' ORDER BY config_key"
        )).fetchall()
        name_map = {
            'pinterest_board_talavera':    'Talavera Tile - Southern California',
            'pinterest_board_stone':       'Natural Stone Soaking Tubs - Mexico',
            'pinterest_board_nexabuilder': 'NexaBuilder - Contractor Matching SoCal',
        }
        boards = []
        for row in rows:
            key, bid = row[0], row[1]
            name = name_map.get(key, key.replace('pinterest_board_','').replace('_',' ').title())
            boards.append({"id": bid, "name": name})
        return {"boards": boards, "source": "local"}
    finally:
        db.close()


async def tracking_middleware(request: Request, call_next):
    params = request.query_params
    path   = str(request.url.path)

    # UTM visit logging
    if any(params.get(k) for k in ["utm_source","utm_medium","utm_campaign","utm_content","utm_term"]):
        try:
            raw_ip  = (request.headers.get("x-forwarded-for","").split(",")[0].strip()
                       or getattr(request.client, "host", ""))
            ip_hash = hashlib.sha256(raw_ip.encode()).hexdigest()[:16]
            db = _db()
            db.execute(sqlt("""
                INSERT INTO utm_visits
                  (page_path,utm_source,utm_medium,utm_campaign,
                   utm_content,utm_term,referrer,user_agent,ip_hash)
                VALUES (:path,:src,:med,:camp,:cont,:term,:ref,:ua,:ip)
            """), {
                "path": path[:500],
                "src":  params.get("utm_source","")[:100],
                "med":  params.get("utm_medium","")[:100],
                "camp": params.get("utm_campaign","")[:200],
                "cont": params.get("utm_content","")[:200],
                "term": params.get("utm_term","")[:200],
                "ref":  request.headers.get("referer","")[:500],
                "ua":   request.headers.get("user-agent","")[:500],
                "ip":   ip_hash,
            })
            db.commit(); db.close()
        except Exception as e:
            log.error(f"UTM tracking: {e}")

    # Pinterest epik click token
    epik = params.get("epik")
    if epik:
        try:
            db = _db()
            db.execute(sqlt("""
                INSERT INTO pin_interaction_logs
                  (epik_token, page_slug, page_path, campaign_name, utm_source, interaction_type)
                VALUES (:e, :slug, :path, :camp, 'pinterest', 'PIN_CLICK')
                ON CONFLICT (epik_token) DO NOTHING
            """), {
                "e":    epik[:200],
                "slug": path.strip("/")[:255],
                "path": path[:500],
                "camp": params.get("utm_campaign","")[:100],
            })
            db.commit(); db.close()
        except Exception as e:
            log.error(f"epik capture: {e}")

    return await call_next(request)
