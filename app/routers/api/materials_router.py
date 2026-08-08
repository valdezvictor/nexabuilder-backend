"""
materials_router.py — Materials Catalog API for NexaBuilder
Endpoints:
  GET  /api/materials/catalog            — list all products
  GET  /api/materials/catalog/{category} — list by category  
  GET  /api/materials/product/{slug}     — single product detail
  PUT  /api/materials/product/{id}       — update product (admin)
  POST /api/materials/image-refresh      — log image update (admin)
  GET  /api/materials/image-refresh/due  — products due for refresh
"""
import os, logging
from datetime import datetime, date
from typing import Optional
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text as sqlt

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/materials", tags=["Materials Catalog"])

ADMIN_KEY = os.getenv("CMS_ADMIN_KEY", "")


def _require_admin(key: str):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg", "postgresql+psycopg2"),
        pool_pre_ping=True
    )
    return sessionmaker(bind=engine)()


# ── Public endpoints ──────────────────────────────────────────────────────────

@router.get("/catalog")
async def list_catalog(category: Optional[str] = None, featured_only: bool = False):
    db = _db()
    try:
        where = "WHERE availability != 'discontinued'"
        params = {}
        if category:
            where += " AND category = :cat"
            params["cat"] = category
        if featured_only:
            where += " AND is_featured = TRUE"
        rows = db.execute(sqlt(f"""
            SELECT id, category, slug, display_name, stone_types,
                   available_finishes, dim_length_in, dim_width_in, dim_height_in,
                   dim_notes, lead_time_weeks, availability, unit, moq,
                   hero_image_url, seo_description, is_featured,
                   price_visible, image_disclaimer, image_updated_at
            FROM materials_catalog
            {where}
            ORDER BY is_featured DESC, category, display_name
        """), params).fetchall()
        return {"products": [dict(r._mapping) for r in rows], "total": len(rows)}
    finally:
        db.close()


@router.get("/catalog/{category}")
async def list_by_category(category: str):
    return await list_catalog(category=category)


@router.get("/product/{slug}")
async def get_product(slug: str):
    db = _db()
    try:
        row = db.execute(sqlt("""
            SELECT * FROM materials_catalog WHERE slug = :slug
        """), {"slug": slug}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Product '{slug}' not found")
        return dict(row._mapping)
    finally:
        db.close()


# ── Admin endpoints ───────────────────────────────────────────────────────────

class ProductUpdate(BaseModel):
    display_name:       Optional[str]   = None
    stone_types:        Optional[list]  = None
    available_finishes: Optional[list]  = None
    dim_length_in:      Optional[float] = None
    dim_width_in:       Optional[float] = None
    dim_height_in:      Optional[float] = None
    dim_notes:          Optional[str]   = None
    lead_time_weeks:    Optional[str]   = None
    availability:       Optional[str]   = None
    hero_image_url:     Optional[str]   = None
    gallery_images:     Optional[list]  = None
    price_usd:          Optional[float] = None
    price_visible:      Optional[bool]  = None
    moq:                Optional[int]   = None
    is_featured:        Optional[bool]  = None
    seo_description:    Optional[str]   = None
    image_updated_at:   Optional[str]   = None


@router.put("/product/{product_id}")
async def update_product(product_id: int, payload: ProductUpdate,
                          x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    db = _db()
    try:
        updates = {k: v for k, v in payload.dict().items() if v is not None}
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")
        set_clause = ", ".join(f"{k}=:{k}" for k in updates)
        updates["id"] = product_id
        updates["updated_at"] = datetime.utcnow()
        db.execute(sqlt(
            f"UPDATE materials_catalog SET {set_clause}, updated_at=:updated_at WHERE id=:id"
        ), updates)
        db.commit()
        return {"status": "updated", "id": product_id}
    finally:
        db.close()


class ImageRefreshLog(BaseModel):
    product_id: int
    new_image:  str
    notes:      Optional[str] = None
    changed_by: Optional[str] = "admin"


@router.post("/image-refresh")
async def log_image_refresh(payload: ImageRefreshLog,
                             x_admin_key: str = Header(...)):
    """Log a monthly image update for a material product."""
    _require_admin(x_admin_key)
    db = _db()
    try:
        # Get current image
        row = db.execute(sqlt(
            "SELECT hero_image_url, display_name FROM materials_catalog WHERE id=:id"
        ), {"id": payload.product_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Product not found")

        old_image = row[0]
        today = date.today().isoformat()

        # Log the change
        db.execute(sqlt("""
            INSERT INTO materials_image_log (material_id, old_image, new_image, changed_by, notes)
            VALUES (:mid, :old, :new, :by, :notes)
        """), {
            "mid": payload.product_id, "old": old_image,
            "new": payload.new_image, "by": payload.changed_by,
            "notes": payload.notes
        })

        # Update the product
        db.execute(sqlt("""
            UPDATE materials_catalog
            SET hero_image_url=:img, image_updated_at=:today, updated_at=NOW()
            WHERE id=:id
        """), {"img": payload.new_image, "today": today, "id": payload.product_id})

        db.commit()
        return {
            "status": "image_updated",
            "product": row[1],
            "old_image": old_image,
            "new_image": payload.new_image,
            "updated_at": today
        }
    finally:
        db.close()


@router.get("/image-refresh/due")
async def get_refresh_due(x_admin_key: str = Header(...)):
    """Return products whose images haven't been refreshed in 30+ days."""
    _require_admin(x_admin_key)
    db = _db()
    try:
        rows = db.execute(sqlt("""
            SELECT id, category, slug, display_name, hero_image_url,
                   image_updated_at,
                   CASE
                     WHEN image_updated_at IS NULL THEN 999
                     ELSE DATE_PART('day', NOW() - image_updated_at::timestamptz)
                   END as days_since_refresh
            FROM materials_catalog
            WHERE availability != 'discontinued'
            ORDER BY days_since_refresh DESC
            LIMIT 50
        """)).fetchall()
        due = [dict(r._mapping) for r in rows if (r[-1] or 999) >= 30]
        fresh = [dict(r._mapping) for r in rows if (r[-1] or 999) < 30]
        return {
            "needs_refresh": due,
            "recently_updated": fresh,
            "total_due": len(due),
            "total_fresh": len(fresh)
        }
    finally:
        db.close()


@router.put("/product/{product_id}/availability")
async def set_availability(product_id: int, status: str,
                            x_admin_key: str = Header(...)):
    """Quick availability toggle: available | limited | discontinued | on_request"""
    _require_admin(x_admin_key)
    valid = {'available', 'limited', 'discontinued', 'on_request'}
    if status not in valid:
        raise HTTPException(status_code=400, detail=f"Status must be one of: {valid}")
    db = _db()
    try:
        db.execute(sqlt(
            "UPDATE materials_catalog SET availability=:s, updated_at=NOW() WHERE id=:id"
        ), {"s": status, "id": product_id})
        db.commit()
        return {"status": "updated", "id": product_id, "availability": status}
    finally:
        db.close()


import boto3 as _b3
import uuid as _uuid
import mimetypes as _mt
from fastapi import UploadFile, File as _File

@router.post("/image-upload/{product_id}")
async def upload_product_image(
    product_id: int,
    file: UploadFile = _File(...),
    x_admin_key: str = Header(...)
):
    """Upload a new product image to S3, log the change, return the public URL."""
    _require_admin(x_admin_key)
    import os as _os
    BUCKET = _os.getenv("MEDIA_BUCKET","nexabuilder-root-site-979841141166-us-west-1-an")
    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT slug, category, hero_image_url, display_name FROM materials_catalog WHERE id=:id"
        ), {"id": product_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Product not found")

        slug, category, old_url, name = row

        # Build S3 key
        ext = _mt.guess_extension(file.content_type or "image/jpeg") or ".jpg"
        ext = ext.replace(".jpe",".jpg")
        s3_key = f"media/nexabuilder/materials/{category}/{slug}{ext}"
        public_url = f"https://www.nexabuilder.com/{s3_key}"

        # Upload to S3
        data = await file.read()
        s3 = _b3.client("s3", region_name="us-west-1")
        s3.put_object(
            Bucket=BUCKET, Key=s3_key, Body=data,
            ContentType=file.content_type or "image/jpeg",
            CacheControl="public,max-age=31536000"
        )

        # Log the image change
        today = __import__("datetime").date.today().isoformat()
        db.execute(sqlt("""
            INSERT INTO materials_image_log (material_id, old_image, new_image, changed_by, notes)
            VALUES (:mid, :old, :new, 'admin-upload', :notes)
        """), {"mid": product_id, "old": old_url, "new": public_url,
               "notes": f"Uploaded via admin CMS on {today}"})

        # Update catalog
        db.execute(sqlt("""
            UPDATE materials_catalog
            SET hero_image_url=:url, image_updated_at=:today, updated_at=NOW()
            WHERE id=:id
        """), {"url": public_url, "today": today, "id": product_id})

        db.commit()

        return {
            "status": "uploaded",
            "public_url": public_url,
            "s3_key": s3_key,
            "product_id": product_id,
            "slug": slug,
            "category": category,
            "display_name": name
        }
    finally:
        db.close()


class _SocialQueueReq(BaseModel):
    platform:           str
    image_url:          str
    caption:            str
    hashtags:           str = ""
    title:              str = ""
    board_id:           str = ""
    link_url:           str = ""

@router.post("/social-queue/{product_id}")
async def queue_social_post(
    product_id: int,
    payload: _SocialQueueReq,
    x_admin_key: str = Header(...)
):
    """Queue a social post for a material product."""
    _require_admin(x_admin_key)
    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT slug, category FROM materials_catalog WHERE id=:id"
        ), {"id": product_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Product not found")
        slug, category = row

        result = db.execute(sqlt("""
            INSERT INTO social_publish_queue
              (tenant_id, platform, material_slug, material_category,
               image_url, caption, hashtags, title, board_id, link_url, status)
            VALUES ('nexabuilder', :platform, :slug, :cat,
                    :img, :caption, :hashtags, :title, :board, :link, 'queued')
            RETURNING id
        """), {
            "platform": payload.platform, "slug": slug, "cat": category,
            "img": payload.image_url, "caption": payload.caption,
            "hashtags": payload.hashtags, "title": payload.title,
            "board": payload.board_id, "link": payload.link_url
        })
        queue_id = result.fetchone()[0]
        db.commit()
        return {"status": "queued", "queue_id": queue_id, "platform": payload.platform}
    finally:
        db.close()


@router.get("/social-queue")
async def get_social_queue(
    status: str = "queued",
    limit: int = 50,
    x_admin_key: str = Header(...)
):
    """Get social publish queue — shows pending, published, and failed posts."""
    _require_admin(x_admin_key)
    db = _db()
    try:
        rows = db.execute(sqlt("""
            SELECT id, platform, material_slug, material_category,
                   image_url, caption, title, status, error_message,
                   platform_post_id, queued_at, published_at, retry_count
            FROM social_publish_queue
            WHERE tenant_id='nexabuilder'
              AND (:status = 'all' OR status = :status)
            ORDER BY queued_at DESC
            LIMIT :limit
        """), {"status": status, "limit": limit}).fetchall()
        return {"queue": [dict(r._mapping) for r in rows]}
    finally:
        db.close()


@router.post("/social-publish/{queue_id}")
async def publish_social_post(
    queue_id: int,
    bg: __import__("fastapi").BackgroundTasks,
    x_admin_key: str = Header(...)
):
    """Trigger publish for a queued social post."""
    _require_admin(x_admin_key)
    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT platform, status FROM social_publish_queue WHERE id=:id"
        ), {"id": queue_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Queue item not found")
        if row[1] == "published":
            return {"status": "already_published"}

        db.execute(sqlt(
            "UPDATE social_publish_queue SET status='publishing' WHERE id=:id"
        ), {"id": queue_id})
        db.commit()
        bg.add_task(_publish_social, queue_id)
        return {"status": "publishing", "queue_id": queue_id, "platform": row[0]}
    finally:
        db.close()


async def _publish_social(queue_id: int):
    """Background task — routes to correct platform publisher."""
    import os as _os, json as _json
    db = _db()
    try:
        row = db.execute(sqlt("""
            SELECT platform, image_url, caption, hashtags, title, board_id, link_url
            FROM social_publish_queue WHERE id=:id
        """), {"id": queue_id}).fetchone()
        if not row:
            return
        platform, img_url, caption, hashtags, title, board_id, link_url = row

        result_id = None
        error = None

        if platform == "pinterest":
            result_id, error = await _publish_pinterest(img_url, title, caption, board_id, link_url)
        elif platform in ("instagram","facebook","tiktok"):
            # Platforms not yet connected — log as skipped with instructions
            error = f"{platform.title()} API not yet connected. Queue saved for when API is activated."
        else:
            error = f"Unknown platform: {platform}"

        if result_id:
            db.execute(sqlt("""
                UPDATE social_publish_queue
                SET status='published', platform_post_id=:pid, published_at=NOW()
                WHERE id=:id
            """), {"pid": result_id, "id": queue_id})
        else:
            db.execute(sqlt("""
                UPDATE social_publish_queue
                SET status='failed', error_message=:err,
                    retry_count=retry_count+1
                WHERE id=:id
            """), {"err": error or "Unknown error", "id": queue_id})
        db.commit()
    except Exception as e:
        log.error(f"Social publish error for queue {queue_id}: {e}")
        try:
            db.execute(sqlt(
                "UPDATE social_publish_queue SET status='failed', error_message=:e WHERE id=:id"
            ), {"e": str(e)[:500], "id": queue_id})
            db.commit()
        except: pass
    finally:
        db.close()


async def _publish_pinterest(img_url, title, caption, board_id, link_url):
    """Publish to Pinterest if token + board are available."""
    import httpx as _hx, os as _os
    db = _db()
    try:
        token_row = db.execute(sqlt(
            "SELECT config_value FROM app_configs WHERE config_key='pinterest_access_token'"
        )).fetchone()
        if not token_row:
            return None, "Pinterest access token not configured"
        token = token_row[0]

        active_board = board_id
        if not active_board:
            board_row = db.execute(sqlt(
                "SELECT config_value FROM app_configs WHERE config_key='pinterest_default_board'"
            )).fetchone()
            active_board = board_row[0] if board_row else None
        if not active_board:
            return None, "No Pinterest board ID provided"

        async with _hx.AsyncClient(timeout=30) as c:
            r = await c.post("https://api.pinterest.com/v5/pins", headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }, json={
                "title": title[:100] if title else "",
                "description": f"{caption}\n\n{hashtags}" if hashtags else caption,
                "media_source": {"source_type": "image_url", "url": img_url},
                "board_id": active_board,
                "link": link_url or "https://www.nexabuilder.com/materials/"
            })
        if r.status_code in (200, 201):
            return r.json().get("id"), None
        else:
            return None, f"Pinterest API {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return None, str(e)
    finally:
        db.close()


# ── Create new product ────────────────────────────────────────────────────────

class ProductCreate(BaseModel):
    display_name:    str
    category:        str
    slug:            Optional[str] = None
    origin_region:   Optional[str] = None
    lead_time_weeks: Optional[str] = None
    unit:            Optional[str] = None
    seo_description: Optional[str] = None


@router.post("/product")
async def create_product(payload: ProductCreate, x_admin_key: str = Header(...)):
    """Create a new product in materials_catalog."""
    _require_admin(x_admin_key)
    import re as _re
    db = _db()
    try:
        slug = payload.slug or _re.sub(r"[^a-z0-9]+", "-", payload.display_name.lower()).strip("-")
        existing = db.execute(sqlt(
            "SELECT id FROM materials_catalog WHERE slug=:s OR LOWER(display_name)=LOWER(:n)"
        ), {"s": slug, "n": payload.display_name}).fetchone()
        if existing:
            raise HTTPException(409, f"Name or slug already taken (id={existing[0]})")
        row = db.execute(sqlt("""
            INSERT INTO materials_catalog
                (display_name, category, slug, origin_region, lead_time_weeks, unit, seo_description,
                 availability, is_featured, price_visible, created_at, updated_at)
            VALUES (:name, :cat, :slug, :origin, :lt, :unit, :seo,
                    'available', FALSE, FALSE, NOW(), NOW())
            RETURNING id, slug
        """), {
            "name": payload.display_name, "cat": payload.category, "slug": slug,
            "origin": payload.origin_region, "lt": payload.lead_time_weeks,
            "unit": payload.unit, "seo": payload.seo_description,
        }).fetchone()
        db.commit()
        return {"id": row[0], "slug": row[1], "display_name": payload.display_name, "category": payload.category}
    finally:
        db.close()


@router.get("/check-name")
async def check_name_unique(name: str, slug: Optional[str] = None, x_admin_key: str = Header(...)):
    """Check if a product name or slug is already taken."""
    _require_admin(x_admin_key)
    import re as _re
    db = _db()
    try:
        auto_slug = slug or _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        rows = db.execute(sqlt(
            "SELECT id, display_name, slug FROM materials_catalog WHERE slug=:s OR LOWER(display_name)=LOWER(:n)"
        ), {"s": auto_slug, "n": name}).fetchall()
        conflicts = [dict(r._mapping) for r in rows]
        return {"unique": len(conflicts) == 0, "auto_slug": auto_slug, "conflicts": conflicts}
    finally:
        db.close()


@router.post("/product/{product_id}/generate-seo")
async def generate_seo_description(product_id: int, x_admin_key: str = Header(...)):
    """Claude-generated unique SEO description for a product."""
    _require_admin(x_admin_key)
    import anthropic as _ant
    db = _db()
    try:
        row = db.execute(sqlt("""
            SELECT display_name, category, slug, stone_types, available_finishes,
                   origin_region, lead_time_weeks, dim_length_in, dim_width_in, seo_description
            FROM materials_catalog WHERE id=:id
        """), {"id": product_id}).fetchone()
        if not row:
            raise HTTPException(404, "Product not found")
        prod = dict(row._mapping)
        existing = db.execute(sqlt(
            "SELECT display_name, seo_description FROM materials_catalog WHERE id!=:id AND seo_description IS NOT NULL"
        ), {"id": product_id}).fetchall()
        existing_descs = chr(10).join([f"- {r[0]}: {(r[1] or '')[:80]}" for r in existing if r[1]])
        facts = []
        if prod.get("stone_types"): facts.append("Stone: " + ", ".join(prod["stone_types"]))
        if prod.get("available_finishes"): facts.append("Finishes: " + ", ".join(prod["available_finishes"]))
        if prod.get("origin_region"): facts.append("Origin: " + prod["origin_region"])
        if prod.get("lead_time_weeks"): facts.append("Lead time: " + prod["lead_time_weeks"])
        prompt = (
            "Write a unique 2-3 sentence SEO description (max 280 chars) for this materials catalog product.\n"
            f"Product: {prod['display_name']}\nCategory: {prod['category']}\n"
            + (("Facts: " + ", ".join(facts) + "\n") if facts else "")
            + (f"Current: {prod['seo_description']}\n" if prod.get("seo_description") else "")
            + f"Existing descriptions (do NOT duplicate):\n{existing_descs}\n\n"
            "Rules: specific use case, no fluff words, clearly different from existing. Return ONLY the description text."
        )
        client = _ant.Anthropic()
        msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=300,
            messages=[{"role": "user", "content": prompt}])
        desc = msg.content[0].text.strip().strip('"')
        return {"product_id": product_id, "generated_description": desc, "char_count": len(desc)}
    finally:
        db.close()


import html as _esc

def _build_product_page(p, NAV, DRAWER, FOOTER, CSS):
    slug    = p["slug"]
    name    = p["display_name"]
    cat     = p["category"]
    hero    = p.get("hero_image_url") or ""
    gallery = list(p.get("gallery_images") or [])
    seo     = p.get("seo_description") or name + " sourced from Mexico for Southern California projects."
    origin  = p.get("origin_region") or "Mexico"
    lead    = p.get("lead_time_weeks") or "4-8 weeks"
    stones  = ", ".join(p.get("stone_types") or []) or "Natural stone"
    fins    = ", ".join(p.get("available_finishes") or []) or "Natural, Honed"

    avail_labels = {"available":"In Stock","low_stock":"Limited Stock","on_order":"On Order","discontinued":"Discontinued"}
    avail_colors = {"available":"#16a34a","low_stock":"#d97706","on_order":"#0891b2","discontinued":"#dc2626"}
    avail_label  = avail_labels.get(p.get("availability","available"), "Available")
    avail_color  = avail_colors.get(p.get("availability","available"), "#16a34a")

    hero_html = ('<img src="' + _esc.escape(hero) + '" alt="' + _esc.escape(name) + '" class="hero-img" loading="eager">'
                 if hero else '<div class="hero-img-placeholder"></div>')

    gc_items = ""
    for i, url in enumerate(gallery[:12]):
        url = str(url)
        gc_items += ('<div class="gc"><div class="gc-img" onclick="openLB(this.querySelector(\'img\').src)">'
                     '<img src="' + _esc.escape(url) + '" alt="' + _esc.escape(name) + ' photo ' + str(i+1) + '" loading="lazy"></div>'
                     '<div class="gc-info"><p class="gc-name">' + _esc.escape(name) + ' — Photo ' + str(i+1) + '</p></div></div>\n')
    if not gc_items:
        gc_items = ('<div class="gc-empty"><p>Photos coming soon — '
                    '<a href="/get-free-quote/?product=' + slug + '" style="color:var(--blue)">request samples</a></p></div>')

    schema_avail = "InStock" if p.get("availability") == "available" else "LimitedAvailability"
    img_schema = '"image":"' + _esc.escape(hero) + '",' if hero else ""

    extra_css = (
        ".product-hero{display:grid;grid-template-columns:1fr 1fr;gap:48px;max-width:var(--max-w);margin:0 auto;padding:48px 48px 32px}\n"
        "@media(max-width:768px){.product-hero{grid-template-columns:1fr;padding:24px}}\n"
        ".hero-img{width:100%;border-radius:16px;aspect-ratio:4/3;object-fit:cover;box-shadow:0 8px 32px rgba(0,0,0,.12)}\n"
        ".hero-img-placeholder{width:100%;border-radius:16px;aspect-ratio:4/3;background:#f3f4f6}\n"
        ".product-info h1{font-family:\"Fraunces\",serif;font-size:clamp(26px,4vw,40px);font-weight:900;color:var(--navy);line-height:1.15;margin-bottom:14px}\n"
        ".product-info .lead{font-size:16px;line-height:1.75;color:var(--muted);margin-bottom:24px}\n"
        ".spec-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:24px}\n"
        ".spec-item{background:#f8f9fa;border-radius:10px;padding:12px 14px}\n"
        ".spec-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:3px}\n"
        ".spec-value{font-size:14px;font-weight:600;color:var(--navy)}\n"
        ".avail-badge{display:inline-block;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700;margin-bottom:16px}\n"
        ".cta-block{display:flex;gap:10px;flex-wrap:wrap}\n"
        ".btn-primary{background:var(--blue);color:#fff;padding:13px 26px;border-radius:10px;font-size:15px;font-weight:600;text-decoration:none;display:inline-block;transition:filter .2s}.btn-primary:hover{filter:brightness(1.12)}\n"
        ".btn-secondary{background:transparent;color:var(--navy);padding:12px 22px;border-radius:10px;font-size:15px;font-weight:600;text-decoration:none;border:2px solid var(--navy);display:inline-block;transition:all .2s}.btn-secondary:hover{background:var(--navy);color:#fff}\n"
        ".gallery-section{padding:32px 48px 64px;max-width:var(--max-w);margin:0 auto}\n"
        ".gallery-section h2{font-family:\"Fraunces\",serif;font-size:26px;font-weight:700;color:var(--navy);margin-bottom:8px}\n"
        ".gg{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;margin-top:20px}\n"
        ".gc{border-radius:12px;overflow:hidden;background:#fff;border:1px solid #e5e7eb;transition:transform .2s,box-shadow .2s}.gc:hover{transform:translateY(-2px);box-shadow:0 10px 28px rgba(0,0,0,.1)}\n"
        ".gc-img{position:relative;cursor:pointer;aspect-ratio:4/3;overflow:hidden}.gc-img img{width:100%;height:100%;object-fit:cover;transition:transform .3s}.gc-img:hover img{transform:scale(1.04)}\n"
        ".gc-info{padding:12px 14px}.gc-name{font-size:13px;font-weight:600;color:#1a2332;margin:0}\n"
        ".gc-empty{grid-column:1/-1;padding:40px;text-align:center;background:#f8f9fa;border-radius:12px;border:2px dashed #e5e7eb;color:var(--muted)}\n"
        ".lb{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:9999;align-items:center;justify-content:center;padding:20px}.lb.open{display:flex}\n"
        ".lb img{max-width:90vw;max-height:90vh;border-radius:8px;object-fit:contain}\n"
        ".lb-close{position:absolute;top:16px;right:20px;background:none;border:none;color:#fff;font-size:30px;cursor:pointer;line-height:1;padding:8px}\n"
    )

    parts = [
        '<!DOCTYPE html><html lang="en"><head>',
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1.0,viewport-fit=cover">',
        '<title>' + _esc.escape(name) + ' from Mexico | NexaBuilder</title>',
        '<meta name="description" content="' + _esc.escape(seo[:155]) + '">',
        '<meta name="theme-color" content="#0a1628">',
        '<link rel="canonical" href="https://www.nexabuilder.com/materials/' + cat + '/' + slug + '/">',
        '<meta name="robots" content="index, follow">',
        ('<meta property="og:image" content="' + _esc.escape(hero) + '">' if hero else ''),
        '<link rel="preconnect" href="https://fonts.googleapis.com">',
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
        '<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,wght@0,700;0,900;1,700&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">',
        '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"' + _esc.escape(name) + '","description":"' + _esc.escape(seo) + '","material":"' + _esc.escape(stones) + '","countryOfOrigin":"MX",' + img_schema + '"brand":{"@type":"Brand","name":"NexaBuilder"},"offers":{"@type":"Offer","availability":"https://schema.org/' + schema_avail + '","seller":{"@type":"Organization","name":"NexaBuilder"}}}</script>',
        '<style>' + CSS + extra_css + '</style>',
        '</head><body>',
        '<a href="#main" class="skip-link">Skip to content</a>',
        NAV, DRAWER,
        '<main id="main">',
        '<nav class="breadcrumb" aria-label="Breadcrumb"><a href="/materials/">Materials</a> &rsaquo; <a href="/materials/' + cat + '/">' + cat.title() + '</a> &rsaquo; ' + _esc.escape(name) + '</nav>',
        '<div class="product-hero"><div>' + hero_html + '</div>',
        '<div class="product-info">',
        '<div class="avail-badge" style="background:' + avail_color + '22;color:' + avail_color + '">' + avail_label + '</div>',
        '<h1>' + _esc.escape(name) + '</h1>',
        '<p class="lead">' + _esc.escape(seo) + '</p>',
        '<div class="spec-grid">',
        '<div class="spec-item"><div class="spec-label">Origin</div><div class="spec-value">' + _esc.escape(origin) + '</div></div>',
        '<div class="spec-item"><div class="spec-label">Lead Time</div><div class="spec-value">' + _esc.escape(lead) + '</div></div>',
        '<div class="spec-item"><div class="spec-label">Material</div><div class="spec-value">' + _esc.escape(stones) + '</div></div>',
        '<div class="spec-item"><div class="spec-label">Finishes</div><div class="spec-value">' + _esc.escape(fins) + '</div></div>',
        '</div>',
        '<div class="cta-block"><a href="/get-free-quote/?product=' + slug + '" class="btn-primary">Get Free Quote</a> <a href="/materials/' + cat + '/" class="btn-secondary">View All ' + cat.title() + '</a></div>',
        '</div></div>',
        '<section class="gallery-section"><h2>Photo Gallery</h2><p style="color:var(--muted);font-size:13px;margin-top:4px">Click any photo to enlarge</p><div class="gg">' + gc_items + '</div></section>',
        '</main>', FOOTER,
        '<div class="lb" id="lb" onclick="closeLB()"><button class="lb-close" onclick="closeLB()" aria-label="Close">&times;</button><img id="lb-img" src="" alt=""></div>',
        "<script>function openLB(src){document.getElementById('lb-img').src=src;document.getElementById('lb').classList.add('open');document.body.style.overflow='hidden';}function closeLB(){document.getElementById('lb').classList.remove('open');document.getElementById('lb-img').src='';document.body.style.overflow='';}document.addEventListener('keydown',function(e){if(e.key==='Escape')closeLB();});</script>",
        '</body></html>',
    ]
    return "".join(parts)



@router.post("/product/{product_id}/publish-to-site")
async def publish_product_page(product_id: int, x_admin_key: str = Header(...)):
    """Build and publish a clean product page to S3."""
    _require_admin(x_admin_key)
    import boto3 as _b3, re as _re, os as _os, anthropic as _ant
    BUCKET = _os.getenv("MEDIA_BUCKET", "nexabuilder-root-site-979841141166-us-west-1-an")
    s3 = _b3.client("s3", region_name="us-west-1")
    db = _db()
    try:
        row = db.execute(sqlt("""
            SELECT id,slug,display_name,category,hero_image_url,gallery_images,
                   seo_description,stone_types,available_finishes,origin_region,
                   lead_time_weeks,availability
            FROM materials_catalog WHERE id=:id
        """), {"id": product_id}).fetchone()
        if not row: raise HTTPException(404, "Product not found")
        p = dict(row._mapping)
        if not p.get("seo_description"):
            try:
                client = _ant.Anthropic()
                msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=150,
                    messages=[{"role":"user","content":
                        "Write a 2-sentence SEO description (max 155 chars) for: " + p["display_name"] + ". "
                        "Category: " + p["category"] + ". Origin: " + (p.get("origin_region") or "Mexico") + ". "
                        "Site: NexaBuilder.com SoCal home improvement. No fluff. Return only the description."}])
                p["seo_description"] = msg.content[0].text.strip()[:155]
                db.execute(sqlt("UPDATE materials_catalog SET seo_description=:d WHERE id=:id"),
                           {"d": p["seo_description"], "id": product_id})
                db.commit()
            except Exception as ex:
                p["seo_description"] = p["display_name"] + " sourced from " + (p.get("origin_region") or "Mexico") + " for Southern California projects."
        NAV = DRAWER = FOOTER = CSS = ""
        try:
            idx = s3.get_object(Bucket=BUCKET, Key="materials/" + p["category"] + "/index.html")["Body"].read().decode("utf-8")
            for pat, key in [(r'<nav class="site-nav".*?</nav>',"N"),(r'<div id="nav-drawer".*?</div>\s*</div>',"D"),(r'<footer.*?</footer>',"F"),(r'<style>(.*?)</style>',"C")]:
                m = _re.search(pat, idx, _re.DOTALL)
                if m:
                    if key=="N": NAV=m.group(0)
                    elif key=="D": DRAWER=m.group(0)
                    elif key=="F": FOOTER=m.group(0)
                    elif key=="C": CSS=m.group(1)
        except: pass
        page = _build_product_page(p, NAV, DRAWER, FOOTER, CSS)
        s3_key = "materials/" + p["category"] + "/" + p["slug"] + "/index.html"
        s3.put_object(Bucket=BUCKET, Key=s3_key, Body=page.encode("utf-8"),
                      ContentType="text/html; charset=utf-8", CacheControl="public,max-age=300")
        return {"status":"published","url":"https://www.nexabuilder.com/materials/" + p["category"] + "/" + p["slug"] + "/","s3_key":s3_key,"product_id":product_id}
    finally:
        db.close()
