import os
from fastapi import APIRouter, Depends, HTTPException, Header, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional
from app.db import get_db
from app.models.content_block import ContentBlock
from app.schemas.content_block import ContentBlockPublic, ContentBlockUpsert

ADMIN_KEY = os.getenv("CMS_ADMIN_KEY", "")
CORS_ORIGINS = ["https://nexabuilder.com","https://www.nexabuilder.com","https://unapiscina.com","https://admin.nexabuilder.com","http://localhost:3000","http://127.0.0.1:5500"]

def _cors(origin):
    allowed = origin if origin in CORS_ORIGINS else CORS_ORIGINS[0]
    return {"Access-Control-Allow-Origin": allowed, "Vary": "Origin"}

def _ct(b):
    ct = b.content_type
    if hasattr(ct, 'value'):
        return ct.value
    s = str(ct)
    return s.split(".")[-1] if "." in s else s

def _row(b):
    return {"id":b.id,"tenant_id":b.tenant_id,"page_slug":b.page_slug,"block_key":b.block_key,"content_type":_ct(b),"value":b.value,"alt_text":b.alt_text,"is_published":b.is_published,"version":b.version,"updated_by":getattr(b,"updated_by",None),"created_at":b.created_at.isoformat() if b.created_at else None,"updated_at":b.updated_at.isoformat() if b.updated_at else None}

async def require_admin(x_admin_key: Optional[str] = Header(default=None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Key")
    return True

cms_admin_router = APIRouter(prefix="/api/cms", tags=["CMS Admin"])

@cms_admin_router.get("/{tenant_id}/pages")
async def list_pages(tenant_id: str, db: AsyncSession = Depends(get_db), _: bool = Depends(require_admin)):
    rows = (await db.execute(select(ContentBlock.page_slug,func.count(ContentBlock.id).label("block_count"),func.count(ContentBlock.id).filter(ContentBlock.is_published==True).label("published")).where(ContentBlock.tenant_id==tenant_id).group_by(ContentBlock.page_slug).order_by(ContentBlock.page_slug))).all()
    return {"tenant_id":tenant_id,"pages":[{"slug":r.page_slug,"block_count":r.block_count,"published":r.published} for r in rows]}

@cms_admin_router.get("/{tenant_id}/{page_slug:path}")
async def list_blocks(tenant_id: str, page_slug: str, db: AsyncSession = Depends(get_db), _: bool = Depends(require_admin)):
    blocks = (await db.execute(select(ContentBlock).where(ContentBlock.tenant_id==tenant_id,ContentBlock.page_slug==page_slug).order_by(ContentBlock.block_key))).scalars().all()
    return [_row(b) for b in blocks]

@cms_admin_router.put("/{tenant_id}/{page_slug:path}/{block_key}")
async def upsert_block(tenant_id: str, page_slug: str, block_key: str, payload: ContentBlockUpsert, db: AsyncSession = Depends(get_db), _: bool = Depends(require_admin)):
    ct = str(payload.content_type or "text").split(".")[-1]
    block = (await db.execute(select(ContentBlock).where(ContentBlock.tenant_id==tenant_id,ContentBlock.page_slug==page_slug,ContentBlock.block_key==block_key))).scalars().first()
    if block:
        block.content_type = ct
        block.value = payload.value
        block.alt_text = payload.alt_text
        block.is_published = payload.is_published
        block.version = (block.version or 0) + 1
        if hasattr(block,"updated_by"):
            block.updated_by = getattr(payload,"updated_by","admin")
    else:
        kwargs = dict(tenant_id=tenant_id,page_slug=page_slug,block_key=block_key,content_type=ct,value=payload.value,alt_text=payload.alt_text,is_published=payload.is_published,version=1)
        if hasattr(ContentBlock,"updated_by"):
            kwargs["updated_by"] = getattr(payload,"updated_by","admin")
        block = ContentBlock(**kwargs)
        db.add(block)
    await db.commit()
    await db.refresh(block)
    return _row(block)

@cms_admin_router.post("/{tenant_id}/{page_slug:path}/{block_key}/publish")
async def publish_block(tenant_id: str, page_slug: str, block_key: str, db: AsyncSession = Depends(get_db), _: bool = Depends(require_admin)):
    block = (await db.execute(select(ContentBlock).where(ContentBlock.tenant_id==tenant_id,ContentBlock.page_slug==page_slug,ContentBlock.block_key==block_key))).scalars().first()
    if not block: raise HTTPException(status_code=404,detail="Block not found")
    block.is_published = True
    await db.commit()
    return {"status":"published","block_key":block_key}

@cms_admin_router.post("/{tenant_id}/{page_slug:path}/{block_key}/unpublish")
async def unpublish_block(tenant_id: str, page_slug: str, block_key: str, db: AsyncSession = Depends(get_db), _: bool = Depends(require_admin)):
    block = (await db.execute(select(ContentBlock).where(ContentBlock.tenant_id==tenant_id,ContentBlock.page_slug==page_slug,ContentBlock.block_key==block_key))).scalars().first()
    if not block: raise HTTPException(status_code=404,detail="Block not found")
    block.is_published = False
    await db.commit()
    return {"status":"unpublished","block_key":block_key}



@cms_admin_router.post("/{tenant_id}/{old_slug:path}/rename")
async def rename_page_slug(
    tenant_id: str,
    old_slug: str,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    _: bool = Depends(require_admin),
):
    new_slug = (payload or {}).get("new_slug", "").strip("/")
    if not new_slug:
        raise HTTPException(status_code=400, detail="new_slug required")

    blocks = (await db.execute(
        select(ContentBlock).where(
            ContentBlock.tenant_id == tenant_id,
            ContentBlock.page_slug == old_slug,
        )
    )).scalars().all()

    if not blocks:
        raise HTTPException(status_code=404, detail=f"No blocks found for {tenant_id}/{old_slug}")

    for block in blocks:
        block.page_slug = new_slug
    await db.commit()

    return {
        "status": "renamed",
        "tenant_id": tenant_id,
        "from_slug": old_slug,
        "to_slug": new_slug,
        "blocks_updated": len(blocks),
    }

cms_public_router = APIRouter(prefix="/api/content", tags=["CMS Public"])

@cms_public_router.options("/{tenant_id}/{page_slug:path}/{block_key}", include_in_schema=False)
async def preflight(tenant_id: str, page_slug: str, block_key: str, request: Request):
    return Response(headers={**_cors(request.headers.get("origin")),"Access-Control-Allow-Methods":"GET, OPTIONS","Access-Control-Allow-Headers":"Content-Type"})

@cms_public_router.get("/{tenant_id}/{page_slug:path}/{block_key}")
async def get_block_public(tenant_id: str, page_slug: str, block_key: str, request: Request, db: AsyncSession = Depends(get_db)):
    block = (await db.execute(select(ContentBlock).where(ContentBlock.tenant_id==tenant_id,ContentBlock.page_slug==page_slug,ContentBlock.block_key==block_key,ContentBlock.is_published==True))).scalars().first()
    if not block: raise HTTPException(status_code=404,detail="Block not found or not published")
    return JSONResponse(content={"page_slug":block.page_slug,"block_key":block.block_key,"tenant_id":block.tenant_id,"content_type":_ct(block),"value":block.value,"alt_text":block.alt_text},headers=_cors(request.headers.get("origin")))

router = cms_public_router
