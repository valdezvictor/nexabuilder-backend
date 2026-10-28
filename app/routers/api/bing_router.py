"""
bing_router.py — Bing Webmaster Tools integration for NexaBuilder
Mirrors the GSC router pattern.

Endpoints:
  POST /api/bing/set-key         - Store Bing API key
  POST /api/bing/sync            - Pull keyword data from Bing API → DB
  GET  /api/bing/keywords        - Return cached Bing keyword data
  GET  /api/bing/keywords/top    - Top queries by impressions
"""
import os, logging
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Header, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import text as sqlt

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/bing", tags=["Bing Search"])

ADMIN_KEY = os.getenv("CMS_ADMIN_KEY", "")
BING_SITE = "https://nexabuilder.com/"


def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True


def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg", "postgresql+psycopg2"),
        echo=False, pool_pre_ping=True
    )
    return sessionmaker(bind=engine)()


def _get_bing_key():
    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT config_value FROM app_configs WHERE config_key='bing_api_key' LIMIT 1"
        )).fetchone()
        return row[0] if row and row[0] else None
    finally:
        db.close()


def _infer_vertical(page: str) -> str:
    mapping = {
        "pool": "pool", "piscina": "pool",
        "roof": "roofing", "location": "local",
        "remodel": "remodeling", "kitchen": "remodeling",
        "electric": "electrical", "plumb": "plumbing",
        "hvac": "hvac", "landscap": "landscaping",
        "material": "materials", "blog": "content",
    }
    p = (page or "").lower()
    for k, v in mapping.items():
        if k in p:
            return v
    return "general"


# ── Key management ────────────────────────────────────────────────────────────

class KeyRequest(BaseModel):
    api_key: str


@router.post("/set-key")
async def set_bing_key(payload: KeyRequest, _: bool = Depends(require_admin)):
    """Store the Bing Webmaster API key from bingwebmaster.com → Settings → API Access."""
    db = _db()
    try:
        db.execute(sqlt("""
            INSERT INTO app_configs (config_key, config_value, updated_at)
            VALUES ('bing_api_key', :k, NOW())
            ON CONFLICT (config_key) DO UPDATE SET config_value=:k, updated_at=NOW()
        """), {"k": payload.api_key})
        db.commit()
        return {"status": "bing_key_stored"}
    finally:
        db.close()


# ── Sync keyword data ─────────────────────────────────────────────────────────

@router.post("/sync")
async def sync_bing_keywords(bg: BackgroundTasks, _: bool = Depends(require_admin)):
    key = _get_bing_key()
    if not key:
        raise HTTPException(status_code=503,
            detail="Bing API key not set. POST /api/bing/set-key first.")
    bg.add_task(_run_bing_sync, key)
    return {"status": "syncing", "message": "Bing keyword sync started in background"}


async def _run_bing_sync(api_key: str):
    """Pull top queries from Bing Search Performance API."""
    import httpx
    db = _db()
    try:
        end_date   = datetime.utcnow().strftime("%Y-%m-%d")
        start_date = (datetime.utcnow() - timedelta(days=28)).strftime("%Y-%m-%d")

        headers = {"apiKey": api_key}

        async with httpx.AsyncClient(timeout=30) as c:
            # Get query stats for the site
            r = await c.get(
                "https://ssl.bing.com/webmaster/api.svc/json/GetQueryStats",
                params={
                    "siteUrl":    BING_SITE,
                    "startDate":  start_date,
                    "endDate":    end_date,
                    "apiKey":     api_key,
                }
            )

        if not r.is_success:
            log.error(f"Bing API error: {r.status_code} {r.text[:200]}")
            return

        data = r.json()
        rows = data.get("d", [])
        log.info(f"Bing sync: {len(rows)} rows received")

        inserted = 0
        for row in rows:
            query    = row.get("Query", "")
            page     = row.get("Url", BING_SITE)
            clicks   = int(row.get("Clicks", 0))
            imps     = int(row.get("Impressions", 0))
            position = float(row.get("AvgImpressionPosition", 0))
            ctr      = float(row.get("Ctr", 0)) / 100  # Bing returns percentage
            vertical = _infer_vertical(page)

            if not query:
                continue

            try:
                db.execute(sqlt("""
                    INSERT INTO gsc_keywords
                      (query, page, clicks, impressions, ctr, position,
                       date_range, vertical, synced_at)
                    VALUES (:q, :p, :c, :i, :ctr, :pos, :dr, :v, NOW())
                    ON CONFLICT (query, page, date_range) DO UPDATE SET
                      clicks=GREATEST(gsc_keywords.clicks, :c),
                      impressions=GREATEST(gsc_keywords.impressions, :i),
                      ctr=:ctr, position=:pos,
                      vertical=:v, synced_at=NOW()
                """), {
                    "q": query[:500], "p": page[:500],
                    "c": clicks, "i": imps,
                    "ctr": ctr, "pos": position,
                    "dr": "last_28_days_bing",
                    "v": vertical
                })
                inserted += 1
            except Exception as e:
                log.warning(f"Bing row insert error: {e}")

        db.commit()
        log.info(f"Bing sync complete: {inserted} rows upserted")

    except Exception as e:
        log.error(f"Bing sync error: {e}")
        db.rollback()
    finally:
        db.close()


# ── Read keyword data ─────────────────────────────────────────────────────────

@router.get("/keywords/top")
async def get_bing_top_keywords(_: bool = Depends(require_admin)):
    """Top Bing queries — stored in gsc_keywords with date_range='last_28_days_bing'."""
    db = _db()
    try:
        rows = db.execute(sqlt("""
            SELECT query, SUM(clicks) as clicks, SUM(impressions) as impressions,
                   AVG(position)::numeric(6,1) as avg_position, vertical
            FROM gsc_keywords
            WHERE date_range = 'last_28_days_bing'
            GROUP BY query, vertical
            ORDER BY impressions DESC
            LIMIT 25
        """)).fetchall()
        last_sync = db.execute(sqlt(
            "SELECT MAX(synced_at) FROM gsc_keywords WHERE date_range='last_28_days_bing'"
        )).scalar()
        total = db.execute(sqlt(
            "SELECT COUNT(DISTINCT query) FROM gsc_keywords WHERE date_range='last_28_days_bing'"
        )).scalar()
        key_set = _get_bing_key() is not None
        return {
            "connected":    key_set,
            "last_synced":  last_sync.isoformat() if last_sync else None,
            "total_queries": total,
            "top_queries":  [dict(r._mapping) for r in rows],
        }
    finally:
        db.close()


@router.get("/status")
async def bing_status(_: bool = Depends(require_admin)):
    key = _get_bing_key()
    total = 0
    last_sync = None
    if key:
        db = _db()
        try:
            total = db.execute(sqlt(
                "SELECT COUNT(*) FROM gsc_keywords WHERE date_range='last_28_days_bing'"
            )).scalar()
            last_sync = db.execute(sqlt(
                "SELECT MAX(synced_at) FROM gsc_keywords WHERE date_range='last_28_days_bing'"
            )).scalar()
        finally:
            db.close()
    return {
        "connected":     key is not None,
        "keyword_count": total,
        "last_synced":   last_sync.isoformat() if last_sync else None,
        "site":          BING_SITE,
    }
