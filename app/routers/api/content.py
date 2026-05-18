"""
app/routers/api/content.py
===========================
Headless CMS endpoints for the nexabuilder-site static pages.

Public endpoint (used by pages on load):
    GET  /api/content/{tenant_id}/{page_slug:path}/{block_key}
         Returns the published block value — or 404 if not set.
         Used by swap.js to replace SVG placeholders with real content.

Admin endpoints (require Authorization header):
    PUT  /api/content/admin/{tenant_id}/{page_slug:path}/{block_key}
         Upsert a block (create or update). Increments version on update.
    GET  /api/content/admin/{tenant_id}/{page_slug:path}
         List all blocks for a given page (draft + published).
    POST /api/content/admin/{tenant_id}/{page_slug:path}/{block_key}/publish
         Toggle is_published = True for a specific block.
    POST /api/content/admin/{tenant_id}/{page_slug:path}/{block_key}/unpublish
         Toggle is_published = False.
    GET  /api/content/admin/{tenant_id}/pages
         List all unique page_slugs that have at least one content block.

Design notes:
- No CMS UI yet — that is Step 4 of the CMS shell build (admin tabbed editor).
- All upserts are idempotent: PUT always works, increments version if updating.
- is_published acts as a draft/live gate: GET public only returns published blocks.
- tenant_id in the URL lets multi-tenant sites share one DB (nexabuilder vs unapiscina).
- The X-Admin-Key header is a simple shared secret stored in SSM.
  Replace with full JWT auth when the CMS shell is ready.
"""

import os
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional

from app.db import get_db
from app.models.content_block import ContentBlock
from app.schemas.content_block import (
    ContentBlockPublic,
    ContentBlockUpsert,
    ContentBlockAdmin,
    ContentBlockList,
)

router = APIRouter(prefix="/api/content", tags=["CMS"])

# ── Simple admin auth ─────────────────────────────────────────────────────────
ADMIN_KEY = os.getenv("CMS_ADMIN_KEY", "")

async def require_admin(x_admin_key: Optional[str] = Header(default=None)):
    """Simple shared-secret guard for admin endpoints.
    Replace with JWT dependency when CMS shell auth is wired up."""
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Key")
    return True


# ── CORS headers for public endpoint (called from static site JS) ─────────────
def _cors(page_origin: Optional[str] = None) -> dict:
    allowed = [
        "https://nexabuilder.com",
        "https://www.nexabuilder.com",
        "https://unapiscina.com",
        "https://admin.nexabuilder.com",
        "http://localhost:3000",
        "http://127.0.0.1:5500",
    ]
    origin = page_origin if page_origin in allowed else "https://nexabuilder.com"
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, PUT, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Admin-Key",
        "Vary": "Origin",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/{tenant_id}/{page_slug:path}/{block_key}",
    response_model=ContentBlockPublic,
    summary="Get a published content block (used by site pages)"
)
async def get_block_public(
    tenant_id: str,
    page_slug: str,
    block_key: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the published value for a given (tenant, page, block_key) address.

    Called by swap.js on page load:
        fetch(`/api/content/nexabuilder/${encodeURIComponent(window.location.pathname)}/${key}`)

    Returns 404 if the block doesn't exist or is not published.
    The site page shows the SVG placeholder in that case — graceful degradation.
    """
    result = await db.execute(
        select(ContentBlock).where(
            ContentBlock.tenant_id == tenant_id,
            ContentBlock.page_slug == page_slug,
            ContentBlock.block_key == block_key,
            ContentBlock.is_published == True,
        )
    )
    block = result.scalars().first()
    if not block:
        raise HTTPException(status_code=404, detail="Block not found or not published")

    from fastapi.responses import JSONResponse
    return JSONResponse(
        content={
            "page_slug":    block.page_slug,
            "block_key":    block.block_key,
            "tenant_id":    block.tenant_id,
            "content_type": block.content_type.value,
            "value":        block.value,
            "alt_text":     block.alt_text,
        },
        headers=_cors(request.headers.get("origin"))
    )


@router.options("/{tenant_id}/{page_slug:path}/{block_key}", include_in_schema=False)
async def options_block(tenant_id: str, page_slug: str, block_key: str, request: Request):
    from fastapi.responses import Response
    return Response(headers=_cors(request.headers.get("origin")))


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN ENDPOINTS  (X-Admin-Key required)
# ═══════════════════════════════════════════════════════════════════════════════

@router.put(
    "/admin/{tenant_id}/{page_slug:path}/{block_key}",
    response_model=ContentBlockAdmin,
    summary="Upsert a content block (admin)"
)
async def upsert_block(
    tenant_id: str,
    page_slug: str,
    block_key: str,
    payload: ContentBlockUpsert,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """
    Create or update a content block.

    - If the block doesn't exist → creates it (version=1).
    - If it exists → updates value/type/alt_text, increments version.
    - is_published can be set here or via the dedicated /publish endpoint.

    Typical use:
        PUT /api/content/admin/nexabuilder/%2F/hero_image
        Body: {"content_type":"image_url","value":"https://cdn.nexabuilder.com/hero.jpg","is_published":true}
    """
    result = await db.execute(
        select(ContentBlock).where(
            ContentBlock.tenant_id == tenant_id,
            ContentBlock.page_slug == page_slug,
            ContentBlock.block_key == block_key,
        )
    )
    block = result.scalars().first()

    if block:
        # Update existing
        block.content_type = payload.content_type
        block.value        = payload.value
        block.alt_text     = payload.alt_text
        block.is_published = payload.is_published
        block.version      = block.version + 1
    else:
        # Create new
        block = ContentBlock(
            tenant_id    = tenant_id,
            page_slug    = page_slug,
            block_key    = block_key,
            content_type = payload.content_type,
            value        = payload.value,
            alt_text     = payload.alt_text,
            is_published = payload.is_published,
            version      = 1,
        )
        db.add(block)

    await db.commit()
    await db.refresh(block)
    return block


@router.get(
    "/admin/{tenant_id}/{page_slug:path}",
    response_model=ContentBlockList,
    summary="List all blocks for a page (admin)"
)
async def list_page_blocks(
    tenant_id: str,
    page_slug: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """
    Returns all blocks (draft + published) for a given page.
    Used by the CMS same-page editor (Step 4 admin shell).
    """
    result = await db.execute(
        select(ContentBlock).where(
            ContentBlock.tenant_id == tenant_id,
            ContentBlock.page_slug == page_slug,
        ).order_by(ContentBlock.block_key)
    )
    blocks = result.scalars().all()
    return {"page_slug": page_slug, "tenant_id": tenant_id, "blocks": blocks, "total": len(blocks)}


@router.post(
    "/admin/{tenant_id}/{page_slug:path}/{block_key}/publish",
    response_model=ContentBlockAdmin,
    summary="Publish a content block"
)
async def publish_block(
    tenant_id: str,
    page_slug: str,
    block_key: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """Set is_published=True. The public GET endpoint will now return this block."""
    result = await db.execute(
        select(ContentBlock).where(
            ContentBlock.tenant_id == tenant_id,
            ContentBlock.page_slug == page_slug,
            ContentBlock.block_key == block_key,
        )
    )
    block = result.scalars().first()
    if not block:
        raise HTTPException(status_code=404, detail="Block not found")
    block.is_published = True
    await db.commit()
    await db.refresh(block)
    return block


@router.post(
    "/admin/{tenant_id}/{page_slug:path}/{block_key}/unpublish",
    response_model=ContentBlockAdmin,
    summary="Unpublish a content block (revert to placeholder)"
)
async def unpublish_block(
    tenant_id: str,
    page_slug: str,
    block_key: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """Set is_published=False. The page reverts to showing the SVG placeholder."""
    result = await db.execute(
        select(ContentBlock).where(
            ContentBlock.tenant_id == tenant_id,
            ContentBlock.page_slug == page_slug,
            ContentBlock.block_key == block_key,
        )
    )
    block = result.scalars().first()
    if not block:
        raise HTTPException(status_code=404, detail="Block not found")
    block.is_published = False
    await db.commit()
    await db.refresh(block)
    return block


@router.get(
    "/admin/{tenant_id}/pages",
    summary="List all pages that have at least one content block (admin)"
)
async def list_pages(
    tenant_id: str,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    """
    Returns a distinct list of page_slugs for a tenant.
    Used by the CMS admin to populate the page picker sidebar.
    """
    result = await db.execute(
        select(ContentBlock.page_slug, func.count(ContentBlock.id).label("block_count"))
        .where(ContentBlock.tenant_id == tenant_id)
        .group_by(ContentBlock.page_slug)
        .order_by(ContentBlock.page_slug)
    )
    rows = result.all()
    return {
        "tenant_id": tenant_id,
        "pages": [{"page_slug": r.page_slug, "block_count": r.block_count} for r in rows],
        "total": len(rows),
    }
