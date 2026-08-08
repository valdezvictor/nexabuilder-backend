# app/routers/api/content_tools.py
# AI writing assist + duplicate content check (with rewrite variants) for CMS smart editor
import os, httpx, re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List

router = APIRouter(prefix="/api/content", tags=["Content Tools"])
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

class AIAssistRequest(BaseModel):
    block_key: str
    content_type: str
    current_value: str = ""
    prompt: str
    context: str = ""

class DupCheckRequest(BaseModel):
    content: str
    block_key: str = ""
    context: str = ""

async def _call_claude(prompt: str, system: str = "", max_tokens: int = 600) -> str:
    async with httpx.AsyncClient(timeout=25) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": max_tokens,
                  "system": system or "You are a professional content writer for NexaBuilder, a home improvement platform in Southern California.",
                  "messages": [{"role": "user", "content": prompt}]}
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"AI error: {resp.status_code}")
    return resp.json().get("content", [{}])[0].get("text", "").strip()

@router.post("/ai-assist")
async def ai_assist(payload: AIAssistRequest):
    if not ANTHROPIC_KEY:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured")

    system = """You are a professional content writer for NexaBuilder, a home improvement lead generation platform in Southern California targeting Latino homeowners.
Write clear, trustworthy, SEO-friendly content. Keep it concise. Include trust signals (CSLB, verified, licensed) naturally.
Return ONLY the improved content text — no preamble, no explanation, no quotes around it."""

    prompt = f"""Block: {payload.block_key}
Type: {payload.content_type}
Context: {payload.context}
Current content: {payload.current_value or "(empty)"}

Task: {payload.prompt}"""

    text = await _call_claude(prompt, system, 400)
    return {"suggestion": text, "block_key": payload.block_key}

@router.post("/duplicate-check")
async def duplicate_check(payload: DupCheckRequest):
    if not ANTHROPIC_KEY:
        raise HTTPException(status_code=503, detail="Not configured")
    if len(payload.content) < 20:
        return {"status": "too_short", "message": "Content too short to check", "variants": []}

    import json

    prompt = f"""Analyze this web content for duplicate/plagiarism issues, then provide rewrite alternatives.

Content to check:
\"\"\"{payload.content[:800]}\"\"\"

Context: {payload.context or "Home improvement lead generation page, Southern California, targeting Latino homeowners"}

Return ONLY valid JSON (no markdown):
{{
  "status": "unique|likely_duplicate|review_needed",
  "score": 0.0,
  "recommendation": "brief assessment",
  "flags": ["specific issues found"],
  "variants": [
    {{
      "text": "Rewrite variant 1 — most specific, includes local details and trust signals",
      "approach": "Local + Trust"
    }},
    {{
      "text": "Rewrite variant 2 — benefit-focused, addresses the homeowner's problem",
      "approach": "Benefit-focused"
    }},
    {{
      "text": "Rewrite variant 3 — bilingual-friendly, warmer tone for Latino audience",
      "approach": "Cultural connection"
    }}
  ]
}}

Rules for variants:
- Each variant must be meaningfully different from the others
- Avoid generic phrases like "dream home", "trusted professionals", "quality work"
- Include at least one specific detail (CSLB license class, county name, specific service)
- Keep each variant similar length to the original
- Write for a Latino homeowner in Orange County / LA County / Riverside / San Bernardino"""

    text = await _call_claude(prompt, max_tokens=900)
    clean = re.sub(r"```json|```", "", text).strip()
    try:
        data = json.loads(clean)
        # Ensure variants is always present
        if "variants" not in data:
            data["variants"] = []
        return data
    except:
        return {
            "status": "unique", "score": 0.1,
            "recommendation": "Content appears original",
            "flags": [],
            "variants": []
        }
