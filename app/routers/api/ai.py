from pydantic import BaseModel
from fastapi import Header
import os
from fastapi import APIRouter, Depends, HTTPException
from app.services.ai_lead_scoring import predict_lead_quality
from sqlalchemy.orm import Session
from app.db import get_db

from app.services.ai_lead_scoring import predict_lead_quality

router = APIRouter(prefix="/api/ai", tags=["AI"])

# AI Lead Score
@router.post("/lead-score")
def ai_lead_score(payload: dict):
    try:
        features = {
            "phone": payload.get("phone"),
            "email": payload.get("email"),
            "budget_max": payload.get("budget_max"),
            "vertical": payload.get("vertical"),
        }

        result = predict_lead_quality(features)
        return {
            "ai_score": result.get("ai_score"),
            "explanations": result.get("explanations", []),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Routing Summary
@router.post("/routing-summary")
def routing_summary(lead_id: int, db: Session = Depends(get_db)):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    (
        log,
        explanation_list,
        scored_contractors,
        alerts,
        requires_financing_escalation,
        summary,
    ) = route_lead(lead, db)

    ai_summary = build_ai_routing_summary(
        lead=lead,
        summary=summary,
        alerts=alerts,
        scored_contractors=scored_contractors,
    )

    return ai_summary


# ── AI SEO Insights ── proxy to Claude server-side (avoids CORS) ──────────────

from pydantic import BaseModel as _PBM
import httpx as _httpx

class _SEOInsightReq(_PBM):
    query:       str
    page:        str = ""
    impressions: int = 0
    clicks:      int = 0
    position:    float = 0.0
    vertical:    str = "general"

@router.post("/seo-insights")
async def ai_seo_insights(payload: _SEOInsightReq,
                           x_admin_key: str = __import__("fastapi").Header(...)):
    admin_key     = os.getenv("CMS_ADMIN_KEY", "")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    from fastapi import HTTPException
    if x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not anthropic_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    pos   = payload.position
    pl    = ("Top 3" if pos<=3 else "Page 1" if pos<=10 else "Page 2" if pos<=20 else f"Pages 3-5 (pos {pos:.0f})" if pos<=50 else f"Page 6+")
    ctr   = f"{(payload.clicks/payload.impressions*100):.2f}%" if payload.impressions else "0%"

    prompt = f"""You are an expert SEO consultant for NexaBuilder, a CSLB-verified contractor-matching platform in Southern California with location, service, and material gallery pages.

QUERY: "{payload.query}"
PAGE: {payload.page or "nexabuilder.com"}
POSITION: {pos:.1f} ({pl}) | IMPRESSIONS: {payload.impressions} | CLICKS: {payload.clicks} | CTR: {ctr} | VERTICAL: {payload.vertical}

Provide analysis with these exact ## headings:

## Why This Position
2-3 sentences specific to this query and page.

## Quick Wins (Do This Week)
3 specific actions naming exact elements to change (H1, title tag, meta description, schema, body copy, internal links).

## Content Gaps
2-3 questions this page should answer based on search intent.

## Internal Linking Opportunity
1-2 specific pages on nexabuilder.com that should link here, with anchor text.

## CMS Action
One precise update to make right now — exact text change.

No generic advice. Be specific to this query and page."""

    async with _httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": anthropic_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 1000,
                  "messages": [{"role": "user", "content": prompt}]})
    if not r.is_success:
        raise HTTPException(status_code=502, detail=f"AI error {r.status_code}: {r.text[:100]}")
    data  = r.json()
    text  = next((b["text"] for b in data.get("content",[]) if b.get("type")=="text"), "")
    return {"insight": text, "tokens": data.get("usage",{}).get("output_tokens",0)}


# ── AI Article Generator ─────────────────────────────────────────────────────
from pydantic import BaseModel as _BM2

class _ArticleGenReq(_BM2):
    topic:       str
    top_queries: list = []

@router.post("/generate-article")
async def generate_article_endpoint(payload: _ArticleGenReq,
                                     x_admin_key: str = __import__("fastapi").Header(...)):
    import json as _json, re as _re
    import httpx as _httpx
    from fastapi import HTTPException
    admin_key = os.getenv("CMS_ADMIN_KEY","")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY","")
    if x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not anthropic_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    top_q = chr(10).join([
        f"- {q.get('query','?')} (pos {q.get('avg_position','?')}, {q.get('impressions','?')} imp)"
        for q in (payload.top_queries or [])[:5]
    ]) or "- licensed contractor los angeles ca"
    prompt = f"""Write a complete HTML blog article for nexabuilder.com about: "{payload.topic}"
Site: CSLB-verified contractor matching for Southern California homeowners.
Top GSC queries for context:
{top_q}
Requirements:
- HTML only (h1,h2,p,ul,li,table) - no markdown
- 5-6 H2 sections: what/why, steps, cost table with SoCal ranges, local context, AEO answer block, CTA
- AEO block: <div style="background:#f0f9ff;border-left:4px solid #0ea5e9;padding:16px 20px;margin:24px 0;border-radius:0 8px 8px 0"><strong>Quick Answer:</strong> direct answer here</div>
- Internal links to /services/, /locations/los-angeles/, /get-quote/
- CTA at end: <div style="background:#0D1117;border:1px solid #D4A435;padding:20px 24px;border-radius:10px;margin:24px 0"><div style="color:#D4A435;font-weight:800;font-size:14px;margin-bottom:8px">Ready to find a licensed contractor?</div><p style="color:#e5e7eb;margin:0 0 14px;font-size:14px">Submit your project and get matched with CSLB-verified contractors within 48 hours.</p><a href="/get-quote/" style="display:inline-block;background:#D4A435;color:#0D1117;font-weight:900;padding:11px 22px;border-radius:8px;text-decoration:none;font-size:14px">Get Free Contractor Quotes</a></div>
- 900-1200 words total
After the HTML add exactly this at the end (outside HTML):
META_JSON_START
{{"seo_title":"...","meta_description":"...","slug":"lowercase-hyphenated","primary_keyword":"{payload.topic}"}}
META_JSON_END"""
    async with _httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={{"x-api-key":anthropic_key,"anthropic-version":"2023-06-01","content-type":"application/json"}},
            json={{"model":"claude-sonnet-4-6","max_tokens":3000,
                  "messages":[{{"role":"user","content":prompt}}]}})
    if not r.is_success:
        raise HTTPException(status_code=502, detail=f"AI error {r.status_code}")
    full = next((b["text"] for b in r.json().get("content",[]) if b.get("type")=="text"),"")
    meta = {{}}
    m = _re.search(r'META_JSON_START\s*(\{.*?\})\s*META_JSON_END', full, _re.DOTALL)
    if m:
        try: meta = _json.loads(m.group(1))
        except: pass
    body = full[:m.start()].strip() if m else full.strip()
    return {{"body_html":body,"seo_title":meta.get("seo_title","")[:120],
             "meta_description":meta.get("meta_description","")[:320],
             "slug":meta.get("slug",""),"primary_keyword":meta.get("primary_keyword",payload.topic)}}


class TopicDiscoveryRequest(BaseModel):
    site_id:  str
    seed:     str
    language: str = "es"
    count:    int = 8


@router.post("/topic-discovery")
async def topic_discovery(payload: TopicDiscoveryRequest,
                           x_admin_key: str = Header(...)):
    """Generate article topic ideas for a micro site."""
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not ANTHROPIC_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    import httpx as _hx, json as _j, re as _re

    SITE_PROFILES = {
        "unapiscina":    {"name":"Una Piscina","domain":"unapiscina.com","vertical":"pool","cslb":"C-53","region":"Sur de California"},
        "eelectricista": {"name":"eElectricista","domain":"eelectricista.com","vertical":"electrical","cslb":"C-10","region":"Sur de California"},
        "piscinasy":     {"name":"Piscinasy","domain":"piscinasy.com","vertical":"pool","cslb":"C-53","region":"Sur de California"},
        "losruferos":    {"name":"Los Ruferos","domain":"losruferos.com","vertical":"roofing","cslb":"C-39","region":"Sur de California"},
        "ijardinero":    {"name":"iJardinero","domain":"ijardinero.com","vertical":"landscaping","cslb":"C-27","region":"Sur de California"},
        "swimmingpul":   {"name":"SwimmingPul","domain":"swimmingpul.com","vertical":"pool","cslb":"C-53","region":"Southern California"},
        "nexabuilder":   {"name":"NexaBuilder","domain":"nexabuilder.com","vertical":"general","cslb":"CSLB","region":"Southern California"},
    }
    p   = SITE_PROFILES.get(payload.site_id, SITE_PROFILES["nexabuilder"])
    lang = payload.language

    if lang == "es":
        prompt = f"""Eres un estratega SEO para {p["name"]} ({p["domain"]}), sitio de {p["vertical"]} en {p["region"]}.

Genera {payload.count} ideas de articulos de blog en espanol para el tema seed: "{payload.seed}"

Para cada idea incluye:
- primary_keyword: la keyword principal (en minusculas, como la buscaria alguien)
- title_hint: angulo especifico del articulo (1 oracion)
- search_intent: informacional | transaccional | local
- difficulty: facil | medio | dificil

Devuelve SOLO JSON array (sin markdown):
[{{"primary_keyword":"...","title_hint":"...","search_intent":"...","difficulty":"..."}}]"""
    else:
        prompt = f"""You are an SEO strategist for {p["name"]} ({p["domain"]}), a {p["vertical"]} site in {p["region"]}.

Generate {payload.count} blog article ideas for seed topic: "{payload.seed}"

For each idea include:
- primary_keyword: the main keyword (lowercase, how someone would search it)
- title_hint: specific article angle (1 sentence)
- search_intent: informational | transactional | local
- difficulty: easy | medium | hard

Return ONLY a JSON array (no markdown):
[{{"primary_keyword":"...","title_hint":"...","search_intent":"...","difficulty":"..."}}]"""

    async with _hx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 1200,
                  "messages": [{"role": "user", "content": prompt}]})

    text = next((b["text"] for b in r.json().get("content",[]) if b.get("type")=="text"), "")
    text = _re.sub(r"^```[a-z]*\n?|```$", "", text, flags=_re.MULTILINE).strip()
    try:
        topics = _j.loads(text)
        return {"topics": topics, "site_id": payload.site_id, "seed": payload.seed}
    except Exception:
        return {"topics": [], "error": "Parse error", "raw": text[:300]}


class SiteInsightRequest(BaseModel):
    query:       str
    page:        str =  None
    impressions: int = 0
    clicks:      int = 0
    position:    float = 0.0
    vertical:    str =  "general"
    domain:      str =  None
    site_name:   str =  None
    language:    str = "en"


@router.post("/seo-insights-site")
async def seo_insights_site(payload: SiteInsightRequest,
                              x_admin_key: str = Header(...)):
    """GSC AI analysis scoped to a specific micro site."""
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not ANTHROPIC_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    import httpx as _hx

    pos = payload.position
    pos_label = (
        "Top 3" if pos <= 3 else
        "Page 1 (positions 4-10)" if pos <= 10 else
        "Page 2 (positions 11-20)" if pos <= 20 else
        f"Pages 3-5 (position {pos:.0f})" if pos <= 50 else
        f"Page 6+ (position {pos:.0f})"
    )
    ctr = f"{(payload.clicks / payload.impressions * 100):.2f}%" if payload.impressions > 0 else "0%"
    site_name = payload.site_name or payload.domain or "this site"
    domain    = payload.domain or "nexabuilder.com"
    lang_note = "The site is in Spanish (Mexican/SoCal dialect)." if payload.language=="es" else ""

    prompt = f"""You are an expert SEO consultant for local home-services websites in Southern California.
{lang_note}

SITE: {site_name} ({domain})
VERTICAL: {payload.vertical or "home services"}

QUERY DATA:
- Search Query: "{payload.query}"
- Landing Page: {payload.page or "/"}
- Current Position: {pos:.1f} ({pos_label})
- Impressions (28 days): {payload.impressions}
- Clicks (28 days): {payload.clicks}
- CTR: {ctr}

Provide your analysis in this exact structure:

## Why This Position
2-3 sentences explaining the current position — be specific to this query, this site, and this page.

## Quick Wins (Do This Week)
3 specific actions that can move the needle within 1-2 weeks. Name the exact element to change (title tag, H1, meta description, body copy, etc.).

## Content Gaps
2-3 specific questions this page should answer that it probably doesn't, based on search intent.

## Internal Linking Opportunity
1-2 specific internal links that should point to this page, with exact anchor text.

## CMS Action
One precise thing to update right now — be exact (e.g. "Update H1 to: '...'").

Keep every recommendation specific to this query and this site. No generic advice."""

    async with _hx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 1000,
                  "messages": [{"role": "user", "content": prompt}]})

    data = r.json()
    text = next((b["text"] for b in data.get("content",[]) if b.get("type")=="text"), "")
    return {"insight": text, "model": data.get("model",""), "usage": data.get("usage",{})}
