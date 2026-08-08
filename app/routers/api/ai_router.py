"""
ai_router.py — NexaBuilder AI proxy endpoints
Proxies Claude API calls server-side to avoid CORS restrictions in the admin browser.
"""
import os, logging
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai", tags=["AI"])

ADMIN_KEY    = os.getenv("CMS_ADMIN_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")


def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True


class SEOInsightRequest(BaseModel):
    query:       str
    page:        str | None = None
    impressions: int = 0
    clicks:      int = 0
    position:    float = 0.0
    vertical:    str | None = "general"


@router.post("/seo-insights")
async def seo_insights(payload: SEOInsightRequest,
                        x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not ANTHROPIC_KEY:
        raise HTTPException(status_code=503,
            detail="ANTHROPIC_API_KEY not configured on server")

    import httpx

    pos = payload.position
    pos_label = (
        "Top 3" if pos <= 3 else
        "Page 1 (positions 4-10)" if pos <= 10 else
        "Page 2 (positions 11-20)" if pos <= 20 else
        f"Pages 3-5 (position {pos:.0f})" if pos <= 50 else
        f"Page 6+ (position {pos:.0f})"
    )
    ctr = f"{(payload.clicks / payload.impressions * 100):.2f}%" if payload.impressions > 0 else "0%"

    prompt = f"""You are an expert SEO consultant specializing in local contractor and home services websites in Southern California. Analyze this Google Search Console data point and provide specific, actionable recommendations.

QUERY DATA:
- Search Query: "{payload.query}"
- Landing Page: {payload.page or "nexabuilder.com"}
- Current Position: {pos:.1f} ({pos_label})
- Impressions (28 days): {payload.impressions}
- Clicks (28 days): {payload.clicks}
- CTR: {ctr}
- Vertical: {payload.vertical or "general"}

CONTEXT:
NexaBuilder is a CSLB-verified contractor-matching platform serving Southern California homeowners. The site has location pages, service pages (/services/{{vertical}}/), material gallery pages (/materials/{{category}}/{{item}}/), and blog articles. Content is bilingual EN/ES.

Provide your analysis in this exact structure:

## Why This Position
2-3 sentences explaining the current position and most likely reasons for it — be specific to this query and page.

## Quick Wins (Do This Week)
3 specific actions that can move the needle within 1-2 weeks. Name the exact page element to change (title tag, H1, meta description, body copy, internal links, schema, etc.).

## Content Gaps
2-3 specific content topics or questions this page should answer that it probably doesn't, based on the search intent.

## Internal Linking Opportunity
1-2 specific internal links that should point to this page from other pages on nexabuilder.com, and what anchor text to use.

## CMS Action
One precise thing to update in the CMS right now — be exact (e.g. "Update the H1 from X to Y").

Keep each section tight — no filler, no generic advice. Every recommendation must be specific to this query and page."""

    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}]
            }
        )

    if not r.is_success:
        log.error(f"Anthropic API error: {r.status_code} {r.text[:200]}")
        raise HTTPException(status_code=502,
            detail=f"AI service error: {r.status_code}")

    data = r.json()
    text = next((b["text"] for b in data.get("content", []) if b.get("type") == "text"), "")
    return {"insight": text, "model": data.get("model",""), "usage": data.get("usage",{})}
