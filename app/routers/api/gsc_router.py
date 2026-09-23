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


# ─── MULTI-SITE GSC SUPPORT ──────────────────────────────────────────────────

@router.post("/import-csv")
async def import_gsc_csv(payload: dict, _: bool = Depends(require_admin)):
    """Import GSC query/page data from CSV text.
    Accepts: {domain, rows: [{query, page, clicks, impressions, ctr, position}]}
    """
    import re as _re
    domain = payload.get("domain", "nexabuilder.com")
    rows   = payload.get("rows", [])
    if not rows:
        raise HTTPException(400, "No rows provided")

    def _infer_v(s):
        s = (s or "").lower()
        for k, v in [
            ("pool","pool"),("piscina","pool"),("swimming","pool"),
            ("roof","roofing"),("rufero","roofing"),
            ("remodel","remodeling"),("bath","remodeling"),("kitchen","remodeling"),
            ("electric","electrical"),("electricista","electrical"),
            ("plumb","plumbing"),("plomero","plumbing"),
            ("hvac","hvac"),("tecnico","hvac"),
            ("landscap","landscaping"),("jardinero","landscaping"),
            ("stone","materials"),("material","materials"),
            ("unapiscina","pool"),("piscinasy","pool"),("swimmingpul","pool"),
            ("losruf","roofing"),("ijardinero","landscaping"),("eelectricista","electrical"),
        ]:
            if k in s: return v
        return "general"

    db = _db()
    inserted = 0
    errors   = 0
    try:
        for row in rows:
            query = str(row.get("query",""))[:500]
            page  = str(row.get("page", f"https://{domain}/"))[:500]
            try:
                clicks = int(float(str(row.get("clicks",0) or 0)))
                impr   = int(float(str(row.get("impressions",0) or 0)))
                ctr_v  = str(row.get("ctr","0")).replace("%","")
                ctr    = float(ctr_v)/100 if float(ctr_v) > 1 else float(ctr_v)
                pos    = float(str(row.get("position",0) or 0))
            except:
                errors += 1; continue

            vertical = _infer_v(page + " " + query)
            try:
                db.execute(sqlt("""
                    INSERT INTO gsc_keywords
                      (query,page,clicks,impressions,ctr,position,date_range,vertical,domain,synced_at)
                    VALUES (:q,:p,:c,:i,:ctr,:pos,'csv_import',:v,:d,NOW())
                    ON CONFLICT (query,page,date_range) DO UPDATE SET
                      clicks=GREATEST(gsc_keywords.clicks,EXCLUDED.clicks),
                      impressions=GREATEST(gsc_keywords.impressions,EXCLUDED.impressions),
                      ctr=EXCLUDED.ctr,position=EXCLUDED.position,
                      vertical=EXCLUDED.vertical,domain=EXCLUDED.domain,synced_at=NOW()
                """), {"q":query,"p":page,"c":clicks,"i":impr,
                       "ctr":ctr,"pos":pos,"v":vertical,"d":domain})
                inserted += 1
            except Exception as e:
                log.warning(f"CSV import row error: {e}")
                errors += 1
        db.commit()
        # Update last_synced_at in gsc_sites
        db.execute(sqlt(
            "UPDATE gsc_sites SET last_synced_at=NOW() WHERE domain=:d"
        ), {"d": domain})
        db.commit()
        return {"ok": True, "domain": domain, "inserted": inserted, "errors": errors}
    finally:
        db.close()


@router.get("/sites")
async def list_gsc_sites(_: bool = Depends(require_admin)):
    """List all registered GSC properties and their sync status."""
    db = _db()
    try:
        sites = db.execute(sqlt(
            "SELECT domain, gsc_property, last_synced_at, is_active, "
            "(SELECT COUNT(*) FROM gsc_keywords k WHERE k.domain=s.domain) as row_count "
            "FROM gsc_sites s ORDER BY domain"
        )).fetchall()
        return {"sites": [dict(r._mapping) for r in sites]}
    finally:
        db.close()


@router.post("/sync-all")
async def sync_all_gsc(bg: BackgroundTasks, _: bool = Depends(require_admin)):
    """Trigger GSC sync for all active properties that share the same OAuth token."""
    token_json = _get_gsc_token()
    if not token_json:
        raise HTTPException(503, "GSC not connected. Visit /api/gsc/authorize first.")
    db = _db()
    try:
        sites = db.execute(sqlt(
            "SELECT domain, gsc_property FROM gsc_sites WHERE is_active=TRUE"
        )).fetchall()
    finally:
        db.close()
    for site in sites:
        bg.add_task(_run_gsc_sync_domain, token_json, site[0], site[1])
    return {"status": "syncing", "properties": [s[0] for s in sites]}


async def _run_gsc_sync_domain(token_json: str, domain: str, gsc_property: str):
    """Pull top 500 queries for a specific GSC property into gsc_keywords."""
    import httpx, json as _j
    db = _db()
    try:
        tokens = _j.loads(token_json)
        access_token = tokens.get("access_token","")
        # Refresh if needed
        if tokens.get("refresh_token"):
            cid = os.getenv("GOOGLE_CLIENT_ID","")
            cs  = os.getenv("GOOGLE_CLIENT_SECRET","")
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post("https://oauth2.googleapis.com/token", data={
                    "client_id":cid,"client_secret":cs,
                    "refresh_token":tokens["refresh_token"],"grant_type":"refresh_token"
                })
                if r.is_success:
                    tokens.update(r.json())
                    access_token = tokens.get("access_token", access_token)

        end_dt   = datetime.utcnow().strftime("%Y-%m-%d")
        start_dt = (datetime.utcnow() - timedelta(days=28)).strftime("%Y-%m-%d")

        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"https://searchconsole.googleapis.com/webmasters/v3/sites/{gsc_property}/searchAnalytics/query",
                headers={"Authorization": f"Bearer {access_token}"},
                json={"startDate":start_dt,"endDate":end_dt,
                      "dimensions":["query","page"],"rowLimit":500}
            )
        if not r.is_success:
            log.error(f"GSC sync failed for {domain}: {r.status_code} {r.text[:100]}")
            return

        rows = r.json().get("rows",[])
        log.info(f"GSC sync {domain}: {len(rows)} rows")
        inserted = 0
        for row in rows:
            keys  = row.get("keys",[])
            query = keys[0] if keys else ""
            page  = keys[1] if len(keys)>1 else f"https://{domain}/"
            vertical = _infer_vertical(page + " " + query)
            try:
                db.execute(sqlt("""
                    INSERT INTO gsc_keywords
                      (query,page,clicks,impressions,ctr,position,date_range,vertical,domain,synced_at)
                    VALUES (:q,:p,:c,:i,:ctr,:pos,'last_28_days',:v,:d,NOW())
                    ON CONFLICT (query,page,date_range) DO UPDATE SET
                      clicks=EXCLUDED.clicks,impressions=EXCLUDED.impressions,
                      ctr=EXCLUDED.ctr,position=EXCLUDED.position,
                      vertical=EXCLUDED.vertical,domain=EXCLUDED.domain,synced_at=NOW()
                """), {"q":query[:500],"p":page[:500],
                       "c":int(row.get("clicks",0)),"i":int(row.get("impressions",0)),
                       "ctr":float(row.get("ctr",0)),"pos":float(row.get("position",0)),
                       "v":vertical,"d":domain})
                inserted += 1
            except Exception as e:
                log.warning(f"Row error {domain}: {e}")
        db.commit()
        db.execute(sqlt("UPDATE gsc_sites SET last_synced_at=NOW() WHERE domain=:d"),{"d":domain})
        db.commit()
        log.info(f"GSC sync complete {domain}: {inserted} rows")
    except Exception as e:
        log.error(f"GSC sync error {domain}: {e}")
        db.rollback()
    finally:
        db.close()


# ─── INDEX COVERAGE / CRITICAL ISSUES ────────────────────────────────────────

@router.get("/coverage")
async def get_coverage(domain: str = "nexabuilder.com", _: bool = Depends(require_admin)):
    """Return GSC index coverage issues for a domain."""
    db = _db()
    try:
        rows = db.execute(sqlt(
            "SELECT * FROM gsc_index_coverage WHERE domain=:d ORDER BY "
            "CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, page_count DESC"
        ), {"d": domain}).fetchall()
        total_not_indexed = db.execute(sqlt(
            "SELECT COALESCE(SUM(page_count),0) FROM gsc_index_coverage "
            "WHERE domain=:d AND reason NOT LIKE '%canonical%' AND reason NOT LIKE '%redirect%' "
            "AND reason NOT LIKE '%noindex%'"
        ), {"d": domain}).fetchone()[0]
        return {
            "domain": domain,
            "issues": [dict(r._mapping) for r in rows],
            "total_issues": len(rows),
            "total_not_indexed": int(total_not_indexed),
        }
    finally:
        db.close()


@router.patch("/coverage/{issue_id}")
async def update_coverage_issue(issue_id: int, payload: dict, _: bool = Depends(require_admin)):
    """Update status/notes/priority of a coverage issue."""
    db = _db()
    try:
        allowed = {"status", "notes", "priority", "validation"}
        updates = {k: v for k, v in payload.items() if k in allowed}
        if not updates:
            raise HTTPException(400, "No valid fields to update")
        set_clause = ", ".join(f"{k}=:{k}" for k in updates)
        updates["id"] = issue_id
        db.execute(sqlt(f"UPDATE gsc_index_coverage SET {set_clause}, updated_at=NOW() WHERE id=:id"), updates)
        db.commit()
        return {"ok": True, "id": issue_id, "updated": list(updates.keys())}
    finally:
        db.close()


@router.post("/coverage/ai-fix/{issue_id}")
async def ai_fix_coverage(issue_id: int, _: bool = Depends(require_admin)):
    """Run Claude AI analysis on a coverage issue and suggest specific fixes."""
    import httpx as _hx, os as _os
    db = _db()
    try:
        row = db.execute(sqlt("SELECT * FROM gsc_index_coverage WHERE id=:id"), {"id": issue_id}).fetchone()
        if not row:
            raise HTTPException(404, "Issue not found")
        issue = dict(row._mapping)
    finally:
        db.close()

    key = _os.environ.get("ANTHROPIC_API_KEY", "")
    PRIORITIES = {
        "Discovered - currently not indexed": "Google found these pages but chose not to index them — usually thin content, duplicate content, or poor signals. These are the highest-value pages to fix.",
        "Crawled - currently not indexed": "Google crawled these pages recently and decided not to index them. Likely thin content or low E-E-A-T signals.",
        "Not found (404)": "Pages returning 404. Fix immediately — any links pointing here are wasted.",
        "Blocked due to access forbidden (403)": "Pages returning 403. Check server config.",
        "Blocked by robots.txt": "Pages blocked by robots.txt — verify these should be blocked.",
        "Excluded by noindex tag": "Pages with noindex meta tag. Verify all 115 are intentionally excluded.",
        "Alternate page with proper canonical tag": "Duplicate pages where canonical correctly points elsewhere. Usually not a problem unless the canonical is wrong.",
        "Duplicate without user-selected canonical": "Duplicate pages with no canonical tag. Add canonical tags immediately.",
    }
    context = PRIORITIES.get(issue["reason"], "")
    prompt = f"""You are an SEO technical specialist for NexaBuilder (nexabuilder.com), a CSLB-verified contractor-matching platform for Southern California homeowners.

COVERAGE ISSUE:
Domain: {issue["domain"]}
Issue Type: {issue["reason"]}
Source: {issue["source"]}
Affected Pages: {issue["page_count"]}
Current Status: {issue["status"]}
Context: {context}

SITE CONTEXT:
- Static HTML pages served from S3/CloudFront
- Service pages at /services/[vertical]/
- Materials pages at /materials/[category]/[item]/
- Blog at /blog/[slug]/
- Locations at /locations/[city]/
- Admin CMS manages content directly

Provide a specific action plan with:

## Root Cause
What is most likely causing this issue for this specific site.

## Exact Fix Steps
Numbered list of exact steps to resolve — be specific to NexaBuilder's stack (S3, CloudFront, static HTML).

## CMS Action
What to update in the NexaBuilder admin CMS right now.

## Expected Timeline
When Google should re-index after the fix.

## Prevention
How to prevent this from recurring."""

    async with _hx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 800,
                  "messages": [{"role": "user", "content": prompt}]})
    text = next((b["text"] for b in r.json().get("content", []) if b.get("type") == "text"), "")

    # Save analysis to notes
    db = _db()
    try:
        db.execute(sqlt("UPDATE gsc_index_coverage SET notes=:n, updated_at=NOW() WHERE id=:id"),
                   {"n": text[:2000], "id": issue_id})
        db.commit()
    finally:
        db.close()
    return {"insight": text, "issue_id": issue_id}
