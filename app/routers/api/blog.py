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
    _require_admin(x_admin_key)
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
