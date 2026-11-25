"""
Admin Brand Perception Router
GET  /api/admin/brand-perception/summary     — latest week's rollup stats
GET  /api/admin/brand-perception/history     — week-by-week trend (last 12 weeks)
GET  /api/admin/brand-perception/raw         — recent raw rows (for gap analysis)
POST /api/admin/brand-perception/probe       — trigger Lambda manually (async)
"""

from __future__ import annotations

import logging
from typing import Optional

import json as _json
import asyncpg
import boto3
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-brand-perception"])

BRAND_LAMBDA = "nexabuilder-brand-perception"


def _get_db_url() -> str:
    return settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


async def require_admin():
    return True  # Enforced at nginx/CloudFront layer


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/brand-perception/summary")
async def get_brand_perception_summary(_: bool = Depends(require_admin)):
    """Latest week's brand perception rollup from weekly_seo_reports.report_json."""
    conn = await asyncpg.connect(_get_db_url())
    try:
        row = await conn.fetchrow(
            """
            SELECT week_ending, report_json, delivered_at
            FROM weekly_seo_reports
            WHERE report_json IS NOT NULL
            ORDER BY week_ending DESC
            LIMIT 1
            """
        )
        if not row:
            return {
                "status": "no_data",
                "message": "No brand perception data yet. Trigger a probe to populate.",
            }

        rj_raw = row["report_json"]; rj = _json.loads(rj_raw) if isinstance(rj_raw, str) else (rj_raw or {})
        bp = rj.get("brand_perception", {})

        mention_rate = float(bp.get("brand_mention_rate") or 0)
        mentioned_count = int(bp.get("brand_mentioned_count") or 0)
        total_queries = int(bp.get("total_queries") or 0)
        sentiment_score = round(mention_rate * 100)

        # Sentiment breakdown
        sb = bp.get("sentiment_breakdown", {})
        positive = int(sb.get("positive") or 0)
        neutral = int(sb.get("neutral") or 0)
        negative = int(sb.get("negative") or 0)

        overall_sentiment = (
            "positive" if mention_rate >= 0.3
            else "neutral" if mention_rate >= 0.1
            else "low"
        )

        return {
            "status": "ok",
            "week_ending": str(row["week_ending"]),
            "delivered_at": row["delivered_at"].isoformat() if row.get("delivered_at") else None,
            "brand": "NexaBuilder",
            "overall_sentiment": overall_sentiment,
            "sentiment_score": sentiment_score,
            "mention_count": mentioned_count,
            "mention_rate": mention_rate,
            "total_queries": total_queries,
            "sentiment_breakdown": {
                "positive": positive,
                "neutral": neutral,
                "negative": negative,
            },
            "top_competitors": bp.get("top_competitors", []),
            "gap_queries": bp.get("gap_queries", []),
            "last_updated": str(row["week_ending"]),
        }
    finally:
        await conn.close()


@router.get("/brand-perception/history")
async def get_brand_perception_history(
    weeks: int = Query(12, le=52),
    _: bool = Depends(require_admin),
):
    """Week-over-week brand mention rate trend."""
    conn = await asyncpg.connect(_get_db_url())
    try:
        rows = await conn.fetch(
            """
            SELECT week_ending,
                   (report_json->'brand_perception'->>'brand_mention_rate')::float AS mention_rate,
                   (report_json->'brand_perception'->>'brand_mentioned_count')::int AS mentioned_count,
                   (report_json->'brand_perception'->>'total_queries')::int AS total_queries
            FROM weekly_seo_reports
            WHERE report_json IS NOT NULL
            ORDER BY week_ending DESC
            LIMIT $1
            """,
            weeks,
        )
        return [
            {
                "week_ending": str(r["week_ending"]),
                "mention_rate": float(r["mention_rate"] or 0),
                "mentioned_count": int(r["mentioned_count"] or 0),
                "total_queries": int(r["total_queries"] or 0),
            }
            for r in reversed(rows)
        ]
    finally:
        await conn.close()


@router.get("/brand-perception/raw")
async def get_brand_perception_raw(
    limit: int = Query(50, le=200),
    source: Optional[str] = Query(None),
    gap_only: bool = Query(False, description="Only rows where brand was NOT mentioned"),
    _: bool = Depends(require_admin),
):
    """Raw ai_brand_perception rows — useful for content gap analysis."""
    conn = await asyncpg.connect(_get_db_url())
    try:
        # Check if ai_brand_perception table exists
        tbl = await conn.fetchval(
            "SELECT to_regclass('public.ai_brand_perception')"
        )
        if not tbl:
            return []

        conditions = []
        params: list = []
        if source:
            params.append(source)
            conditions.append(f"ai_source = ${len(params)}")
        if gap_only:
            conditions.append("brand_mentioned = false")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = await conn.fetch(
            f"""
            SELECT id, ai_source, query, brand_mentioned, sentiment,
                   competitor_mentioned, gap_notes, checked_at
            FROM ai_brand_perception
            {where}
            ORDER BY checked_at DESC
            LIMIT ${len(params)}
            """,
            *params,
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@router.post("/brand-perception/probe")
async def trigger_brand_perception_probe(_: bool = Depends(require_admin)):
    """Fire the brand perception Lambda asynchronously."""
    lambda_client = boto3.client("lambda", region_name="us-west-1")
    try:
        resp = lambda_client.invoke(
            FunctionName=BRAND_LAMBDA,
            InvocationType="Event",
            Payload=b"{}",
        )
        return {
            "status": "triggered",
            "lambda": BRAND_LAMBDA,
            "status_code": resp["StatusCode"],
        }
    except lambda_client.exceptions.ResourceNotFoundException:
        raise HTTPException(
            status_code=503,
            detail=f"Lambda {BRAND_LAMBDA} not found.",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
