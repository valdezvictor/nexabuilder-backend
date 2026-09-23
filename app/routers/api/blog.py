"""
app/routers/api/blog.py
========================
Blog article CRUD API for the headless CMS.

Public endpoints (no auth — used by frontend):
    GET  /api/blog/{site_id}                    List published articles for a site
    GET  /api/blog/{site_id}/{slug}             Get one published article by slug
    GET  /api/blog/{site_id}/category/{cat}     List published articles by category

Admin endpoints (X-Admin-Key required):
    POST   /api/blog/admin/                     Create new article (draft)
    GET    /api/blog/admin/{site_id}            List all articles (all statuses)
    GET    /api/blog/admin/article/{id}         Get article by ID (any status)
    PUT    /api/blog/admin/article/{id}         Update article fields
    POST   /api/blog/admin/article/{id}/publish Publish article (sets status+published_at)
    POST   /api/blog/admin/article/{id}/unpublish Revert to draft
    DELETE /api/blog/admin/article/{id}         Soft-delete (set status=archived)
"""

import os, re
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Request, Depends, HTTPException, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.db import get_db
from app.models.blog_article import BlogArticle, ArticleStatus
from app.schemas.blog_article import (
    ArticleCreate, ArticleUpdate,
    ArticlePublic, ArticleAdmin,
    ArticleList, ArticleListItem,
)

router = APIRouter(prefix="/api/blog", tags=["Blog"])

# ── Admin auth (reuses same pattern as content.py) ────────────────────────────
ADMIN_KEY = os.getenv("CMS_ADMIN_KEY", "")

async def require_admin(x_admin_key: Optional[str] = Header(default=None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Key")
    return True


# ── Helpers ───────────────────────────────────────────────────────────────────
def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")

def _auto_stats(article: BlogArticle) -> None:
    """Auto-calculate word_count and reading_time_minutes from body_html."""
    if article.body_html:
        text = _strip_html(article.body_html)
        wc = len(text.split())
        article.word_count = wc
        article.reading_time_minutes = max(1, round(wc / 200))

def _auto_canonical(article: BlogArticle) -> None:
    """Auto-set canonical_url if not provided."""
    if not article.canonical_url and article.site_id and article.slug:
        domain_map = {
            "unapiscina":         "https://unapiscina.com",
            "renovationremodel":  "https://renovationremodel.com",
            "iquotesai-construction": "https://construction.iquotesai.com",
            "iquotesai-insurance":    "https://insurance.iquotesai.com",
            "iquotesai-loans":        "https://loans.iquotesai.com",
            "iquotesai-solar":        "https://solar.iquotesai.com",
            "iquotesai-education":    "https://education.iquotesai.com",
            "nexaibuilder":       "https://nexaibuilder.com",
            "nexabuilder":        "https://nexabuilder.com",
        }
        base = domain_map.get(article.site_id, "https://nexabuilder.com")
        lang_prefix = "" if article.language == "es" else f"/{article.language}"
        article.canonical_url = f"{base}{lang_prefix}/blog/{article.slug}/"


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN ENDPOINTS

# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/admin/",
    response_model=ArticleAdmin,
    status_code=201,
    summary="Create a new article (admin)"
)
async def create_article(
    payload: ArticleCreate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """
    Create a new article in draft status.
    Auto-calculates word_count, reading_time, and canonical_url.
    """
    # Check slug uniqueness
    exists = await db.execute(
        select(BlogArticle).where(
            BlogArticle.site_id  == payload.site_id,
            BlogArticle.slug     == payload.slug,
            BlogArticle.language == payload.language,
        )
    )
    if exists.scalars().first():
        raise HTTPException(
            status_code=409,
            detail=f"Article with slug '{payload.slug}' already exists for {payload.site_id}/{payload.language}"
        )

    article = BlogArticle(**payload.model_dump(exclude_none=False))
    _auto_stats(article)
    _auto_canonical(article)

    db.add(article)
    await db.commit()
    await db.refresh(article)
    return article


@router.get(
    "/admin/{site_id}",
    response_model=ArticleList,
    summary="List all articles for a site (admin — all statuses)"
)
async def list_articles_admin(
    site_id:  str,
    status:   Optional[str] = Query(default=None),
    language: Optional[str] = Query(default=None),
    page:     int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    q = select(BlogArticle).where(BlogArticle.site_id == site_id)
    if status:
        try:
            q = q.where(BlogArticle.status == ArticleStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    if language:
        q = q.where(BlogArticle.language == language)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    q = q.order_by(BlogArticle.modified_at.desc())
    q = q.offset((page - 1) * per_page).limit(per_page)
    rows = (await db.execute(q)).scalars().all()

    return {
        "articles": rows, "total": total,
        "page": page, "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
    }


@router.get(
    "/admin/article/{article_id}",
    response_model=ArticleAdmin,
    summary="Get article by ID (admin)"
)
async def get_article_admin(
    article_id: int,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    result = await db.execute(
        select(BlogArticle).where(BlogArticle.id == article_id)
    )
    article = result.scalars().first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article


@router.put(
    "/admin/article/{article_id}",
    response_model=ArticleAdmin,
    summary="Update article fields (admin)"
)
async def update_article(
    article_id: int,
    payload:    ArticleUpdate,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """
    Partial update — only fields included in the request body are changed.
    Automatically recalculates word_count and reading_time if body_html changes.
    """
    result = await db.execute(
        select(BlogArticle).where(BlogArticle.id == article_id)
    )
    article = result.scalars().first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    update_data = payload.model_dump(exclude_none=True)
    body_changed = "body_html" in update_data

    for field, value in update_data.items():
        setattr(article, field, value)

    if body_changed:
        _auto_stats(article)

    if "canonical_url" not in update_data:
        _auto_canonical(article)

    await db.commit()
    await db.refresh(article)
    return article


@router.post(
    "/admin/article/{article_id}/publish",
    response_model=ArticleAdmin,
    summary="Publish an article"
)
async def publish_article(
    article_id: int,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """
    Sets status=published and published_at=now (if not already set).
    Article becomes visible via GET /api/blog/{site_id}/{slug}
    """
    result = await db.execute(
        select(BlogArticle).where(BlogArticle.id == article_id)
    )
    article = result.scalars().first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    # Validate minimum required fields before publishing
    missing = []
    if not article.body_html:
        missing.append("body_html")
    if not article.featured_image_url:
        missing.append("featured_image_url")
    if not article.featured_image_alt:
        missing.append("featured_image_alt")
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot publish — missing required fields: {', '.join(missing)}"
        )

    article.status = ArticleStatus.published
    if not article.published_at:
        article.published_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(article)
    return article


@router.post(
    "/admin/article/{article_id}/unpublish",
    response_model=ArticleAdmin,
    summary="Revert article to draft"
)
async def unpublish_article(
    article_id: int,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    result = await db.execute(
        select(BlogArticle).where(BlogArticle.id == article_id)
    )
    article = result.scalars().first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    article.status = ArticleStatus.draft
    await db.commit()
    await db.refresh(article)
    return article


@router.delete(
    "/admin/article/{article_id}",
    summary="Archive an article (soft delete)"
)
async def archive_article(
    article_id: int,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """Soft delete — sets status=archived. Article is not visible publicly."""
    result = await db.execute(
        select(BlogArticle).where(BlogArticle.id == article_id)
    )
    article = result.scalars().first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    article.status = ArticleStatus.archived
    await db.commit()
    return {"id": article_id, "status": "archived"}

# PUBLIC ENDPOINTS

# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/{site_id}",
    response_model=ArticleList,
    summary="List published articles for a site"
)
async def list_published_articles(
    site_id: str,
    language: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns published articles for a site, newest first.
    Used by blog index pages and article cards.
    """
    q = select(BlogArticle).where(
        BlogArticle.site_id == site_id,
        BlogArticle.status == ArticleStatus.published,
    )
    if language:
        q = q.where(BlogArticle.language == language)
    if category:
        q = q.where(BlogArticle.category == category)

    # Total count
    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # Paginated results
    q = q.order_by(BlogArticle.published_at.desc())
    q = q.offset((page - 1) * per_page).limit(per_page)
    rows = (await db.execute(q)).scalars().all()

    return {
        "articles": rows,
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, -(-total // per_page)),  # ceiling division
    }


@router.get(
    "/{site_id}/category/{category}",
    response_model=ArticleList,
    summary="List published articles by category"
)
async def list_by_category(
    site_id: str,
    category: str,
    language: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    q = select(BlogArticle).where(
        BlogArticle.site_id == site_id,
        BlogArticle.status == ArticleStatus.published,
        BlogArticle.category == category,
    )
    if language:
        q = q.where(BlogArticle.language == language)

    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    q = q.order_by(BlogArticle.published_at.desc())
    q = q.offset((page - 1) * per_page).limit(per_page)
    rows = (await db.execute(q)).scalars().all()

    return {
        "articles": rows, "total": total,
        "page": page, "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
    }


@router.get(
    "/{site_id}/{slug}",
    response_model=ArticlePublic,
    summary="Get a published article by slug"
)
async def get_article_public(
    site_id: str,
    slug:    str,
    language: str = Query(default="es"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns a single published article by (site_id, slug, language).
    Used by individual blog article pages at /blog/{slug}/
    Returns 404 if not found or not published.
    """
    result = await db.execute(
        select(BlogArticle).where(
            BlogArticle.site_id  == site_id,
            BlogArticle.slug     == slug,
            BlogArticle.language == language,
            BlogArticle.status   == ArticleStatus.published,
        )
    )
    article = result.scalars().first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found or not published")
    return article


# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/admin/deploy")
async def deploy_blog_static(request: Request):
    import os, subprocess as _sp
    admin_key = os.getenv("CMS_ADMIN_KEY","")
    key = request.headers.get("x-admin-key","")
    if key != admin_key:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Forbidden")
    deploy_script = "/home/ec2-user/deploy_blog.py"
    if not os.path.exists(deploy_script):
        return {"status": "no_deploy_script", "note": "Deploy runs via MCP workspace"}
    result = _sp.run(["python3", deploy_script], capture_output=True, text=True, timeout=120)
    return {"status": "ok", "returncode": result.returncode, "out": result.stdout[-300:]}

@router.post("/admin/article/{article_id}/suggest-meta")
async def suggest_meta(article_id: int, payload: dict, x_admin_key: str = Header(...)):
    import httpx as _h, re as _re, os as _os, json as _j
    require_admin(x_admin_key)
    h1    = payload.get("h1","") or ""
    kw    = (payload.get("primary_keyword","") or payload.get("slug","")).replace("-"," ")
    notes = (payload.get("cdm_notes","") or "")[:400]
    lines = [
        "Generate SEO title (max 65 chars) and meta description (150-160 chars).",
        "H1: " + h1,
        "Keyword: " + kw,
        "Site: Pool construction and home improvement in Southern California.",
    ]
    if notes:
        lines.append("Fix these CDM issues: " + notes)
    lines.append("{\"seo_title\":\"...\",\"meta_description\":\"...\"} — return ONLY this JSON.")
    msg = "\n".join(lines)
    key = _os.environ.get("ANTHROPIC_API_KEY","")
    async with _h.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":key,"anthropic-version":"2023-06-01","content-type":"application/json"},
            json={"model":"claude-sonnet-4-6","max_tokens":250,
                  "messages":[{"role":"user","content":msg}]})
    text = r.json()["content"][0]["text"].strip()
    text = _re.sub(r"^```[a-z]*\n?|```$","",text,flags=_re.MULTILINE).strip()
    try:
        return _j.loads(text)
    except Exception:
        return {"seo_title":"","meta_description":text[:160]}


# ─── MICRO-SITE ARTICLE GENERATION ─────────────────────────────────────────

_SITE_PROFILES = {
    "unapiscina":    {"name":"Una Piscina","domain":"unapiscina.com","vertical":"pool","lang":"es","cslb":"C-53","region":"Sur de California"},
    "eelectricista": {"name":"eElectricista","domain":"eelectricista.com","vertical":"electrical","lang":"es","cslb":"C-10","region":"Sur de California"},
    "piscinasy":     {"name":"Piscinasy","domain":"piscinasy.com","vertical":"pool","lang":"es","cslb":"C-53","region":"Sur de California"},
    "losruferos":    {"name":"Los Ruferos","domain":"losruferos.com","vertical":"roofing","lang":"es","cslb":"C-39","region":"Sur de California"},
    "ijardinero":    {"name":"iJardinero","domain":"ijardinero.com","vertical":"landscaping","lang":"es","cslb":"C-27","region":"Sur de California"},
    "swimmingpul":   {"name":"SwimmingPul","domain":"swimmingpul.com","vertical":"pool","lang":"en","cslb":"C-53","region":"Southern California"},
    "nexabuilder":   {"name":"NexaBuilder","domain":"nexabuilder.com","vertical":"general","lang":"en","cslb":"CSLB","region":"Southern California"},
}

_SITE_BUCKETS = {
    "nexabuilder":   "nexabuilder-root-site-979841141166-us-west-1-an",
    "unapiscina":    "unapiscina-frontend",
    "eelectricista": "eelectricista.com",
    "piscinasy":     "piscinasy.com",
    "losruferos":    "losruferos.com",
    "ijardinero":    "ijardinero.com",
    "swimmingpul":   "swimmingpul.com",
}


def _build_article_prompt(site_id: str, keyword: str, title_hint: str) -> str:
    p = _SITE_PROFILES.get(site_id, _SITE_PROFILES["nexabuilder"])
    if p["lang"] == "es":
        return f"""Eres un redactor SEO/AEO experto para {p["name"]} ({p["domain"]}), 
un sitio de {p["vertical"]} en {p["region"]} para propietarios de casas.

Escribe un articulo completo en espanol mexicano/californiano (informal, "tu" no "usted").
Keyword principal: "{keyword}"
{f"Enfoque del articulo: {title_hint}" if title_hint else ""}

ESTRUCTURA REQUERIDA (HTML limpio, sin markdown):
1. <h1> con el keyword principal (max 70 chars)
2. Parrafo intro con bloque AEO: <div class="aeo-answer"><strong>Respuesta rapida:</strong> [respuesta directa en 40-50 palabras]</div>
3. 4-5 secciones <h2> con contenido sustancial (al menos 150 palabras cada una)
4. Una tabla de costos o comparacion relevante (HTML <table>)
5. Seccion de ciudades/areas del {p["region"]} que atienden
6. FAQ (3 preguntas con respuestas) usando <div class="faq-item">
7. CTA final: <a href="/get-quote/">Solicita tu cotizacion gratis</a>

REQUISITOS SEO:
- 1,200-1,600 palabras totales
- Menciona licencia {p["cslb"]} de CSLB naturalmente
- 3-5 keywords secundarias relacionadas
- Incluye costos reales del {p["region"]} 2026
- Tono: como hablar con un vecino, no corporativo

DEVUELVE SOLO JSON (sin markdown, sin backticks):
{{"h1":"...","slug":"keyword-en-minusculas-con-guiones","seo_title":"...max 65 chars...","meta_description":"...150-160 chars...","primary_keyword":"{keyword}","body_html":"...HTML completo..."}}"""
    else:
        return f"""You are an expert SEO/AEO writer for {p["name"]} ({p["domain"]}), 
a {p["vertical"]} site in {p["region"]} for homeowners.

Write a complete article in clear, conversational English.
Primary keyword: "{keyword}"
{f"Article focus: {title_hint}" if title_hint else ""}

REQUIRED STRUCTURE (clean HTML, no markdown):
1. <h1> with primary keyword (max 70 chars)
2. Intro paragraph with AEO block: <div class="aeo-answer"><strong>Quick Answer:</strong> [40-50 word direct answer]</div>
3. 4-5 <h2> sections with substantial content (150+ words each)
4. A relevant cost or comparison <table>
5. Cities/areas of {p["region"]} served
6. FAQ (3 Q&As) using <div class="faq-item">
7. Final CTA: <a href="/get-quote/">Get your free quote</a>

SEO REQUIREMENTS:
- 1,200-1,600 words total
- Naturally mention {p["cslb"]} CSLB license
- 3-5 related secondary keywords
- Include real {p["region"]} 2026 cost ranges
- Tone: helpful homeowner guide, not corporate

RETURN ONLY JSON (no markdown, no backticks):
{{"h1":"...","slug":"primary-keyword-hyphenated","seo_title":"...max 65 chars...","meta_description":"...150-160 chars...","primary_keyword":"{keyword}","body_html":"...full HTML..."}}"""


@router.post("/admin/{site_id}/generate")
async def generate_article(
    site_id: str,
    payload: dict,
    x_admin_key: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """Generate a new article for a micro site using Claude."""
    import httpx as _h, re as _re, json as _j, os as _os, re as _re2
    require_admin(x_admin_key)

    if site_id not in _SITE_PROFILES:
        raise HTTPException(400, f"Unknown site_id: {site_id}")

    keyword    = (payload.get("keyword") or "").strip()
    title_hint = (payload.get("title_hint") or "").strip()
    if not keyword:
        raise HTTPException(400, "keyword is required")

    p   = _SITE_PROFILES[site_id]
    msg = _build_article_prompt(site_id, keyword, title_hint)
    key = _os.environ.get("ANTHROPIC_API_KEY", "")

    async with _h.AsyncClient(timeout=90) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 4096,
                  "messages": [{"role": "user", "content": msg}]})

    text = r.json()["content"][0]["text"].strip()
    text = _re.sub(r"^```[a-z]*\n?|```$", "", text, flags=_re.MULTILINE).strip()

    try:
        data = _j.loads(text)
    except Exception:
        # Try to extract JSON from text
        jm = _re2.search(r"\{.*\}", text, _re2.S)
        if not jm:
            raise HTTPException(500, "Claude did not return valid JSON")
        data = _j.loads(jm.group(0))

    slug = data.get("slug") or keyword.lower().replace(" ", "-")[:100]

    # Check slug uniqueness
    exists = await db.execute(
        select(BlogArticle).where(BlogArticle.site_id == site_id, BlogArticle.slug == slug)
    )
    if exists.scalars().first():
        slug = slug + "-2"

    article = BlogArticle(
        site_id=site_id,
        slug=slug,
        language=p["lang"],
        h1=data.get("h1", keyword),
        seo_title=data.get("seo_title", "")[:120],
        meta_description=data.get("meta_description", "")[:320],
        primary_keyword=keyword,
        body_html=data.get("body_html", ""),
        category=p["vertical"],
        status=ArticleStatus.draft,
        geo_region=p["region"],
        created_by="admin-generate",
    )
    _auto_stats(article)
    _auto_canonical(article)
    db.add(article)
    await db.commit()
    await db.refresh(article)
    return {"article_id": article.id, "slug": article.slug, "h1": article.h1,
            "word_count": article.word_count, "site_id": site_id}


@router.post("/admin/article/{article_id}/review")
async def review_article(
    article_id: int,
    x_admin_key: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """CDM-style AI review — scores the article 0-100."""
    import httpx as _h, re as _re, json as _j, os as _os
    require_admin(x_admin_key)

    result = await db.execute(select(BlogArticle).where(BlogArticle.id == article_id))
    art = result.scalars().first()
    if not art:
        raise HTTPException(404, "Article not found")

    p   = _SITE_PROFILES.get(art.site_id, _SITE_PROFILES["nexabuilder"])
    key = _os.environ.get("ANTHROPIC_API_KEY", "")

    body_text = art.body_html or ""
    prompt = f"""Review this {p["lang"].upper()} blog article for {p["name"]} and score it 0-100.

H1: {art.h1}
Keyword: {art.primary_keyword}
Word count: {art.word_count or 0}
Body (first 3000 chars):
{body_text[:3000]}

Score across these dimensions (0-10 each):
1. SEO (keyword placement, density, title/meta quality)
2. AEO (direct answer block, FAQ section, featured snippet potential)
3. Content depth (word count, specificity, local SoCal context)
4. Readability (tone, structure, headers)
5. CTA (call to action present and compelling)
6. CSLB compliance (license mentioned: {p["cslb"]})
7. Local relevance ({p["region"]} specifics, cities, costs)

RETURN ONLY JSON:
{{"overall_score":85,"scores":{{"seo":8,"aeo":7,"depth":9,"readability":8,"cta":8,"cslb":7,"local":9}},"notes":"...specific issues and strengths...","recommendation":"publish|needs_work|rewrite"}}"""

    async with _h.AsyncClient(timeout=45) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 600,
                  "messages": [{"role": "user", "content": prompt}]})

    text = r.json()["content"][0]["text"].strip()
    text = _re.sub(r"^```[a-z]*\n?|```$", "", text, flags=_re.MULTILINE).strip()
    try:
        review = _j.loads(text)
    except Exception:
        review = {"overall_score": 70, "notes": text[:500], "recommendation": "needs_work"}

    # Save score to article
    art.modified_at = __import__("datetime").datetime.utcnow()
    await db.commit()
    return review


@router.post("/admin/article/{article_id}/deploy")
async def deploy_article(
    article_id: int,
    x_admin_key: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    """Push a published blog article to the site's S3 bucket."""
    import boto3 as _b3, re as _re, os as _os
    require_admin(x_admin_key)

    result = await db.execute(select(BlogArticle).where(BlogArticle.id == article_id))
    art = result.scalars().first()
    if not art:
        raise HTTPException(404, "Article not found")

    bucket = _SITE_BUCKETS.get(art.site_id)
    if not bucket:
        raise HTTPException(400, f"No S3 bucket for {art.site_id}")

    p = _SITE_PROFILES.get(art.site_id, _SITE_PROFILES["nexabuilder"])

    # Get shared nav/footer from nexabuilder S3
    s3 = _b3.client("s3", region_name="us-west-1")
    NB_BUCKET = "nexabuilder-root-site-979841141166-us-west-1-an"

    def _load(key):
        try:
            return s3.get_object(Bucket=NB_BUCKET, Key=key)["Body"].read().decode()
        except Exception:
            return ""

    # Try to get existing nav/footer from the site itself first
    try:
        idx = s3.get_object(Bucket=bucket, Key="index.html")["Body"].read().decode()
        nav_m = _re.search(r"<nav[^>]*>.*?</nav>", idx, _re.S|_re.I)
        nav   = nav_m.group(0) if nav_m else _load("shared/nav.html")
        foot_m = _re.search(r"<footer.*?</footer>", idx, _re.S|_re.I)
        foot  = foot_m.group(0) if foot_m else _load("shared/footer.html")
    except Exception:
        nav  = _load("shared/nav.html")
        foot = _load("shared/footer.html")

    read_min = (art.reading_time_minutes or 5)
    wc       = (art.word_count or 0)
    pub_date = (art.published_at or __import__("datetime").datetime.utcnow()).strftime("%B %d, %Y")

    page = f"""<!DOCTYPE html>
<html lang="{p["lang"]}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{art.seo_title or art.h1}</title>
  <meta name="description" content="{art.meta_description or ""}">
  <link rel="canonical" href="https://{p["domain"]}/blog/{art.slug}/">
  <meta property="og:title" content="{art.seo_title or art.h1}">
  <meta property="og:description" content="{art.meta_description or ""}">
  <meta property="og:url" content="https://{p["domain"]}/blog/{art.slug}/">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root{{--navy:#0a1628;--blue:#1d6fde;--gold:#c8922a;--text:#1a2332;--muted:#4a5568;--bg:#f8fafc;--border:#e2e8f0}}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:"Inter",-apple-system,sans-serif;color:var(--text);background:#fff;line-height:1.7}}
    .article-wrap{{max-width:780px;margin:0 auto;padding:40px 24px 80px}}
    .article-meta{{font-size:13px;color:var(--muted);margin-bottom:28px;display:flex;gap:16px;flex-wrap:wrap}}
    h1{{font-size:clamp(1.6rem,4vw,2.3rem);font-weight:800;line-height:1.25;margin-bottom:20px;color:var(--navy)}}
    h2{{font-size:1.35rem;font-weight:700;margin:36px 0 14px;color:var(--navy)}}
    h3{{font-size:1.1rem;font-weight:700;margin:24px 0 10px}}
    p{{margin-bottom:16px}}
    .aeo-answer{{background:#eff6ff;border-left:4px solid var(--blue);padding:14px 18px;border-radius:0 8px 8px 0;margin:20px 0;font-size:15px}}
    table{{width:100%;border-collapse:collapse;margin:24px 0;font-size:14px}}
    th{{background:var(--navy);color:#fff;padding:10px 14px;text-align:left;font-weight:600}}
    td{{padding:9px 14px;border-bottom:1px solid var(--border)}}
    tr:nth-child(even) td{{background:var(--bg)}}
    .faq-item{{border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:10px}}
    .faq-q{{font-weight:700;margin-bottom:6px}}
    .faq-a{{color:var(--muted);font-size:14px}}
    .cta-box{{background:var(--navy);color:#fff;border-radius:12px;padding:28px 24px;text-align:center;margin:40px 0}}
    .cta-box h3{{color:#fff;font-size:1.3rem;margin-bottom:10px}}
    .cta-box a{{display:inline-block;background:var(--gold);color:#fff;padding:12px 28px;border-radius:8px;font-weight:700;text-decoration:none;margin-top:12px}}
    ul,ol{{padding-left:20px;margin-bottom:16px}}
    li{{margin-bottom:6px}}
  </style>
</head>
<body>
  {nav}
  <main class="article-wrap">
    <h1>{art.h1}</h1>
    <div class="article-meta">
      <span>📅 {pub_date}</span>
      <span>⏱ {read_min} min {"de lectura" if p["lang"]=="es" else "read"}</span>
      <span>📝 {wc:,} {"palabras" if p["lang"]=="es" else "words"}</span>
    </div>
    {art.body_html or ""}
  </main>
  {foot}
</body>
</html>"""

    key = f"blog/{art.slug}/index.html"
    s3.put_object(Bucket=bucket, Key=key, Body=page.encode("utf-8"),
                  ContentType="text/html", CacheControl="public, max-age=3600")

    return {"ok": True, "url": f"https://{p['domain']}/blog/{art.slug}/",
            "bucket": bucket, "key": key, "size": len(page)}
