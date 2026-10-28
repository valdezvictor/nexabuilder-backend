# app/routers/api/keyword_research.py
# Proxy to Anthropic API for keyword research — avoids CORS in admin console
import os, httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/keywords", tags=["Keyword Research"])

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

class KeywordRequest(BaseModel):
    vertical: str
    seed_keyword: str
    language: str = "en"

@router.post("/research")
async def research_keywords(payload: KeywordRequest):
    if not ANTHROPIC_KEY:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured")

    lang = "Spanish (Mexican/SoCal dialect)" if payload.language == "es" else "English"

    prompt = f"""You are a keyword research expert for a California home improvement lead generation site targeting homeowners in Southern California.

Vertical: {payload.vertical}
Seed keyword: "{payload.seed_keyword}"
Language: {lang}
Market: Southern California homeowners, ages 30-65

Return ONLY valid JSON (no markdown, no preamble):
{{
  "primary_keyword": "the single best keyword to target",
  "search_volume_estimate": "monthly searches estimate (e.g. 2,400)",
  "difficulty": "Low/Medium/High",
  "intent": "Informational/Commercial/Transactional",
  "article_titles": ["5 article title ideas that would rank for this vertical"],
  "related_keywords": [{{"keyword": "...", "type": "long-tail|question|local|seasonal"}}],
  "questions_people_ask": ["What does X cost in California?"],
  "seasonal_tip": "When is the best time to publish content for this vertical?",
  "local_modifiers": ["SoCal city/region terms to append to keywords"],
  "aeo_angle": "The best featured snippet angle for this topic"
}}"""

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1200,
                "messages": [{"role": "user", "content": prompt}]
            }
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {resp.status_code}")

    data = resp.json()
    text = data.get("content", [{}])[0].get("text", "")

    import json, re
    clean = re.sub(r'```json|```', '', text).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Invalid JSON from model")
