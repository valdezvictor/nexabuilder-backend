# app/routers/api/revenue.py
# Revenue dashboard endpoints — reads from revenue_events + leads tables
from typing import Optional
from fastapi import APIRouter, Query
from sqlalchemy import text
from app.db import get_sessionmaker

router = APIRouter(prefix="/api/revenue", tags=["Revenue"])

def _range_clause(range_val: str) -> str:
    if range_val == "7":
        return "AND re.created_at >= NOW() - INTERVAL '7 days'"
    elif range_val == "30":
        return "AND re.created_at >= NOW() - INTERVAL '30 days'"
    elif range_val == "90":
        return "AND re.created_at >= NOW() - INTERVAL '90 days'"
    return ""  # all time

@router.get("/kpis")
async def get_kpis(range: str = Query("30")):
    rc = _range_clause(range)
    SL = get_sessionmaker()
    async with SL() as db:
        row = (await db.execute(text(f"""
            SELECT
                COALESCE(SUM(re.revenue_usd), 0)                AS total_revenue,
                COUNT(DISTINCT re.lead_id)                      AS leads_sold,
                (SELECT COUNT(*) FROM leads l
                 WHERE 1=1 {rc.replace('re.', 'l.')})           AS total_leads,
                COALESCE(AVG(re.bid_amount_usd), 0)             AS avg_bid,
                (SELECT network_slug FROM revenue_events
                 WHERE revenue_usd > 0 {rc.replace('AND re.', 'AND ')}
                 GROUP BY network_slug
                 ORDER BY SUM(revenue_usd) DESC LIMIT 1)        AS top_network
            FROM revenue_events re
            WHERE re.revenue_usd > 0 {rc}
        """))).fetchone()

        total_rev   = float(row[0] or 0)
        leads_sold  = int(row[1] or 0)
        total_leads = int(row[2] or 0)
        avg_bid     = float(row[3] or 0)
        top_network = row[4] or "—"
        conv_rate   = (leads_sold / total_leads * 100) if total_leads > 0 else 0.0

    return {
        "total_revenue":   round(total_rev, 2),
        "leads_sold":      leads_sold,
        "total_leads":     total_leads,
        "avg_bid":         round(avg_bid, 2),
        "conversion_rate": round(conv_rate, 1),
        "top_network":     top_network,
    }

@router.get("/by-network")
async def by_network(range: str = Query("30")):
    rc = _range_clause(range)
    SL = get_sessionmaker()
    async with SL() as db:
        rows = (await db.execute(text(f"""
            SELECT
                COALESCE(network_slug, 'internal') AS network_slug,
                COUNT(*)                           AS leads,
                COALESCE(SUM(revenue_usd), 0)      AS revenue,
                COALESCE(AVG(bid_amount_usd), 0)   AS avg_bid
            FROM revenue_events
            WHERE revenue_usd > 0 {rc.replace('re.', '')}
            GROUP BY network_slug
            ORDER BY revenue DESC
        """))).fetchall()
    return [{"network_slug": r[0], "leads": int(r[1]), "revenue": round(float(r[2]),2), "avg_bid": round(float(r[3]),2)} for r in rows]

@router.get("/by-vertical")
async def by_vertical(range: str = Query("30")):
    rc = _range_clause(range)
    SL = get_sessionmaker()
    async with SL() as db:
        rows = (await db.execute(text(f"""
            SELECT
                COALESCE(vertical, 'general') AS vertical,
                COUNT(*)                      AS leads,
                COALESCE(SUM(revenue_usd), 0) AS revenue,
                COALESCE(AVG(bid_amount_usd), 0) AS avg_bid
            FROM revenue_events
            WHERE revenue_usd > 0 {rc.replace('re.', '')}
            GROUP BY vertical
            ORDER BY revenue DESC
        """))).fetchall()
    return [{"vertical": r[0], "leads": int(r[1]), "revenue": round(float(r[2]),2), "avg_bid": round(float(r[3]),2)} for r in rows]

@router.get("/daily")
async def daily(range: str = Query("30")):
    days = {"7": 7, "30": 30, "90": 90, "all": 365}.get(range, 30)
    SL = get_sessionmaker()
    async with SL() as db:
        rows = (await db.execute(text(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('day', created_at), 'Mon DD') AS day,
                COUNT(*)                       AS leads,
                COALESCE(SUM(revenue_usd), 0)  AS revenue
            FROM revenue_events
            WHERE created_at >= NOW() - INTERVAL '{days} days'
            GROUP BY DATE_TRUNC('day', created_at)
            ORDER BY DATE_TRUNC('day', created_at)
        """))).fetchall()
    return [{"day": r[0], "leads": int(r[1]), "revenue": round(float(r[2]),2)} for r in rows]

@router.get("/events")
async def events(range: str = Query("30"), limit: int = Query(20, le=100)):
    rc = _range_clause(range)
    SL = get_sessionmaker()
    async with SL() as db:
        rows = (await db.execute(text(f"""
            SELECT
                re.created_at, re.network_slug, re.partner_slug,
                re.vertical, re.lead_score, re.revenue_usd,
                re.bid_amount_usd, re.status, re.routing
            FROM revenue_events re
            WHERE 1=1 {rc}
            ORDER BY re.created_at DESC
            LIMIT :limit
        """), {"limit": limit})).fetchall()
    return [{
        "created_at":    str(r[0]),
        "network_slug":  r[1],
        "partner_slug":  r[2],
        "vertical":      r[3],
        "lead_score":    r[4],
        "revenue_usd":   float(r[5] or 0),
        "bid_amount_usd":float(r[6] or 0),
        "status":        r[7],
        "routing":       r[8],
    } for r in rows]
