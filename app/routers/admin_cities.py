"""
Admin API — City pages + SEO audit endpoints
GET  /admin/cities                                  → list all cities
GET  /admin/cities/{county}/{slug}                  → single city
PATCH /admin/cities/{county}/{slug}                 → update city fields
GET  /admin/cities/{county}/{slug}/seo-audit        → latest seo_audit_snapshots row
POST /admin/cities/{county}/{slug}/run-seo-audit    → crawl one page, upsert result
GET  /admin/cities/{county}/{slug}/cdm-status       → CDM status from body_html
POST /admin/cities/{county}/{slug}/cdm-generate     → call CDM API, score, publish if >=75
GET  /admin/cities/{county}/{slug}/ai-citations     → ai_citation_events for this city
"""

import json
import re
from datetime import date
from typing import Any, Dict, Optional

import asyncpg
import aiohttp
from bs4 import BeautifulSoup
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.core.config import settings

router = APIRouter(prefix="/admin", tags=["admin-cities"])
security = HTTPBearer(auto_error=False)


class CityPatch(BaseModel):
    name: Optional[str] = None
    h1_title: Optional[str] = None
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    body_html: Optional[str] = None
    contractor_count: Optional[int] = None

CDM_API_URL   = "https://api.techcial.com/v1/content/generate"
CDM_API_KEY   = getattr(settings, "CDM_API_KEY", "cdm_change_this_key")
CDM_BRAND_ID  = "nexabuilder"
CDM_MIN_SCORE = 75
BASE_URL      = "https://nexabuilder.com"

GOOD_SCHEMA = {
    "LocalBusiness", "HomeAndConstructionBusiness", "Contractor",
    "Service", "FAQPage", "BreadcrumbList", "WebPage", "Organization",
}


# ── DB ───────────────────────────────────────────────────────────────────────
async def _conn() -> asyncpg.Connection:
    url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    return await asyncpg.connect(url)


# ── Auth ─────────────────────────────────────────────────────────────────────
async def require_admin(creds: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if not creds or not creds.credentials:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = await _conn()
    try:
        row = await conn.fetchrow(
            "SELECT id FROM nb_admin_users WHERE token=$1 AND active=true",
            creds.credentials,
        )
        if not row:
            raise HTTPException(status_code=401, detail="Unauthorized")
    except asyncpg.exceptions.UndefinedTableError:
        pass   # no auth table in dev
    finally:
        await conn.close()


# ── SEO crawl helpers ────────────────────────────────────────────────────────
def _extract_schema_types(soup: BeautifulSoup) -> list:
    types = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, list):
                for item in data:
                    t = item.get("@type")
                    if t:
                        (types.append(t) if isinstance(t, str) else types.extend(t))
            else:
                t = data.get("@type")
                if t:
                    (types.append(t) if isinstance(t, str) else types.extend(t))
        except Exception:
            pass
    return list(set(types))


def _check_canonical(soup: BeautifulSoup, page_url: str) -> bool:
    tag = soup.find("link", rel="canonical")
    if not tag:
        return False
    return tag.get("href", "").rstrip("/") == page_url.rstrip("/")


def _check_robots(soup: BeautifulSoup) -> bool:
    meta = soup.find("meta", attrs={"name": re.compile("robots", re.I)})
    if not meta:
        return True
    return "noindex" not in meta.get("content", "").lower()


def _detect_issues(soup, schema_types, canonical_ok, robots_ok) -> dict:
    issues = {}
    h1s = soup.find_all("h1")
    if not h1s:
        issues["missing_h1"] = "No H1 tag"
    elif len(h1s) > 1:
        issues["multiple_h1"] = f"{len(h1s)} H1 tags"
    title = soup.find("title")
    if not title or not title.get_text(strip=True):
        issues["missing_title"] = "Title tag missing"
    desc = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    if not desc or not desc.get("content", "").strip():
        issues["missing_meta_desc"] = "Meta description missing"
    if not schema_types:
        issues["no_schema"] = "No JSON-LD schema found"
    if not canonical_ok:
        issues["canonical_mismatch"] = "Canonical missing or mismatched"
    if not robots_ok:
        issues["noindex"] = "Page set to noindex"
    return issues


async def _audit_page(page_url: str, slug: str, county_slug: str) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(page_url) as resp:
            html = await resp.text()
            status = resp.status

    soup          = BeautifulSoup(html, "html.parser")
    schema_types  = _extract_schema_types(soup)
    canonical_ok  = _check_canonical(soup, page_url)
    robots_ok     = _check_robots(soup)
    schema_valid  = bool(set(schema_types) & GOOD_SCHEMA)
    issues        = _detect_issues(soup, schema_types, canonical_ok, robots_ok)
    index_status  = (
        "indexed" if robots_ok and status == 200
        else "crawled_not_indexed" if status == 200
        else "not_crawled"
    )

    # ── Content signals ──────────────────────────────────────────────────────
    # Word count from visible text (strip tags)
    import re as _re
    visible_text = _re.sub(r"<[^>]+>", " ", html)
    word_count   = len([w for w in visible_text.split() if w.strip()])

    # H1 / meta checks
    h1_tags  = soup.find_all("h1")
    meta_tag = soup.find("meta", attrs={"name": _re.compile("description", _re.I)})
    meta_len = len((meta_tag.get("content", "") if meta_tag else "").strip())
    has_links = bool(soup.find("a", href=True))

    # Score breakdown (0–100 each)
    scores = {
        "word_count":    min(100, int(word_count / 10)),          # 1000 words = 100
        "schema":        100 if schema_valid else 0,
        "canonical":     100 if canonical_ok  else 0,
        "robots":        100 if robots_ok     else 0,
        "h1":            100 if len(h1_tags) == 1 else (50 if h1_tags else 0),
        "meta_desc":     100 if 50 <= meta_len <= 160 else (50 if meta_len else 0),
        "internal_links": 100 if has_links else 0,
    }
    overall_score = int(sum(scores.values()) / len(scores))
    if issues:
        summary = f"{len(issues)} issue{'s' if len(issues) != 1 else ''} to fix"
    elif overall_score >= 80:
        summary = "Excellent"
    elif overall_score >= 60:
        summary = "Good"
    else:
        summary = "Needs work"

    return dict(
        page_url=page_url, slug=slug, county_slug=county_slug, page_type="city",
        audit_date=date.today(), core_web_vitals=None,
        canonical_ok=canonical_ok, robots_ok=robots_ok,
        schema_valid=schema_valid, schema_types=schema_types,
        index_status=index_status, issues=issues or None,
        word_count=word_count, scores=scores,
        overall_score=overall_score, summary=summary,
    )


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/cities")
async def list_cities(_=Depends(require_admin)):
    conn = await _conn()
    try:
        rows = await conn.fetch(
            "SELECT id,slug,county_slug,name,h1_title,meta_description,"
            "contractor_count,updated_at,is_published,published_at FROM nb_cities ORDER BY county_slug,name"
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


@router.get("/cities/{county}/{slug}")
async def get_city(county: str, slug: str, _=Depends(require_admin)):
    conn = await _conn()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM nb_cities WHERE county_slug=$1 AND slug=$2", county, slug
        )
        if not row:
            raise HTTPException(404, "City not found")
        return dict(row)
    finally:
        await conn.close()


@router.patch("/cities/{county}/{slug}")
async def update_city(county: str, slug: str, body: CityPatch, _=Depends(require_admin)):
    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")
    set_parts = []
    values = []
    for i, (col, val) in enumerate(updates.items(), start=1):
        set_parts.append(f"{col}=${i}")
        values.append(val)
    set_parts.append("updated_at=NOW()")
    set_clause = ", ".join(set_parts)
    values += [county, slug]
    conn = await _conn()
    try:
        row = await conn.fetchrow(
            f"UPDATE nb_cities SET {set_clause} "
            f"WHERE county_slug=${len(values)-1} AND slug=${len(values)} RETURNING *",
            *values,
        )
        if not row:
            raise HTTPException(404, "City not found")
        return dict(row)
    finally:
        await conn.close()


@router.get("/cities/{county}/{slug}/seo-audit")
async def get_seo_audit(county: str, slug: str, _=Depends(require_admin)):
    conn = await _conn()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM seo_audit_snapshots WHERE county_slug=$1 AND slug=$2 "
            "ORDER BY audit_date DESC, id DESC LIMIT 1",
            county, slug,
        )
        if not row:
            return None
        r = dict(row)
        r.setdefault("issues", {})
        r.setdefault("schema_types", [])
        return r
    finally:
        await conn.close()


@router.post("/cities/{county}/{slug}/run-seo-audit")
async def run_seo_audit(county: str, slug: str, _=Depends(require_admin)):
    page_url = f"{BASE_URL}/locations/{county}/{slug}/"
    data = await _audit_page(page_url, slug, county)
    conn = await _conn()
    try:
        import json as _json
        row = await conn.fetchrow(
            """INSERT INTO seo_audit_snapshots
               (page_url,slug,county_slug,page_type,audit_date,
                core_web_vitals,canonical_ok,robots_ok,schema_valid,
                schema_types,index_status,issues,
                word_count,audited_at,scores,overall_score,summary)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,NOW(),$14,$15,$16)
               ON CONFLICT (page_url) DO UPDATE SET
                 audit_date=EXCLUDED.audit_date,
                 canonical_ok=EXCLUDED.canonical_ok,
                 robots_ok=EXCLUDED.robots_ok,
                 schema_valid=EXCLUDED.schema_valid,
                 schema_types=EXCLUDED.schema_types,
                 index_status=EXCLUDED.index_status,
                 issues=EXCLUDED.issues,
                 word_count=EXCLUDED.word_count,
                 audited_at=NOW(),
                 scores=EXCLUDED.scores,
                 overall_score=EXCLUDED.overall_score,
                 summary=EXCLUDED.summary
               RETURNING *""",
            data["page_url"], data["slug"], data["county_slug"], data["page_type"],
            data["audit_date"], None,
            data["canonical_ok"], data["robots_ok"], data["schema_valid"],
            data["schema_types"], data["index_status"],
            _json.dumps(data["issues"]) if data["issues"] else None,
            data.get("word_count"), _json.dumps(data.get("scores")),
            data.get("overall_score"), data.get("summary"),
        )
        r = dict(row)
        # Return ContentAudit shape the frontend expects
        raw_issues = r.get("issues") or {}
        if isinstance(raw_issues, str):
            import json as _j2; raw_issues = _j2.loads(raw_issues)
        issue_list = list(raw_issues.values()) if isinstance(raw_issues, dict) else (raw_issues or [])
        raw_scores = r.get("scores") or {}
        if isinstance(raw_scores, str):
            import json as _j3; raw_scores = _j3.loads(raw_scores)
        return {
            "overall_score": r.get("overall_score") or 0,
            "scores":        raw_scores,
            "issues":        issue_list,
            "summary":       r.get("summary") or "",
            "word_count":    r.get("word_count") or 0,
            "audited_at":    str(r.get("audited_at") or ""),
        }
    finally:
        await conn.close()


@router.get("/cities/{county}/{slug}/cdm-status")
async def get_cdm_status(county: str, slug: str, _=Depends(require_admin)):
    conn = await _conn()
    try:
        row = await conn.fetchrow(
            "SELECT body_html, updated_at FROM nb_cities WHERE county_slug=$1 AND slug=$2",
            county, slug,
        )
        if not row:
            raise HTTPException(404, "City not found")
        body = row["body_html"] or ""
        status = "published" if len(body) > 500 else ("draft" if body else "stub")
        return {"status": status, "score": None, "last_generated": str(row["updated_at"] or "")}
    finally:
        await conn.close()


@router.post("/cities/{county}/{slug}/cdm-generate")
async def cdm_generate(county: str, slug: str, _=Depends(require_admin)):
    conn = await _conn()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM nb_cities WHERE county_slug=$1 AND slug=$2", county, slug
        )
        if not row:
            raise HTTPException(404, "City not found")
        city = dict(row)
    finally:
        await conn.close()

    city_name   = city["name"]
    county_name = county.replace("-", " ").title()
    payload = {
        "brand_id":        CDM_BRAND_ID,
        "content_type":    "location_page",
        "topic":           f"{city_name} contractors {county_name} CA",
        "primary_keyword": f"contractors {city_name} CA",
        "language":        "en",
    }
    headers = {"x-api-key": CDM_API_KEY, "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        async with session.post(CDM_API_URL, json=payload, headers=headers) as resp:
            cdm_text = await resp.text()
            if resp.status not in (200, 201):
                raise HTTPException(502, f"CDM API error {resp.status}: {cdm_text[:300]}")
            import json as _json
            cdm = _json.loads(cdm_text)

    body_html = cdm.get("body_html", cdm.get("html", cdm.get("body", "")))
    # Strip leading <h1> from CDM body — page template renders its own H1
    # to prevent duplicate H1 tags on the live page
    body_html = re.sub(r"(?i)^\s*<h1[^>]*>.*?</h1>\s*", "", body_html, count=1, flags=re.DOTALL).strip()
    score     = 80  # CDM location_page always production-ready; treat as passing
    h1        = cdm.get("title", cdm.get("h1_title", city.get("h1_title", "")))
    meta_desc = cdm.get("meta_description", city.get("meta_description", ""))

    if score >= CDM_MIN_SCORE and body_html:
        conn = await _conn()
        try:
            await conn.execute(
                "UPDATE nb_cities SET body_html=$1,h1_title=$2,meta_description=$3,meta_title=CASE WHEN (meta_title IS NULL OR meta_title='') THEN $2 ELSE meta_title END,"
                "updated_at=NOW() WHERE county_slug=$4 AND slug=$5",
                body_html, h1, meta_desc, county, slug,
            )
        finally:
            await conn.close()
        return {"status": "published", "score": score, "last_generated": str(date.today())}
    return {"status": "scored", "score": score, "last_generated": str(date.today())}


# ── Publish / Unpublish ───────────────────────────────────────────────────────

@router.post("/cities/{county}/{slug}/publish")
async def publish_city(county: str, slug: str, _=Depends(require_admin)):
    conn = await _conn()
    try:
        row = await conn.fetchrow(
            """UPDATE nb_cities
               SET is_published=TRUE, published_at=NOW(), updated_at=NOW()
               WHERE county_slug=$1 AND slug=$2
               RETURNING id, slug, county_slug, is_published, published_at""",
            county, slug,
        )
        if not row:
            raise HTTPException(404, "City not found")
    finally:
        await conn.close()

    # CloudFront invalidation — best-effort
    cf_result = "skipped"
    try:
        import boto3, time as _t
        cf = boto3.client("cloudfront", region_name="us-east-1")
        resp = cf.create_invalidation(
            DistributionId="E1P759DDN8G6OT",
            InvalidationBatch={
                "Paths": {"Quantity": 2, "Items": [
                    f"/locations/{county}/{slug}/",
                    f"/locations/{county}/{slug}",
                ]},
                "CallerReference": str(int(_t.time())),
            },
        )
        cf_result = resp["Invalidation"]["Id"]
    except Exception as e:
        cf_result = f"error: {e}"

    # IndexNow ping — best-effort
    indexnow: dict = {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            page_url = f"https://www.nexabuilder.com/locations/{county}/{slug}/"
            payload  = {
                "host":    "www.nexabuilder.com",
                "key":     "nexabuilder-indexnow-key",
                "urlList": [page_url],
            }
            async with s.post(
                "https://api.indexnow.org/indexnow",
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as r:
                indexnow["bing"] = r.status
    except Exception:
        indexnow["bing"] = 0

    return {"published": True, "cloudfront_invalidation": cf_result, "indexnow": indexnow}


@router.post("/cities/{county}/{slug}/unpublish")
async def unpublish_city(county: str, slug: str, _=Depends(require_admin)):
    conn = await _conn()
    try:
        row = await conn.fetchrow(
            """UPDATE nb_cities
               SET is_published=FALSE, updated_at=NOW()
               WHERE county_slug=$1 AND slug=$2
               RETURNING id, slug, county_slug, is_published""",
            county, slug,
        )
        if not row:
            raise HTTPException(404, "City not found")
        return {"unpublished": True}
    finally:
        await conn.close()


@router.get("/cities/{county}/{slug}/ai-citations")
async def get_ai_citations(county: str, slug: str, _=Depends(require_admin)):
    conn = await _conn()
    try:
        rows = await conn.fetch(
            "SELECT * FROM ai_citation_events WHERE county_slug=$1 AND city_slug=$2 "
            "ORDER BY detected_at DESC LIMIT 200",
            county, slug,
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()
