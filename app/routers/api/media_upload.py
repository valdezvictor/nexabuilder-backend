"""Media Upload API for NexaBuilder CMS — media_upload.py"""
import os, re, uuid, mimetypes
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Header, UploadFile, File, Form
from pydantic import BaseModel, Field, field_validator
_BaseImgModel = BaseModel
from enum import Enum

router = APIRouter(prefix="/api/media", tags=["Media"])

ADMIN_KEY    = os.getenv("CMS_ADMIN_KEY", "")
REGION       = "us-west-1"
MEDIA_BUCKET = "nexabuilder-root-site-979841141166-us-west-1-an"
MEDIA_CDN    = "https://www.nexabuilder.com"
MEDIA_PREFIX = "media"
MAX_FILE_SIZE = 8 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg","image/jpg","image/png","image/webp","image/gif"}


class ImageType(str, Enum):
    MATERIAL    = "material"
    REAL_BEFORE = "real_before"
    REAL_AFTER  = "real_after"
    AI_AFTER    = "ai_after"

ALT_PREFIX = {
    ImageType.MATERIAL:    "",
    ImageType.REAL_BEFORE: "Unfinished",
    ImageType.REAL_AFTER:  "",
    ImageType.AI_AFTER:    "AI concept design showing",
}
ALT_TIPS = {
    ImageType.MATERIAL:    "Describe: material, color, texture, use. E.g. 'Charcoal grey basalt split-face stone tiles for outdoor feature walls.'",
    ImageType.REAL_BEFORE: "Start with 'Unfinished'. E.g. 'Unfinished concrete patio before natural flagstone paving installation.'",
    ImageType.REAL_AFTER:  "Describe completed work. E.g. 'Completed natural travertine pool coping with bullnose edge on Orange County pool.'",
    ImageType.AI_AFTER:    "MUST start: 'AI concept design showing'. E.g. 'AI concept design showing luxury travertine pool deck for SoCal home.'",
}

BAD_PREFIXES = ["image of ","photo of ","picture of ","this is ","a photo of "]

def clean_alt(v: str, atype: ImageType) -> str:
    v = v.strip()
    for bp in BAD_PREFIXES:
        if v.lower().startswith(bp): v = v[len(bp):]
    if atype == ImageType.AI_AFTER:
        req = ALT_PREFIX[ImageType.AI_AFTER]
        if not v.lower().startswith(req.lower()): v = f"{req} {v}"
    if atype == ImageType.REAL_BEFORE:
        if not v.lower().startswith("unfinished"): v = f"Unfinished {v.lower()}"
    return v[:125]

async def require_admin(x_admin_key: Optional[str] = Header(default=None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid X-Admin-Key")
    return True

def _s3(): import boto3; return boto3.client("s3", region_name=REGION)

def _db_session():
    from sqlalchemy import create_engine; from sqlalchemy.orm import sessionmaker
    url = os.getenv("DATABASE_URL","").replace("postgresql+asyncpg","postgresql+psycopg2")
    return sessionmaker(bind=create_engine(url, echo=False))()

def _s3key(tenant_id, page_slug, filename):
    safe_slug = re.sub(r'[^a-z0-9\-/]','',page_slug.lower())
    safe_name = re.sub(r'[^a-z0-9\-_.]','',filename.lower().replace(' ','-'))
    uid = uuid.uuid4().hex[:8]
    return f"{MEDIA_PREFIX}/{tenant_id}/{safe_slug}/{uid}-{safe_name}"

@router.post("/upload")
async def upload_media(
    file:              UploadFile = File(...),
    tenant_id:         str  = Form("nexabuilder"),
    page_slug:         str  = Form(...),
    block_key:         str  = Form(...),
    asset_type:        str  = Form("material"),
    alt_text:          str  = Form(""),
    description:       str  = Form(""),
    material_category: str  = Form(""),
    stone_type:        str  = Form(""),
    color_family:      str  = Form(""),
    style_category:    str  = Form(""),
    origin:            str  = Form(""),
    artisan_name:      str  = Form(""),
    artisan_story:     str  = Form(""),
    _: bool = Depends(require_admin),
):
    ct = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
    if ct not in ALLOWED_TYPES:
        raise HTTPException(400, f"File type not allowed: {ct}. Use JPEG, PNG, or WebP.")
    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(413, f"File too large ({len(data)//1024}KB). Max 8MB.")
    
    atype = ImageType(asset_type)
    cleaned_alt = clean_alt(alt_text, atype)
    
    s3 = _s3()
    s3k = _s3key(tenant_id, page_slug, file.filename or "image.jpg")
    cdn_url = f"{MEDIA_CDN}/{s3k}"
    
    try:
        s3.put_object(Bucket=MEDIA_BUCKET, Key=s3k, Body=data,
            ContentType=ct, CacheControl="public, max-age=31536000",
            Metadata={"tenant":tenant_id,"page":page_slug,"block":block_key,"asset-type":atype.value})
    except Exception as e:
        raise HTTPException(500, f"S3 upload failed: {str(e)[:100]}")
    
    w = h = None
    try:
        from PIL import Image as PI; import io
        img = PI.open(io.BytesIO(data)); w,h = img.size
    except Exception: pass
    
    from sqlalchemy import text as sqlt
    db = _db_session()
    asset_id = None
    try:
        row = db.execute(sqlt("""
            INSERT INTO construction_assets
              (tenant_id,page_slug,block_key,url,s3_key,asset_type,
               alt_text,description,material_category,stone_type,color_family,
               style_category,origin,artisan_name,artisan_story,
               ai_generated,width,height,file_size,mime_type,uploaded_by)
            VALUES (:t,:p,:b,:url,:s3k,:at,:alt,:desc,:mc,:st,:cf,:sc,:or,:an,:as_,:ai,:w,:h,:fs,:mime,'cms')
            RETURNING id
        """),{"t":tenant_id,"p":page_slug,"b":block_key,"url":cdn_url,"s3k":s3k,
              "at":atype.value,"alt":cleaned_alt,"desc":description,"mc":material_category,
              "st":stone_type,"cf":color_family,"sc":style_category,"or":origin,
              "an":artisan_name,"as_":artisan_story,"ai":atype==ImageType.AI_AFTER,
              "w":w,"h":h,"fs":len(data),"mime":ct})
        asset_id = row.scalar(); db.commit()
    except Exception as e:
        db.rollback()
        return {"status":"uploaded_no_db","url":cdn_url,"alt_text":cleaned_alt,"error":str(e)[:100]}
    finally:
        db.close()
    
    # Also update content_blocks so the publish pipeline picks up the new image URL
    db2 = _db_session()
    try:
        db2.execute(sqlt(
            "UPDATE content_blocks SET value=:url, alt_text=:alt, content_type='image_url', "
            "version=version+1, updated_by='media_upload' "
            "WHERE tenant_id=:t AND page_slug=:p AND block_key=:b"
        ), {"url": cdn_url, "alt": cleaned_alt, "t": tenant_id, "p": page_slug, "b": block_key})
        db2.commit()
    except Exception:
        pass  # non-fatal — asset is uploaded, content_block update is best-effort
    finally:
        db2.close()

    # Auto-publish: push content_blocks for this page to live S3 HTML
    try:
        from app.routers.api.seo_pipeline import (
            _get_s3, _fetch_html, slug_to_s3_key, _patch_content_block, _upload_html
        )
        from sqlalchemy import text as _sqlt2, create_engine as _ce2
        from sqlalchemy.orm import sessionmaker as _sm2
        from app.models.content_block import ContentBlock
        from sqlalchemy import select as _sel2
        _pub_s3 = _get_s3()
        _pub_key = slug_to_s3_key(page_slug)
        _pub_html = _fetch_html(_pub_s3, _pub_key)
        _pub_engine = _ce2(os.getenv("DATABASE_URL","").replace("postgresql+asyncpg","postgresql+psycopg2"),echo=False)
        with _sm2(bind=_pub_engine)() as _pub_session:
            _pub_blocks = _pub_session.execute(
                _sel2(ContentBlock).where(
                    ContentBlock.tenant_id==tenant_id,
                    ContentBlock.page_slug==page_slug,
                    ContentBlock.is_published==True,
                )
            ).scalars().all()
            _pub_changed = []
            for _pb in _pub_blocks:
                _pct = str(_pb.content_type).split(".")[-1]
                if _pct in ("text","html","image_url") and _pb.value:
                    _pnh, _pok = _patch_content_block(_pub_html, _pb.block_key, _pb.value, _pct, _pb.alt_text or "", _pb.page_slug)
                    if _pok:
                        _pub_html = _pnh
                        _pub_changed.append(_pb.block_key)
            if _pub_changed:
                _upload_html(_pub_s3, _pub_key, _pub_html)
    except Exception as _pub_err:
        pass  # Upload succeeded; publish is best-effort

    return {"status":"ok","id":asset_id,"url":cdn_url,"s3_key":s3k,
            "alt_text":cleaned_alt,"asset_type":atype.value,"width":w,"height":h,"file_size":len(data),
            "published":True}


@router.get("/assets/{page_slug:path}")
async def list_assets(page_slug: str, block_key: Optional[str] = None, _: bool = Depends(require_admin)):
    from sqlalchemy import text as sqlt
    db = _db_session()
    try:
        q = "SELECT id,url,s3_key,block_key,asset_type,alt_text,description,stone_type,color_family,style_category,origin,artisan_name,ai_generated,width,height,created_at FROM construction_assets WHERE page_slug=:p"
        params = {"p": page_slug}
        if block_key: q += " AND block_key=:b"; params["b"] = block_key
        q += " ORDER BY id ASC"
        rows = db.execute(sqlt(q), params).fetchall()
        cols = ["id","url","s3_key","block_key","asset_type","alt_text","description","stone_type","color_family","style_category","origin","artisan_name","ai_generated","width","height","created_at"]
        return {"assets": [dict(zip(cols,[str(v) if hasattr(v,'isoformat') else v for v in row])) for row in rows]}
    finally:
        db.close()


class UpdateAsset(BaseModel):
    asset_type:    Optional[str] = None
    alt_text:      Optional[str] = Field(None, max_length=125)
    description:   Optional[str] = None
    stone_type:    Optional[str] = None
    color_family:  Optional[str] = None
    style_category:Optional[str] = None
    origin:        Optional[str] = None
    artisan_name:  Optional[str] = None
    artisan_story: Optional[str] = None

@router.put("/asset/{asset_id}")
async def update_asset(asset_id: int, payload: UpdateAsset, _: bool = Depends(require_admin)):
    from sqlalchemy import text as sqlt; import datetime
    db = _db_session()
    try:
        upd = {k:v for k,v in payload.dict().items() if v is not None}
        if payload.alt_text is not None:
            upd["alt_text"] = clean_alt(payload.alt_text, ImageType(payload.asset_type or "material"))
        if not upd: return {"status":"noop"}
        set_clause = ", ".join(f"{k}=:{k}" for k in upd)
        upd.update({"id":asset_id,"now":datetime.datetime.utcnow()})
        db.execute(sqlt(f"UPDATE construction_assets SET {set_clause}, updated_at=:now WHERE id=:id"), upd)
        db.commit(); return {"status":"updated","id":asset_id}
    finally: db.close()

@router.delete("/asset/{asset_id}")
async def delete_asset(asset_id: int, _: bool = Depends(require_admin)):
    from sqlalchemy import text as sqlt
    db = _db_session()
    try:
        row = db.execute(sqlt("SELECT s3_key FROM construction_assets WHERE id=:id"),{"id":asset_id}).fetchone()
        if not row: raise HTTPException(404,"Asset not found")
        if row[0]:
            try: _s3().delete_object(Bucket=MEDIA_BUCKET, Key=row[0])
            except Exception: pass
        db.execute(sqlt("DELETE FROM construction_assets WHERE id=:id"),{"id":asset_id})
        db.commit(); return {"status":"deleted","id":asset_id}
    finally: db.close()

class AltRequest(BaseModel):
    asset_type:        str = "material"
    stone_type:        str = ""
    color_family:      str = ""
    style_category:    str = ""
    origin:            str = ""
    material_category: str = ""
    page_slug:         str = ""
    existing_alt:      str = ""

@router.post("/ai-alt")
async def gen_alt_text(payload: AltRequest, _: bool = Depends(require_admin)):
    atype = ImageType(payload.asset_type)
    prefix = ALT_PREFIX.get(atype,"")
    tip = ALT_TIPS.get(atype,"")
    ctx = ". ".join(p for p in [
        payload.stone_type and f"Stone: {payload.stone_type}",
        payload.color_family and f"Color: {payload.color_family}",
        payload.style_category and f"Style: {payload.style_category}",
        payload.origin and f"Origin: {payload.origin}",
    ] if p) or "Natural stone material for Southern California projects"
    page_ctx = payload.page_slug.replace("materials/stone/","").replace("-"," ").title()
    
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY",""))
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=80,
            system="You write SEO-compliant image alt text for construction materials. Output ONLY the alt text, max 125 chars, no quotes.",
            messages=[{"role":"user","content":
                f"Image type: {atype.value}\nContext: {ctx}\nPage: {page_ctx}\n"
                f"{'Must begin with: '+repr(prefix) if prefix else ''}\nGuideline: {tip}\n"
                f"{'Improve: '+payload.existing_alt if payload.existing_alt else ''}\nWrite alt text now:"}]
        )
        raw = msg.content[0].text.strip().strip('"')
        alt = clean_alt(raw, atype)
        return {"alt_text":alt,"char_count":len(alt),"asset_type":atype.value,"prefix_required":prefix,"tip":tip}
    except Exception as e:
        templates = {
            ImageType.MATERIAL:    f"{payload.stone_type or 'Natural stone'} {payload.color_family or ''} tiles for {page_ctx} projects in SoCal".strip(),
            ImageType.REAL_BEFORE: f"Unfinished site before {page_ctx.lower()} installation in Southern California",
            ImageType.REAL_AFTER:  f"Completed {page_ctx.lower()} installation in Southern California",
            ImageType.AI_AFTER:    f"AI concept design showing {page_ctx.lower()} {payload.style_category or ''} design for Southern California home".strip(),
        }
        alt = templates.get(atype,"Natural stone material for Southern California projects")[:125]
        return {"alt_text":alt,"char_count":len(alt),"asset_type":atype.value,"fallback":True}

# Export alt text constants for frontend reference
@router.get("/meta/alt-tips")
async def alt_tips():
    return {
        "types": [t.value for t in ImageType],
        "prefixes": {t.value: v for t,v in ALT_PREFIX.items()},
        "tips": {t.value: v for t,v in ALT_TIPS.items()},
        "max_chars": 125,
    }


class SaveArtisanRequest(BaseModel):
    tenant_id:     str = "nexabuilder"
    page_slug:     str
    block_key:     str
    asset_type:    str = "material"
    alt_text:      Optional[str] = None
    stone_type:    Optional[str] = None
    color_family:  Optional[str] = None
    style_category:Optional[str] = None
    origin:        Optional[str] = None
    artisan_name:  Optional[str] = None
    artisan_story: Optional[str] = None

@router.post("/save-artisan")
async def save_artisan(payload: SaveArtisanRequest, _: bool = Depends(require_admin)):
    """
    Persist artisan/metadata fields for an existing gallery image block and
    re-patch the live S3 HTML so data-* attrs + gc-meta-tags update immediately.
    Called by the CMS Save Block when no new image file is being uploaded.
    """
    from sqlalchemy import text as sqlt
    import datetime

    # 1 — Upsert into construction_assets
    db = _db_session()
    try:
        existing = db.execute(sqlt("""
            SELECT id FROM construction_assets
            WHERE page_slug=:p AND block_key=:b
            ORDER BY updated_at DESC LIMIT 1
        """), {"p": payload.page_slug, "b": payload.block_key}).fetchone()

        upd = {k: v for k, v in {
            "stone_type":     payload.stone_type,
            "color_family":   payload.color_family,
            "style_category": payload.style_category,
            "origin":         payload.origin,
            "artisan_name":   payload.artisan_name,
            "artisan_story":  payload.artisan_story,
        }.items() if v is not None}
        if payload.alt_text is not None:
            upd["alt_text"] = clean_alt(payload.alt_text, ImageType(payload.asset_type))

        if existing and upd:
            set_clause = ", ".join(f"{k}=:{k}" for k in upd)
            upd["id"]  = existing[0]
            upd["now"] = datetime.datetime.utcnow()
            db.execute(sqlt(
                f"UPDATE construction_assets SET {set_clause}, updated_at=:now WHERE id=:id"
            ), upd)
            db.commit()
        elif not existing and upd:
            cb = db.execute(sqlt(
                "SELECT value FROM content_blocks "
                "WHERE tenant_id=:t AND page_slug=:p AND block_key=:b LIMIT 1"
            ), {"t": payload.tenant_id, "p": payload.page_slug, "b": payload.block_key}).fetchone()
            db.execute(sqlt("""
                INSERT INTO construction_assets
                  (tenant_id,page_slug,block_key,url,asset_type,alt_text,
                   stone_type,color_family,style_category,origin,artisan_name,artisan_story,
                   ai_generated,uploaded_by)
                VALUES (:t,:p,:b,:url,:at,:alt,:st,:cf,:sc,:or,:an,:as_,false,'cms_save')
            """), {
                "t": payload.tenant_id, "p": payload.page_slug, "b": payload.block_key,
                "url": cb[0] if cb else "",
                "at": payload.asset_type,
                "alt": upd.get("alt_text",""),
                "st":  upd.get("stone_type",""),
                "cf":  upd.get("color_family",""),
                "sc":  upd.get("style_category",""),
                "or":  upd.get("origin",""),
                "an":  upd.get("artisan_name",""),
                "as_": upd.get("artisan_story",""),
            })
            db.commit()
    except Exception as e:
        db.rollback()
        return {"status": "db_error", "error": str(e)[:200]}
    finally:
        db.close()

    # 2 — Also update alt_text in content_blocks if provided
    if payload.alt_text:
        db3 = _db_session()
        try:
            cleaned = clean_alt(payload.alt_text, ImageType(payload.asset_type))
            db3.execute(sqlt(
                "UPDATE content_blocks "
                "SET alt_text=:alt, version=version+1, updated_by='save_artisan' "
                "WHERE tenant_id=:t AND page_slug=:p AND block_key=:b"
            ), {"alt": cleaned, "t": payload.tenant_id, "p": payload.page_slug, "b": payload.block_key})
            db3.commit()
        except Exception:
            pass
        finally:
            db3.close()

    # 3 — Re-patch live S3 HTML so data-* attrs and gc-meta-tags update immediately
    try:
        from app.routers.api.seo_pipeline import (
            _get_s3, _fetch_html, slug_to_s3_key, _patch_content_block, _upload_html
        )
        from app.models.content_block import ContentBlock
        from sqlalchemy import create_engine as _ce2, select as _sel2
        from sqlalchemy.orm import sessionmaker as _sm2

        _s3c  = _get_s3()
        _key  = slug_to_s3_key(payload.page_slug)
        _html = _fetch_html(_s3c, _key)

        _eng = _ce2(
            os.getenv("DATABASE_URL","").replace("postgresql+asyncpg","postgresql+psycopg2"),
            echo=False
        )
        with _sm2(bind=_eng)() as _sess:
            _blocks = _sess.execute(
                _sel2(ContentBlock).where(
                    ContentBlock.tenant_id  == payload.tenant_id,
                    ContentBlock.page_slug  == payload.page_slug,
                    ContentBlock.is_published == True,
                )
            ).scalars().all()

            _changed = []
            for _b in _blocks:
                _ct = str(_b.content_type).split(".")[-1]
                if _ct in ("text","html","image_url") and _b.value:
                    _nh, _ok = _patch_content_block(
                        _html, _b.block_key, _b.value, _ct, _b.alt_text or "", _b.page_slug
                    )
                    if _ok:
                        _html = _nh
                        _changed.append(_b.block_key)
            if _changed:
                _upload_html(_s3c, _key, _html)

        return {"status": "ok", "published": True, "patched_blocks": _changed}
    except Exception as e:
        return {"status": "ok_no_publish", "published": False, "error": str(e)[:300]}


class SaveImgFitRequest(BaseModel):
    tenant_id: str = "nexabuilder"
    page_slug: str
    block_key: str
    img_style:  str  # e.g. "object-fit:cover;object-position:50% 20%"

@router.post("/save-img-fit")
async def save_img_fit(payload: SaveImgFitRequest, _: bool = Depends(require_admin)):
    """
    Update the style= attribute on the <img> tag inside a gallery card's gc-img div.
    Allows setting object-fit and object-position without a re-upload.
    """
    try:
        from app.routers.api.seo_pipeline import _get_s3, _fetch_html, slug_to_s3_key, _upload_html
        import re as _re

        _s3  = _get_s3()
        _key = slug_to_s3_key(payload.page_slug)
        _html = _fetch_html(_s3, _key)

        # Find the gallery card
        _ki = _html.find(f'data-cms-key="{payload.block_key}"')
        if _ki < 0:
            return {"status": "error", "error": f"block_key {payload.block_key} not found in HTML"}

        # Find gc-img then <img inside it
        _gci = _html.find('<div class="gc-img"', _ki)
        _img_start = _html.find('<img ', _gci) if _gci >= 0 else -1
        _gci_close = _html.find('</div>', _gci) if _gci >= 0 else -1

        if _img_start < 0 or (_gci_close >= 0 and _img_start > _gci_close):
            return {"status": "error", "error": "No <img> tag found in this gallery card"}

        _img_end = _html.find('>', _img_start)
        _img_tag = _html[_img_start:_img_end+1]

        # Build the style value — merge object-fit and object-position into existing style
        _base_style = "width:100%;height:100%;"
        _new_style = f"{_base_style}{payload.img_style}"

        # Replace or inject style= attribute on the img tag
        if 'style="' in _img_tag:
            _img_tag_new = _re.sub(r'style="[^"]*"', f'style="{_new_style}"', _img_tag, count=1)
        else:
            _img_tag_new = _img_tag[:-1] + f' style="{_new_style}">' 

        _new_html = _html[:_img_start] + _img_tag_new + _html[_img_end+1:]
        _upload_html(_s3, _key, _new_html)

        return {"status": "ok", "published": True, "applied_style": _new_style}
    except Exception as e:
        return {"status": "error", "published": False, "error": str(e)[:300]}



import urllib.parse as _up_img

class _ImgReq(_BaseImgModel):
    query: str
    max_results: int = 6

@router.post("/image-search")
async def image_search(payload: _ImgReq, _: None = Depends(require_admin)):
    q = payload.query.strip()
    suffixes = ["", " home improvement", " construction"]
    results = []
    for i in range(min(payload.max_results, 9)):
        seed = abs(hash(q + str(i))) % 9999
        enc = _up_img.quote(q + suffixes[i % len(suffixes)])
        results.append({
            "url": f"https://source.unsplash.com/800x500/?{enc}&sig={seed}",
            "alt": q, "title": q, "source": "Unsplash", "width": 800, "height": 500
        })
        if len(results) >= payload.max_results:
            break
    return {"results": results, "query": q, "total": len(results)}
