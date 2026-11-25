"""
Admin AI Citations Router
GET  /api/admin/ai-citations/summary              — 30-day counts + trend
GET  /api/admin/ai-citations/{county}/{slug}      — city citation history
POST /api/admin/ai-citations/probe                — async Lambda trigger (batch)
POST /api/admin/ai-citations/probe/{county}/{slug} — sync single-city probe
"""
from __future__ import annotations
import json, re, logging, asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

import asyncpg, boto3, httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin-ai-citations"])

PROBE_LAMBDA  = "nexabuilder-citation-probe"
TARGET_DOMAIN = "nexabuilder.com"
QUERY_TEMPLATES = [
    "Who are the best licensed contractors in {city}, {county} County California?",
    "Find me a reputable home improvement contractor in {city} CA",
    "Best general contractor {city} California for home renovation",
    "Recommended roofing contractors in {city} {county} County CA",
]

def _db(): return settings.DATABASE_URL.replace("postgresql+asyncpg://","postgresql://")
async def require_admin(): return True

def _rank(text: str) -> int:
    lower = text.lower()
    idx = lower.find(TARGET_DOMAIN)
    if idx == -1: return 99
    return len(re.findall(r'https?://(?!nexabuilder)', lower[:idx])) + 1

async def _probe_perplexity(query, api_key):
    # Perplexity migrated from /chat/completions to /v1/agent (Agent API) in 2026
    async with httpx.AsyncClient() as client:
        r = await client.post("https://api.perplexity.ai/v1/agent",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "effort": "fast",
                "instructions": "You are a local contractor research assistant. Search the web and answer concisely.",
                "input": [{"type": "message", "role": "user", "content": query}],
                "tools": [{"type": "web_search", "filters": {"search_recency_filter": "month"}}],
            },
            timeout=45)
        r.raise_for_status()
        d = r.json()
    text = d.get("output_text", "")
    urls = []
    for item in d.get("output", []):
        if item.get("type") == "search_results":
            for res in item.get("results", []):
                u = res.get("url", "")
                if TARGET_DOMAIN in u.lower():
                    urls.append(u)
    cited = TARGET_DOMAIN in text.lower() or bool(urls)
    url = urls[0] if urls else None
    import re as _re2
    sents = _re2.split(r"(?<=[.!?])\\s+", text)
    snip = next((s for s in sents if TARGET_DOMAIN in s.lower()), None) if cited else None
    return {"source": "perplexity", "cited": cited, "url": url,
            "snippet": snip[:500] if snip else None, "rank": _rank(text), "query": query}

async def _probe_openai(query, api_key):
    async with httpx.AsyncClient() as client:
        r = await client.post("https://api.openai.com/v1/responses",
            headers={"Authorization":f"Bearer {api_key}"},
            json={"model":"gpt-4o","tools":[{"type":"web_search_preview"}],"input":query}, timeout=45)
        r.raise_for_status(); d = r.json()
    text, url = "", None
    for block in d.get("output",[]):
        if block.get("type")=="message":
            for part in block.get("content",[]):
                if part.get("type")=="output_text": text += part.get("text","")
                for ann in part.get("annotations",[]):
                    if ann.get("type")=="url_citation" and TARGET_DOMAIN in ann.get("url",""):
                        url = ann["url"]
    cited = TARGET_DOMAIN in text.lower()
    sents = re.split(r'(?<=[.!?])\s+', text)
    snip = next((s for s in sents if TARGET_DOMAIN in s.lower()),None) if cited else None
    return {"source":"chatgpt","cited":cited,"url":url,"snippet":snip[:500] if snip else None,"rank":_rank(text),"query":query}

async def _probe_city(slug, county, name):
    county_name = county.replace("-"," ").title()
    tmpl = QUERY_TEMPLATES[hash(slug) % len(QUERY_TEMPLATES)]
    query = tmpl.format(city=name, county=county_name)
    ssm = boto3.client("ssm", region_name="us-west-1")
    results = []
    try:
        pk = ssm.get_parameter(Name="/nexabuilder/api/PERPLEXITY_KEY",WithDecryption=True)["Parameter"]["Value"]
        results.append(await _probe_perplexity(query, pk))
    except Exception as e:
        logger.warning("Perplexity probe skipped: %s", e)
    try:
        ok = ssm.get_parameter(Name="/nexabuilder/api/OPENAI_KEY",WithDecryption=True)["Parameter"]["Value"]
        results.append(await _probe_openai(query, ok))
    except Exception as e:
        logger.warning("OpenAI probe failed: %s", e)
    return results

# ── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/ai-citations/summary")
async def citation_summary(_=Depends(require_admin)):
    conn = await asyncpg.connect(_db())
    try:
        now = datetime.now(timezone.utc)
        rows30 = await conn.fetch(
            """SELECT source, COUNT(*) AS total,
               COUNT(*) FILTER (WHERE rank_in_response IS NOT NULL) AS cited,
               MAX(detected_at) AS last_seen
               FROM ai_citation_events WHERE detected_at >= $1 GROUP BY source ORDER BY cited DESC""",
            now - timedelta(days=30))
        rows7 = await conn.fetch(
            """SELECT source, COUNT(*) FILTER (WHERE rank_in_response IS NOT NULL) AS cited
               FROM ai_citation_events WHERE detected_at >= $1 GROUP BY source""",
            now - timedelta(days=7))
        c7 = {r["source"]:r["cited"] for r in rows7}
        trend = await conn.fetch(
            """SELECT DATE(detected_at AT TIME ZONE 'UTC') AS day, source,
               COUNT(*) FILTER (WHERE rank_in_response IS NOT NULL) AS cited
               FROM ai_citation_events WHERE detected_at >= $1 GROUP BY day,source ORDER BY day""",
            now - timedelta(days=14))
        by_source = [{"source": r["source"],
                      "count": r["cited"],
                      "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
                      "total_probes_30d": r["total"],
                      "total_cited_30d": r["cited"],
                      "citation_rate_30d": round(r["cited"]/max(r["total"],1),3),
                      "cited_7d": c7.get(r["source"],0)}
                     for r in rows30]
        total_citations = sum(s["count"] for s in by_source)
        return {
            "total_citations": total_citations,
            "by_source": by_source,
            "daily_trend": [{"date": str(r["day"]), "count": r["cited"], "source": r["source"]} for r in trend]}
    finally:
        await conn.close()

@router.get("/ai-citations/{county}/{slug}")
async def city_citations(county:str, slug:str,
    source:Optional[str]=Query(None), limit:int=Query(50,le=200), _=Depends(require_admin)):
    conn = await asyncpg.connect(_db())
    try:
        q = "SELECT id,source,query,url_cited,snippet,rank_in_response,detected_at FROM ai_citation_events WHERE county_slug=$1 AND city_slug=$2"
        args = [county, slug]
        if source: q += " AND source=$3"; args.append(source)
        q += f" ORDER BY detected_at DESC LIMIT ${len(args)+1}"; args.append(limit)
        rows = await conn.fetch(q, *args)
        return [dict(r) for r in rows]
    finally:
        await conn.close()

class ProbeRequest(BaseModel):
    county_slug: Optional[str] = None
    limit: int = 50

@router.post("/ai-citations/probe")
async def trigger_probe(req:ProbeRequest, _=Depends(require_admin)):
    lc = boto3.client("lambda", region_name="us-west-1")
    payload = {"limit":req.limit}
    if req.county_slug: payload["county_slug"] = req.county_slug
    try:
        resp = lc.invoke(FunctionName=PROBE_LAMBDA, InvocationType="Event", Payload=json.dumps(payload))
        return {"status":"triggered","lambda":PROBE_LAMBDA,"payload":payload,"http_status":resp["StatusCode"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/ai-citations/probe/{county}/{slug}")
async def probe_single(county:str, slug:str, _=Depends(require_admin)):
    conn = await asyncpg.connect(_db())
    try:
        city = await conn.fetchrow(
            "SELECT slug,county_slug,name FROM nb_cities WHERE slug=$1 AND county_slug=$2", slug, county)
        if not city: raise HTTPException(status_code=404, detail="City not found")
        results = await _probe_city(city["slug"], city["county_slug"], city["name"])
        saved = []
        for r in results:
            await conn.execute(
                """INSERT INTO ai_citation_events
                   (source,query,url_cited,snippet,rank_in_response,detected_at,city_slug,county_slug)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                r["source"],r["query"],r.get("url"),r.get("snippet"),
                r["rank"] if r["cited"] else None,
                datetime.now(timezone.utc), slug, county)
            saved.append({"source":r["source"],"cited":r["cited"],"rank":r.get("rank"),"url":r.get("url")})
        return {"city":slug,"county":county,"probes_run":len(saved),"results":saved}
    finally:
        await conn.close()
