# app/routers/api/meta_generator.py
# AI-powered title tag + meta description generator
import os, httpx, re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/meta", tags=["Meta Generator"])
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

class MetaRequest(BaseModel):
    page_type: str          # "blog", "service", "home", "location"
    topic: str              # main subject / keyword
    primary_keyword: str    # target keyword
    secondary_keywords: str = ""
    site: str = "nexabuilder.com"
    language: str = "en"

@router.post("/generate")
async def generate_meta(payload: MetaRequest):
    if not ANTHROPIC_KEY:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured")

    lang = "Spanish (Mexican/SoCal dialect, warm and direct)" if payload.language == "es" else "English (clear, action-oriented)"

    prompt = f"""Generate SEO-optimized meta tags for a home improvement lead generation page.

Site: {payload.site}
Page type: {payload.page_type}
Topic: {payload.topic}
Primary keyword: "{payload.primary_keyword}"
Secondary keywords: {payload.secondary_keywords or "none"}
Language: {lang}

Rules:
- Title: 50-60 chars, primary keyword near start, brand at end (| NexaBuilder or | UnaP iscina)
- Description: 140-155 chars, include primary keyword, clear value prop, soft CTA
- Generate 3 variants for each so the user can pick the best one

Return ONLY valid JSON:
{{
  "titles": [
    {{"text": "...", "chars": 0, "score": "Good|Too short|Too long"}},
    {{"text": "...", "chars": 0, "score": "Good|Too short|Too long"}},
    {{"text": "...", "chars": 0, "score": "Good|Too short|Too long"}}
  ],
  "descriptions": [
    {{"text": "...", "chars": 0, "score": "Good|Too short|Too long"}},
    {{"text": "...", "chars": 0, "score": "Good|Too short|Too long"}},
    {{"text": "...", "chars": 0, "score": "Good|Too short|Too long"}}
  ],
  "canonical_suggestion": "relative path (e.g. /servicios/piscinas/)",
  "viewport_meta": "width=device-width, initial-scale=1"
}}"""

    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 800, "messages": [{"role": "user", "content": prompt}]}
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"AI error: {resp.status_code}")

    text = resp.json().get("content", [{}])[0].get("text", "")
    clean = re.sub(r"```json|```", "", text).strip()
    try:
        import json
        data = json.loads(clean)
        # Add char counts if missing
        for item in data.get("titles", []):
            item["chars"] = len(item.get("text",""))
            if item["chars"] < 30: item["score"] = "Too short"
            elif item["chars"] > 60: item["score"] = "Too long"
            else: item["score"] = "Good"
        for item in data.get("descriptions", []):
            item["chars"] = len(item.get("text",""))
            if item["chars"] < 100: item["score"] = "Too short"
            elif item["chars"] > 155: item["score"] = "Too long"
            else: item["score"] = "Good"
        return data
    except:
        raise HTTPException(status_code=502, detail="Invalid response from AI")
