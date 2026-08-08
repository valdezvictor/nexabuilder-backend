"""
keyword_rank_router.py — #20 Keyword Rank Tracking
GET  /api/rank/snapshot        — pull current GSC positions for target keywords
GET  /api/rank/history         — position history over time
GET  /api/rank/movers          — biggest position changes (winners + losers)
POST /api/rank/target-keywords — add a new target keyword
"""
import os, logging
from datetime import datetime, timedelta, date
from typing import Optional
from fastapi import APIRouter, Header, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import text as sqlt

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/rank", tags=["Keyword Rank Tracking"])
ADMIN_KEY = os.getenv("CMS_ADMIN_KEY", "")


def _req(key):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg", "postgresql+psycopg2"),
        pool_pre_ping=True)
    return sessionmaker(bind=engine)()


# ── 1. Snapshot — pull today's positions from gsc_keywords table ──────────────
@router.get("/snapshot")
async def take_snapshot(
    bg: BackgroundTasks,
    x_admin_key: str = Header(...)
):
    """
    Cross-references target_keywords against gsc_keywords (already synced daily).
    Writes today's position for each target keyword into keyword_rank_history.
    Safe to call multiple times — uses ON CONFLICT DO UPDATE.
    """
    _req(x_admin_key)
    bg.add_task(_run_snapshot)
    return {"status": "snapshot_queued", "message": "Rank snapshot running in background. Check /api/rank/history in ~30s."}


async def _run_snapshot():
    db = _db()
    try:
        today = date.today().isoformat()
        rows = db.execute(sqlt("""
            SELECT
                tk.id       AS keyword_id,
                tk.keyword,
                AVG(gk.position)::NUMERIC(6,1)  AS position,
                SUM(gk.impressions)              AS impressions,
                SUM(gk.clicks)                   AS clicks,
                CASE WHEN SUM(gk.impressions) > 0
                     THEN (SUM(gk.clicks)::FLOAT / SUM(gk.impressions))::NUMERIC(5,4)
                     ELSE 0 END                  AS ctr,
                MAX(gk.page)                     AS url
            FROM target_keywords tk
            LEFT JOIN gsc_keywords gk
                ON LOWER(gk.query) = LOWER(tk.keyword)
                AND gk.date_range IN ('last_28_days','last_7_days')
            GROUP BY tk.id, tk.keyword
        """)).fetchall()

        inserted = 0
        for row in rows:
            if row.position is None:
                continue
            try:
                db.execute(sqlt("""
                    INSERT INTO keyword_rank_history
                      (keyword_id, keyword, recorded_date, position,
                       impressions, clicks, ctr, url, source)
                    VALUES
                      (:kid, :kw, :dt, :pos, :imp, :clk, :ctr, :url, 'gsc')
                    ON CONFLICT (keyword_id, recorded_date, source)
                    DO UPDATE SET
                      position    = EXCLUDED.position,
                      impressions = EXCLUDED.impressions,
                      clicks      = EXCLUDED.clicks,
                      ctr         = EXCLUDED.ctr,
                      url         = EXCLUDED.url
                """), {
                    "kid": row.keyword_id, "kw": row.keyword,
                    "dt": today, "pos": float(row.position),
                    "imp": int(row.impressions or 0),
                    "clk": int(row.clicks or 0),
                    "ctr": float(row.ctr or 0),
                    "url": row.url
                })
                inserted += 1
            except Exception as e:
                log.warning(f"Rank insert error for {row.keyword}: {e}")

        db.commit()
        log.info(f"Rank snapshot: {inserted} keywords recorded for {today}")
    except Exception as e:
        log.error(f"Rank snapshot error: {e}")
        db.rollback()
    finally:
        db.close()


# ── 2. History — position over time for all or one keyword ────────────────────
@router.get("/history")
async def get_history(
    keyword:  Optional[str] = None,
    vertical: Optional[str] = None,
    days:     int = 30,
    x_admin_key: str = Header(...)
):
    _req(x_admin_key)
    db = _db()
    try:
        since = (date.today() - timedelta(days=days)).isoformat()
        where = ["h.recorded_date >= :since"]
        params = {"since": since}
        if keyword:
            where.append("LOWER(h.keyword) LIKE :kw")
            params["kw"] = f"%{keyword.lower()}%"
        if vertical:
            where.append("tk.vertical = :vert")
            params["vert"] = vertical

        rows = db.execute(sqlt(f"""
            SELECT
                h.keyword, h.recorded_date, h.position,
                h.impressions, h.clicks, h.ctr, h.url,
                tk.vertical, tk.priority, tk.target_position
            FROM keyword_rank_history h
            JOIN target_keywords tk ON tk.id = h.keyword_id
            WHERE {" AND ".join(where)}
            ORDER BY h.keyword, h.recorded_date DESC
        """), params).fetchall()

        # Group by keyword for easy charting
        grouped: dict = {}
        for r in rows:
            k = r.keyword
            if k not in grouped:
                grouped[k] = {
                    "keyword": k, "vertical": r.vertical,
                    "priority": r.priority, "target": r.target_position,
                    "history": []
                }
            grouped[k]["history"].append({
                "date": str(r.recorded_date),
                "position": float(r.position) if r.position else None,
                "impressions": r.impressions,
                "clicks": r.clicks
            })

        return {
            "period_days": days,
            "keywords": list(grouped.values()),
            "total": len(grouped)
        }
    finally:
        db.close()


# ── 3. Movers — biggest position changes ──────────────────────────────────────
@router.get("/movers")
async def get_movers(
    days: int = 7,
    x_admin_key: str = Header(...)
):
    """Compare current week vs prior week. Returns winners and losers."""
    _req(x_admin_key)
    db = _db()
    try:
        today      = date.today().isoformat()
        week_ago   = (date.today() - timedelta(days=days)).isoformat()
        prior_week = (date.today() - timedelta(days=days*2)).isoformat()

        rows = db.execute(sqlt("""
            WITH current_period AS (
                SELECT keyword_id, keyword,
                       AVG(position) AS pos_now,
                       SUM(impressions) AS imp_now,
                       SUM(clicks) AS clk_now
                FROM keyword_rank_history
                WHERE recorded_date >= :week_ago
                GROUP BY keyword_id, keyword
            ),
            prior_period AS (
                SELECT keyword_id,
                       AVG(position) AS pos_then
                FROM keyword_rank_history
                WHERE recorded_date >= :prior_week
                  AND recorded_date < :week_ago
                GROUP BY keyword_id
            ),
            combined AS (
                SELECT
                    c.keyword,
                    c.pos_now,
                    p.pos_then,
                    c.imp_now,
                    c.clk_now,
                    tk.vertical, tk.priority, tk.target_position,
                    CASE WHEN p.pos_then IS NOT NULL
                         THEN (p.pos_then - c.pos_now)
                         ELSE NULL
                    END AS position_change
                FROM current_period c
                JOIN target_keywords tk ON tk.id = c.keyword_id
                LEFT JOIN prior_period p ON p.keyword_id = c.keyword_id
            )
            SELECT * FROM combined
            ORDER BY position_change DESC NULLS LAST
        """), {"week_ago": week_ago, "prior_week": prior_week, "today": today}).fetchall()

        all_kws = [dict(r._mapping) for r in rows]
        winners = [r for r in all_kws if (r.get('position_change') or 0) > 0]
        losers  = [r for r in all_kws if (r.get('position_change') or 0) < 0]
        flat    = [r for r in all_kws if r.get('position_change') is None]

        return {
            "period_days": days,
            "winners": winners[:10],
            "losers":  losers[-10:],
            "no_change": flat[:5],
            "total_tracked": len(all_kws)
        }
    finally:
        db.close()


# ── 4. Add target keyword ─────────────────────────────────────────────────────
class KWPayload(BaseModel):
    keyword:         str
    vertical:        Optional[str] = None
    intent:          Optional[str] = "transactional"
    target_position: Optional[int] = 10
    priority:        Optional[str] = "medium"
    page_url:        Optional[str] = None
    notes:           Optional[str] = None


@router.post("/target-keywords")
async def add_keyword(payload: KWPayload, x_admin_key: str = Header(...)):
    _req(x_admin_key)
    db = _db()
    try:
        db.execute(sqlt("""
            INSERT INTO target_keywords
              (keyword, vertical, intent, target_position, priority, page_url, notes)
            VALUES
              (:kw, :vert, :intent, :target, :pri, :url, :notes)
            ON CONFLICT (keyword) DO UPDATE SET
              vertical = EXCLUDED.vertical,
              priority = EXCLUDED.priority,
              page_url = EXCLUDED.page_url
        """), {
            "kw": payload.keyword.lower().strip(),
            "vert": payload.vertical, "intent": payload.intent,
            "target": payload.target_position, "pri": payload.priority,
            "url": payload.page_url, "notes": payload.notes
        })
        db.commit()
        return {"status": "added", "keyword": payload.keyword}
    finally:
        db.close()


# ── 5. Summary — current rankings snapshot ───────────────────────────────────
@router.get("/summary")
async def get_summary(
    vertical: Optional[str] = None,
    x_admin_key: str = Header(...)
):
    """Current position for all target keywords, latest snapshot."""
    _req(x_admin_key)
    db = _db()
    try:
        params = {}
        where = ""
        if vertical:
            where = "AND tk.vertical = :vert"
            params["vert"] = vertical

        rows = db.execute(sqlt(f"""
            SELECT
                tk.keyword, tk.vertical, tk.priority,
                tk.target_position,
                h.position       AS current_position,
                h.impressions,
                h.clicks,
                h.recorded_date  AS last_updated,
                CASE
                    WHEN h.position IS NULL THEN 'not_tracking'
                    WHEN h.position <= tk.target_position THEN 'on_target'
                    WHEN h.position <= tk.target_position + 10 THEN 'close'
                    ELSE 'needs_work'
                END AS status
            FROM target_keywords tk
            LEFT JOIN LATERAL (
                SELECT position, impressions, clicks, recorded_date
                FROM keyword_rank_history
                WHERE keyword_id = tk.id
                ORDER BY recorded_date DESC
                LIMIT 1
            ) h ON TRUE
            WHERE 1=1 {where}
            ORDER BY tk.priority DESC, h.position ASC NULLS LAST
        """), params).fetchall()

        return {
            "keywords": [dict(r._mapping) for r in rows],
            "total": len(rows),
            "on_target": sum(1 for r in rows if r.status == 'on_target'),
            "no_data":   sum(1 for r in rows if r.status == 'not_tracking'),
        }
    finally:
        db.close()
