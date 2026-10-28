"""
instagram_router.py — Instagram OAuth + webhook handler
GET  /api/auth/instagram/callback  — OAuth redirect (captures code → exchanges for token)
POST /api/auth/instagram/webhook   — Instagram webhook receiver
GET  /api/auth/instagram/webhook   — Webhook verification challenge
GET  /api/instagram/me             — Test current token
"""
import os, logging, hashlib, hmac
from fastapi import APIRouter, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth/instagram", tags=["Instagram"])

ADMIN_KEY    = os.getenv("CMS_ADMIN_KEY", "")
IG_VERIFY_TOKEN = "nexabuilder_webhook_verify_2026"


def _db():
    from sqlalchemy import create_engine, text as sqlt
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        os.getenv("DATABASE_URL","").replace("postgresql+asyncpg","postgresql+psycopg2"),
        pool_pre_ping=True)
    return sessionmaker(bind=engine)()


# ── OAuth callback — receives ?code= from Instagram ───────────────────────────
@router.get("/callback")
async def instagram_callback(request: Request, code: str = None, error: str = None):
    """
    Instagram redirects here after OAuth login.
    Exchanges the code for a short-lived token, then upgrades to long-lived.
    """
    import httpx, json

    if error:
        log.error(f"Instagram OAuth error: {error}")
        return HTMLResponse(f"""
        <html><body style="font-family:sans-serif;padding:40px;background:#0a1628;color:#fff">
        <h2>❌ Instagram Auth Error</h2><p>{error}</p>
        <p>Close this window and try again from the admin console.</p>
        </body></html>""")

    if not code:
        return HTMLResponse("<html><body>No code received.</body></html>")

    db = _db()
    try:
        # Get stored credentials
        creds = {r.config_key: r.config_value for r in db.execute(
            __import__('sqlalchemy').text(
                "SELECT config_key, config_value FROM app_configs WHERE config_key IN "
                "('instagram_meta_app_id','instagram_app_secret','instagram_account_id')"
            )).fetchall()}

        app_id     = creds.get('instagram_meta_app_id','')
        app_secret = creds.get('instagram_app_secret','')
        redirect   = f"https://api.nexabuilder.com/api/auth/instagram/callback"

        if not app_secret:
            return HTMLResponse("<html><body>App secret not configured. Add instagram_app_secret to app_configs.</body></html>")

        # Exchange code for short-lived token
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.instagram.com/oauth/access_token", data={
                "client_id":     app_id,
                "client_secret": app_secret,
                "grant_type":    "authorization_code",
                "redirect_uri":  redirect,
                "code":          code,
            })
        token_data = r.json()
        log.info(f"Instagram short-lived token response: {r.status_code} {str(token_data)[:100]}")

        short_token  = token_data.get("access_token","")
        ig_user_id   = str(token_data.get("user_id",""))

        if not short_token:
            return HTMLResponse(f"<html><body>Token exchange failed: {token_data}</body></html>")

        # Exchange for long-lived token (60 days)
        async with httpx.AsyncClient(timeout=30) as c:
            r2 = await c.get(
                f"https://graph.instagram.com/access_token"
                f"?grant_type=ig_exchange_token"
                f"&client_secret={app_secret}"
                f"&access_token={short_token}"
            )
        long_data  = r2.json()
        long_token = long_data.get("access_token", short_token)
        expires_in = long_data.get("expires_in", 5183944)

        # Save to app_configs
        from sqlalchemy import text as sqlt
        db.execute(sqlt("""
            INSERT INTO app_configs (config_key, config_value) VALUES
              ('instagram_access_token', :token),
              ('instagram_account_id',  :uid),
              ('instagram_token_expires_in', :exp)
            ON CONFLICT (config_key) DO UPDATE SET
              config_value = EXCLUDED.config_value, updated_at = NOW()
        """), {"token": long_token, "uid": ig_user_id, "exp": str(expires_in)})
        db.commit()

        days = expires_in // 86400
        log.info(f"✓ Instagram long-lived token saved. Expires in {days} days. IG user: {ig_user_id}")

        return HTMLResponse(f"""
        <html><body style="font-family:sans-serif;padding:40px;background:#0a1628;color:#fff;text-align:center">
        <div style="font-size:48px">✓</div>
        <h2 style="color:#C8922A">Instagram Connected!</h2>
        <p>Long-lived token saved. Expires in <strong>{days} days</strong>.</p>
        <p style="color:#9ca3af">Instagram User ID: {ig_user_id}</p>
        <p style="color:#9ca3af;margin-top:24px">You can close this window.<br>
        The 📷 Instagram button in the Materials CMS is now active.</p>
        </body></html>""")

    except Exception as e:
        log.error(f"Instagram callback error: {e}")
        return HTMLResponse(f"<html><body>Error: {e}</body></html>")
    finally:
        db.close()


# ── Webhook verification (GET) ────────────────────────────────────────────────
@router.get("/webhook")
async def webhook_verify(request: Request):
    """Meta sends a GET to verify the webhook endpoint."""
    params = dict(request.query_params)
    mode      = params.get("hub.mode","")
    token     = params.get("hub.verify_token","")
    challenge = params.get("hub.challenge","")

    if mode == "subscribe" and token == IG_VERIFY_TOKEN:
        log.info("✓ Instagram webhook verified")
        return HTMLResponse(challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Webhook receiver (POST) ───────────────────────────────────────────────────
@router.post("/webhook")
async def webhook_receive(request: Request):
    """Receive Instagram webhook events (messages, comments, etc.)."""
    import json as _json

    body = await request.body()
    sig  = request.headers.get("x-hub-signature-256","")

    # Verify signature
    db = _db()
    try:
        secret_row = db.execute(__import__('sqlalchemy').text(
            "SELECT config_value FROM app_configs WHERE config_key='instagram_app_secret'"
        )).fetchone()
        if secret_row:
            expected = "sha256=" + hmac.new(
                secret_row[0].encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                raise HTTPException(status_code=403, detail="Invalid signature")
    finally:
        db.close()

    try:
        data = _json.loads(body)
        log.info(f"Instagram webhook event: {str(data)[:200]}")
    except Exception:
        pass

    return {"status": "ok"}


# ── Test current token ────────────────────────────────────────────────────────
@router.get("/me")
async def get_instagram_me(x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    import httpx

    db = _db()
    try:
        token_row = db.execute(__import__('sqlalchemy').text(
            "SELECT config_value FROM app_configs WHERE config_key='instagram_access_token'"
        )).fetchone()
        if not token_row:
            return {"error": "No Instagram token configured. Complete OAuth at /api/auth/instagram/callback"}

        token = token_row[0]
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"https://graph.instagram.com/v25.0/me"
                f"?fields=id,username,account_type,media_count"
                f"&access_token={token}"
            )
        return r.json()
    finally:
        db.close()
