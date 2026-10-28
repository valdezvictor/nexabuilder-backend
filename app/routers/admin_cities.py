"""
Admin API — City pages + SEO audit endpoints
GET  /admin/cities                                  → list all cities
GET  /admin/cities/{county}/{slug}                  → single city
GET  /admin/cities/{county}/{slug}/seo-audit        → latest seo_audit_snapshots row
POST /admin/cities/{county}/{slug}/run-seo-audit    → crawl one page, upsert result
GET  /admin/cities/{county}/{slug}/cdm-status       → CDM status from body_html
POST /admin/cities/{county}/{slug}/cdm-generate     → call CDM API, score, publish if >=75
GET  /admin/cities/{county}/{slug}/ai-citations     → ai_citation_events for this city
"""

import json
import re
from datetime import date
from typing import Optional

import asyncpg
import aiohttp
from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.core.config import settings

router = APIRouter(prefix="/admin", tags=["admin-cities"])
security = HTTPBearer(auto_error=False)

CDM_API_URL   = "https://api.techcial.com/v1/content"
CDM_API_KEY   = "GidhUSbSVmhSzpY8Xd7gfBEJJYB-ycHKz5j-JxEYSpU"
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
    # Delegate to existing token validation used across the admin
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
    return dict(
        page_url=page_url, slug=slug, county_slug=county_slug, page_type="city",
        audit_date=str(date.today()), core_web_vitals=None,
        canonical_ok=canonical_ok, robots_ok=robots_ok,
        schema_valid=schema_valid, schema_types=schema_types,
        index_status=index_status, issues=issues or None,
    )


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/cities")
async def list_cities(_=Depends(require_admin)):
    conn = await _conn()
    try:
        rows = await conn.fetch(
            "SELECT id,slug,county_slug,name,h1_title,meta_description,"
            "contractor_count,updated_at FROM nb_cities ORDER BY county_slug,name"
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
        row = await conn.fetchrow(
            """INSERT INTO seo_audit_snapshots
               (page_url,slug,county_slug,page_type,audit_date,
                core_web_vitals,canonical_ok,robots_ok,schema_valid,
                schema_types,index_status,issues)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
               ON CONFLICT DO NOTHING RETURNING *""",
            data["page_url"], data["slug"], data["county_slug"], data["page_type"],
            data["audit_date"], None,
            data["canonical_ok"], data["robots_ok"], data["schema_valid"],
            data["schema_types"], data["index_status"], data["issues"],
        )
        if not row:
            row = await conn.fetchrow(
                "SELECT * FROM seo_audit_snapshots WHERE county_slug=$1 AND slug=$2 "
                "ORDER BY audit_date DESC, id DESC LIMIT 1",
                county, slug,
            )
        return dict(row)
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

    payload = {
        "brand_id": CDM_BRAND_ID,
        "content_type": "city_page",
        "variables": {
            "city_name":        city["name"],
            "county":           county.replace("-", " ").title(),
            "slug":             slug,
            "county_slug":      county,
            "contractor_count": city.get("contractor_count", 0),
        },
    }
    headers = {"Authorization": f"Bearer {CDM_API_KEY}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(CDM_API_URL, json=payload, headers=headers) as resp:
            if resp.status not in (200, 201):
                raise HTTPException(502, f"CDM API error {resp.status}")
            cdm = await resp.json()

    body_html = cdm.get("html", cdm.get("body", ""))
    score     = cdm.get("score", 0)
    h1        = cdm.get("h1_title", city.get("h1_title", ""))
    meta_desc = cdm.get("meta_description", city.get("meta_description", ""))

    if score >= CDM_MIN_SCORE and body_html:
        conn = await _conn()
        try:
            await conn.execute(
                "UPDATE nb_cities SET body_html=$1,h1_title=$2,meta_description=$3,"
                "updated_at=NOW() WHERE county_slug=$4 AND slug=$5",
                body_html, h1, meta_desc, county, slug,
            )
        finally:
            await conn.close()
        return {"status": "published", "score": score, "last_generated": str(date.today())}
    return {"status": "scored", "score": score, "last_generated": str(date.today())}


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
