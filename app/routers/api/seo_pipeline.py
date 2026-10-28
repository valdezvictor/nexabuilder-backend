import os, re, json, time
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel
from typing import Optional, List, Dict

router = APIRouter(prefix="/api/seo", tags=["SEO Pipeline"])

ADMIN_KEY  = os.getenv("CMS_ADMIN_KEY", "")
NB_BUCKET  = "nexabuilder-root-site-979841141166-us-west-1-an"
CF_DIST    = "EDLQAZ1IS2WIG"
REGION     = "us-west-1"

# ─── Canonical block key registry ──────────────────────────────────────────────
# CONTENT keys: visible page content, live in Content tab
# SEO keys: head metadata, live in SEO/AEO/GEO tab only
# This strict separation prevents SEO keys appearing in Add Block dropdown

CONTENT_KEYS_BY_PAGE = {
    "home": ["hero_eyebrow","hero_headline","hero_body",
             "services_section_label","services_headline","services_subheadline",
             "how_section_label","how_headline",
             "social_section_label","social_headline",
             "locations_section_label","locations_headline","locations_subheadline",
             "bilingual_section_label","bilingual_headline","bilingual_body",
             "faq_section_label","faq_headline","cta_headline"],
    "_service_page":  ["hero_character","hero_eyebrow","hero_headline","hero_body"],
    "_location_page": ["hero_eyebrow","hero_headline",
                       "services_section_label","services_headline",
                       "cities_section_label","cities_headline"],
    "_location_city": ["hero_eyebrow","hero_headline",
                       "services_section_label","services_headline",
                       "why_section_label","why_headline"],
    "about":          ["hero_eyebrow","hero_headline",
                       "mission_section_label","mission_headline",
                       "values_section_label","values_headline",
                       "brand_section_label","about_mascot_hero","about_team_photo",
                       "area_section_label","area_headline"],
    "cost-guide":     ["hero_eyebrow","hero_headline",
                       "guides_section_label","guides_headline",
                       "pricing_section_label","pricing_headline"],
    "materials":      ["hero_eyebrow","hero_headline",
                       "categories_section_label","categories_headline",
                       "how_section_label","how_headline"],
    "_material_page": ["hero_eyebrow","hero_headline",
                       "items_section_label","related_section_label"],
    "_stone_gallery_page": ["filter_stones","filter_colors","filter_styles","hero_eyebrow","hero_headline","hero_body",
                       "items_section_label","gallery_headline","gallery_sub",
                       "gallery_0","gallery_1","gallery_2","gallery_3",
                       "gallery_4","gallery_5","gallery_6","gallery_7",
                       "install_section_label","install_headline","install_sub","install_body",
                       "faq_section_label","faq_headline","faq_sub",
                       "cta_headline","cta_body"],
    "materials/stone": ["hero_eyebrow","hero_headline","hero_body","items_section_label","mat_stone_0","mat_stone_title_0","mat_stone_desc_0","mat_stone_1","mat_stone_title_1","mat_stone_desc_1","mat_stone_2","mat_stone_title_2","mat_stone_desc_2","mat_stone_3","mat_stone_title_3","mat_stone_desc_3","mat_stone_4","mat_stone_title_4","mat_stone_desc_4","mat_stone_5","mat_stone_title_5","mat_stone_desc_5","mat_stone_6","mat_stone_title_6","mat_stone_desc_6","mat_stone_7","mat_stone_title_7","mat_stone_desc_7","mat_stone_8","mat_stone_title_8","mat_stone_desc_8","mat_stone_9","mat_stone_title_9","mat_stone_desc_9","mat_stone_10","mat_stone_title_10","mat_stone_desc_10","mat_stone_11","mat_stone_title_11","mat_stone_desc_11","mat_specs","related_section_label","related_cards","cta_headline","cta_body"],
    "materials/outdoor": ["hero_eyebrow","hero_headline","hero_body","items_section_label","mat_outdoor_0","mat_outdoor_title_0","mat_outdoor_desc_0","mat_outdoor_1","mat_outdoor_title_1","mat_outdoor_desc_1","mat_outdoor_2","mat_outdoor_title_2","mat_outdoor_desc_2","mat_outdoor_3","mat_outdoor_title_3","mat_outdoor_desc_3","mat_outdoor_4","mat_outdoor_title_4","mat_outdoor_desc_4","mat_outdoor_5","mat_outdoor_title_5","mat_outdoor_desc_5","mat_specs","related_section_label","related_cards","cta_headline","cta_body"],
    "materials/doors": ["hero_eyebrow","hero_headline","hero_body","items_section_label","mat_doors_0","mat_doors_title_0","mat_doors_desc_0","mat_doors_1","mat_doors_title_1","mat_doors_desc_1","mat_doors_2","mat_doors_title_2","mat_doors_desc_2","mat_doors_3","mat_doors_title_3","mat_doors_desc_3","mat_doors_4","mat_doors_title_4","mat_doors_desc_4","mat_doors_5","mat_doors_title_5","mat_doors_desc_5","mat_specs","related_section_label","related_cards","cta_headline","cta_body"],
    "materials/kitchen": ["hero_eyebrow","hero_headline","hero_body","items_section_label","mat_kitchen_0","mat_kitchen_title_0","mat_kitchen_desc_0","mat_kitchen_1","mat_kitchen_title_1","mat_kitchen_desc_1","mat_kitchen_2","mat_kitchen_title_2","mat_kitchen_desc_2","mat_kitchen_3","mat_kitchen_title_3","mat_kitchen_desc_3","mat_kitchen_4","mat_kitchen_title_4","mat_kitchen_desc_4","mat_kitchen_5","mat_kitchen_title_5","mat_kitchen_desc_5","mat_specs","related_section_label","related_cards","cta_headline","cta_body"],
    "materials/bathroom": ["hero_eyebrow","hero_headline","hero_body","items_section_label","mat_bathroom_0","mat_bathroom_title_0","mat_bathroom_desc_0","mat_bathroom_1","mat_bathroom_title_1","mat_bathroom_desc_1","mat_bathroom_2","mat_bathroom_title_2","mat_bathroom_desc_2","mat_bathroom_3","mat_bathroom_title_3","mat_bathroom_desc_3","mat_bathroom_4","mat_bathroom_title_4","mat_bathroom_desc_4","mat_bathroom_5","mat_bathroom_title_5","mat_bathroom_desc_5","mat_specs","related_section_label","related_cards","cta_headline","cta_body"],
    "_tile_gallery_page": ["hero_eyebrow","hero_headline","hero_body","items_section_label","mat_tile_0","mat_tile_title_0","mat_tile_desc_0","mat_tile_1","mat_tile_title_1","mat_tile_desc_1","mat_tile_2","mat_tile_title_2","mat_tile_desc_2","mat_tile_3","mat_tile_title_3","mat_tile_desc_3","mat_tile_4","mat_tile_title_4","mat_tile_desc_4","mat_tile_5","mat_tile_title_5","mat_tile_desc_5","mat_specs","related_section_label","related_cards","tal_section_label","tal_headline","tal_sub","tal_groups","cta_headline","cta_body"],
}

SEO_KEYS = ["seo_title","seo_description","canonical","seo_keywords",
            "aeo_faq_question","aeo_faq_answer","aeo_definition",
            "geo_entity_type","geo_entity_name","geo_authority",
            "geo_region","geo_citations","geo_freshness"]

def _resolve_template(slug: str) -> str:
    """Map a page slug to its content key template name."""
    parts = slug.strip("/").split("/")
    if slug == "home" or slug == "":
        return "home"
    if parts[0] == "services" and len(parts) > 1:
        return "_service_page"
    if parts[0] == "locations" and len(parts) == 2:
        return "_location_page"
    if parts[0] == "locations" and len(parts) >= 3:
        return "_location_city"
    if parts[0] == "materials" and len(parts) == 3 and parts[1] == "stone":
        return "_stone_gallery_page"
    if parts[0] == "materials" and len(parts) == 2 and slug in CONTENT_KEYS_BY_PAGE:
        return slug
    if parts[0] == "materials" and len(parts) >= 2 and parts[1] == "tile":
        if len(parts) == 2:
            return "_tile_gallery_page"
        return "_stone_gallery_page"  # tile/* child pages use full child page template
    if parts[0] == "materials" and len(parts) == 3:
        return "_stone_gallery_page"  # all materials/*/child pages use the child template
    if parts[0] == "materials" and len(parts) > 1:
        return "_material_page"
    if slug in CONTENT_KEYS_BY_PAGE:
        return slug
    return "_service_page"

def _get_content_keys(slug: str) -> List[str]:
    template = _resolve_template(slug)
    return CONTENT_KEYS_BY_PAGE.get(template, CONTENT_KEYS_BY_PAGE["_service_page"])

@router.get("/block-keys/{page_slug:path}")
async def get_block_keys(page_slug: str, key_type: str = "content"):
    """
    Return block keys for a given page.
    ?key_type=content  → only visible content keys (for Add Block dropdown)
    ?key_type=seo      → only SEO/AEO/GEO keys
    ?key_type=all      → all keys
    """
    slug = page_slug.strip("/") or "home"
    content_keys = _get_content_keys(slug)
    if key_type == "seo":
        return {"page_slug": page_slug, "block_keys": SEO_KEYS}
    if key_type == "all":
        return {"page_slug": page_slug, "block_keys": content_keys + SEO_KEYS}
    # Default: content only
    return {"page_slug": page_slug, "block_keys": content_keys}

async def require_admin(x_admin_key: Optional[str] = Header(default=None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid X-Admin-Key")
    return True

def slug_to_s3_key(slug: str) -> str:
    slug = slug.strip("/")
    if slug in ("home", ""):
        return "index.html"
    return f"{slug}/index.html"

def _get_s3():
    import boto3; return boto3.client("s3", region_name=REGION)

def _get_cf():
    import boto3; return boto3.client("cloudfront", region_name=REGION)

def _fetch_html(s3, s3_key: str) -> str:
    try:
        obj = s3.get_object(Bucket=NB_BUCKET, Key=s3_key)
        return obj["Body"].read().decode("utf-8")
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Page not found: s3://{NB_BUCKET}/{s3_key}")

def _extract_block_value(html: str, key: str):
    """Extract the inner content of a data-cms-key element."""
    pos = html.find(f'data-cms-key="{key}"')
    if pos < 0: return None, None
    tag_start = html.rfind("<", 0, pos)
    tm = re.match(r"<([a-zA-Z][a-zA-Z0-9]*)", html[tag_start:])
    if not tm: return None, None
    tag_name = tm.group(1)
    open_end = html.find(">", pos)
    close_pos = html.find(f"</{tag_name}>", open_end)
    if close_pos < 0: return None, None
    inner = html[open_end+1:close_pos]
    ct = "html" if re.search(r"<[a-z]", inner) else "text"
    if ct == "text":
        inner = re.sub(r'&[a-z]+;', lambda m: {
            '&mdash;':'—','&rsquo;':"'",'&ntilde;':'ñ','&ndash;':'–',
            '&amp;':'&','&middot;':'·','&nbsp;':' ','&laquo;':'«','&raquo;':'»'
        }.get(m.group(), m.group()), inner).strip()
    return ct, inner.strip()

class SeedFromHtmlRequest(BaseModel):
    tenant_id: str
    page_slug: str
    overwrite: bool = False  # if True, overwrite existing blocks

@router.post("/seed-from-html")
async def seed_from_html(payload: SeedFromHtmlRequest, _: bool = Depends(require_admin)):
    """
    Read the live HTML for a page, extract all data-cms-key elements,
    and seed the DB with their current values.
    Preserves existing blocks unless overwrite=True.
    """
    slug = payload.page_slug.strip("/") or "home"
    s3_key = slug_to_s3_key(slug)
    s3 = _get_s3()
    html = _fetch_html(s3, s3_key)

    # Find all data-cms-key in the HTML (body content)
    html_keys = list(dict.fromkeys(re.findall(r'data-cms-key="([^"]+)"', html)))

    # Also extract SEO head tags as virtual blocks
    def _get_meta(html_text, name_pattern, value_attr="content"):
        tag = re.search(r"<meta[^>]+" + name_pattern + r"[^>]+>", html_text, re.IGNORECASE)
        if not tag: return None
        m = re.search(value_attr + r'="([^"]*)"', tag.group())
        return m.group(1).strip() if m else None
    def _get_link_href(html_text, rel_val):
        tag = re.search(r'<link[^>]+rel="' + rel_val + r'"[^>]+>', html_text, re.IGNORECASE)
        if not tag: tag = re.search(r'<link[^>]+rel='' + rel_val + r''[^>]+>', html_text, re.IGNORECASE)
        if not tag: return None
        m = re.search(r'href="([^"]*)"', tag.group())
        return m.group(1).strip() if m else None
    seo_head_blocks = []
    title_m = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    if title_m: seo_head_blocks.append({"key":"seo_title","content_type":"text","value":title_m.group(1).strip()})
    desc_val = _get_meta(html, "name=[^>]*description")
    if desc_val: seo_head_blocks.append({"key":"seo_description","content_type":"text","value":desc_val})
    canon_val = _get_link_href(html, "canonical")
    if canon_val: seo_head_blocks.append({"key":"canonical","content_type":"text","value":canon_val})
    robots_val = _get_meta(html, "name=[^>]*robots")
    if robots_val: seo_head_blocks.append({"key":"seo_keywords","content_type":"text","value":robots_val})

    if not html_keys and not seo_head_blocks:
        return {"status": "no_keys", "message": f"No data-cms-key attributes or SEO head tags found in {s3_key}"}

    # Extract values for body content keys
    blocks_to_seed = []
    for key in html_keys:
        ct, value = _extract_block_value(html, key)
        if value:
            blocks_to_seed.append({"key": key, "content_type": ct, "value": value})

    # Add SEO head blocks
    blocks_to_seed.extend(seo_head_blocks)

    if not blocks_to_seed:
        return {"status": "no_values", "message": "Keys found but no extractable values"}

    # Seed into DB via direct psql
    import sys as _sys; _sys.path.insert(0, '/var/www/nexabuilder/backend/current')
    try:
        _db_url = os.getenv("DATABASE_URL","")
        from sqlalchemy import select as _select, text as _text, create_engine as _ce
        from sqlalchemy.orm import sessionmaker as _sm
        _engine = _ce(_db_url.replace("postgresql+asyncpg","postgresql+psycopg2"),echo=False)
        _conflict = "DO UPDATE SET content_type=EXCLUDED.content_type, value=EXCLUDED.value, is_published=true" if payload.overwrite else "DO NOTHING"
        with _sm(bind=_engine)() as _session:
            for b in blocks_to_seed:
                _session.execute(_text(
                    "INSERT INTO content_blocks (tenant_id,page_slug,block_key,content_type,value,is_published,version,updated_by) "
                    "VALUES (:tenant,:slug,:key,:ct,:val,true,1,'auto_seed') "
                    f"ON CONFLICT (tenant_id,page_slug,block_key) {_conflict}"
                ), {"tenant":payload.tenant_id,"slug":slug,"key":b["key"],"ct":b["content_type"],"val":b["value"]})
            _session.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB error: {str(e)[:200]}")
    return {
        "status": "seeded",
        "page": slug,
        "s3_key": s3_key,
        "blocks_found": len(html_keys),
        "blocks_seeded": len(blocks_to_seed),
        "keys": [b["key"] for b in blocks_to_seed],
        "overwrite": payload.overwrite,
    }

class SeoPublishRequest(BaseModel):
    tenant_id: str
    page_slug: str
    seo_title: Optional[str] = None
    seo_description: Optional[str] = None
    canonical: Optional[str] = None
    push_content_blocks: bool = True

def _patch_content_block(html, block_key, value, content_type, alt_text="", page_slug=""):
    if not value: return html, False
    if content_type in ("image_url", "image"):
        # Try src= attribute on same element first
        pat = r'(data-cms-key="' + re.escape(block_key) + r'"[^>]*\bsrc=")[^"]*(")'
        new_html, n = re.subn(pat, lambda m: m.group(1)+value+m.group(2), html)
        if n == 0:
            pat2 = r'(\bsrc=")[^"]*("[^>]*data-cms-key="' + re.escape(block_key) + r'")'
            new_html, n = re.subn(pat2, lambda m: m.group(1)+value+m.group(2), html)
        if n == 0:
            # Gallery card: key is on the outer .gc div; gc-img is immediately after
            key_pos = html.find(f'data-cms-key="{block_key}"')
            if key_pos >= 0:
                # gc-img opens AFTER the key (within ~200 chars)
                gc_img_start = html.find('<div class="gc-img"', key_pos)
                gc_ph_start  = html.find('<div class="gc-ph">', gc_img_start) if gc_img_start >= 0 else -1
                if gc_ph_start >= 0 and gc_ph_start < key_pos + 2000:
                    # Find the CLOSING tag of gc-ph by counting nested divs
                    depth = 0; pos = gc_ph_start; gc_ph_end = -1
                    while pos < len(html):
                        od = html.find("<div", pos)
                        cd = html.find("</div>", pos)
                        if od >= 0 and od < cd:
                            depth += 1; pos = od + 4
                        elif cd >= 0:
                            depth -= 1; pos = cd + 6
                            if depth == 0: gc_ph_end = pos; break
                        else:
                            break
                    if gc_ph_end > 0:
                        safe_alt = (alt_text or "").replace('"', "&quot;")
                        img_tag = f'<img src="{value}" alt="{safe_alt}" style="width:100%;height:100%;object-fit:cover;" loading="lazy">'
                        new_html = html[:gc_ph_start] + img_tag + html[gc_ph_end:]
                        # Also add data-img to the gc-img opening tag so modal can read it
                        gc_img_open_end = new_html.find('>', gc_img_start)
                        if gc_img_open_end > 0 and 'data-img=' not in new_html[gc_img_start:gc_img_open_end]:
                            new_html = new_html[:gc_img_open_end] + f' data-img="{value}"' + new_html[gc_img_open_end:]
                        
                        # Pull artisan metadata from construction_assets and write to data-* attrs + gc-meta
                        try:
                            import os as _os
                            from sqlalchemy import create_engine as _ce, text as _sqlt
                            from sqlalchemy.orm import sessionmaker as _sm
                            _eng = _ce(_os.getenv("DATABASE_URL","").replace("postgresql+asyncpg","postgresql+psycopg2"), echo=False)
                            with _sm(bind=_eng)() as _db:
                                _row = _db.execute(_sqlt("""
                                    SELECT stone_type, color_family, style_category, origin, artisan_name, artisan_story
                                    FROM construction_assets
                                    WHERE page_slug=:p AND block_key=:b
                                    ORDER BY updated_at DESC LIMIT 1
                                """), {"p": page_slug, "b": block_key}).fetchone()
                            if _row:
                                _stone = _row[0] or ""
                                _color = _row[1] or ""
                                _style = _row[2] or ""
                                _origin = _row[3] or ""
                                _artisan = _row[4] or ""
                                _story = _row[5] or ""
                                # Re-locate gc-img after img injection
                                _gi2 = new_html.find('data-cms-key="' + block_key + '"')
                                _gc_img2 = new_html.find('<div class="gc-img"', _gi2) if _gi2 >= 0 else -1
                                _gc_img_end2 = new_html.find('>', _gc_img2) if _gc_img2 >= 0 else -1
                                if _gc_img_end2 > 0:
                                    _existing = new_html[_gc_img2:_gc_img_end2]
                                    _attrs = ""
                                    for _attr, _val in [("data-stone",_stone),("data-color",_color),("data-style",_style),
                                                        ("data-origin",_origin),("data-artisan",_artisan),("data-story",_story)]:
                                        if _val and _attr not in _existing:
                                            _attrs += f' {_attr}="{_val.replace(chr(34), "&quot;")}"'
                                        elif _val:
                                            # Replace ONLY within gc-img opening tag
                                            import re as _re
                                            _gi2_end = new_html.find('>', _gc_img2)
                                            if _gi2_end > 0:
                                                _tag2 = new_html[_gc_img2:_gi2_end]
                                                _tag2 = _re.sub(rf'{_attr}="[^"]*"', f'{_attr}="{_val.replace(chr(34), "&quot;")}"', _tag2, count=1)
                                                new_html = new_html[:_gc_img2] + _tag2 + new_html[_gi2_end:]
                                    if _attrs:
                                        new_html = new_html[:_gc_img_end2] + _attrs + new_html[_gc_img_end2:]
                                
                                # Update gc-meta-title and gc-meta-tags
                                _gi3 = new_html.find('data-cms-key="' + block_key + '"')
                                _meta_start = new_html.find('<div class="gc-meta">', _gi3) if _gi3 >= 0 else -1
                                _meta_end = new_html.find('</div></div>', _meta_start) if _meta_start >= 0 else -1
                                if _meta_start >= 0 and _meta_end >= 0:
                                    # Build tags HTML
                                    _tags_html = ""
                                    for _tv in [t for t in [_stone, _style, _color] if t]:
                                        _tags_html += f'<span class="tag">{_tv}</span>'
                                    # Find and update gc-meta-title
                                    _mt_start = new_html.find('<div class="gc-meta-title"', _meta_start)
                                    _mt_end = new_html.find('</div>', _mt_start) if _mt_start >= 0 else -1
                                    # Find and update gc-meta-tags
                                    _tgs_start = new_html.find('<div class="gc-meta-tags">', _meta_start)
                                    _tgs_end = new_html.find('</div>', _tgs_start) if _tgs_start >= 0 else -1
                                    if _tgs_start >= 0 and _tags_html:
                                        # Find the CLOSING </div> of gc-meta-tags (after all inner <span> tags)
                                        # Find actual > of opening tag (handles corrupt HTML)
                                        _tgs_open_end = new_html.find('>', _tgs_start)
                                        if _tgs_open_end >= _tgs_start and _tgs_open_end < _tgs_start + 40:
                                            _tgs_close = new_html.find('</div>', _tgs_open_end + 1)
                                            if _tgs_close >= 0:
                                                new_html = new_html[:_tgs_open_end+1] + _tags_html + new_html[_tgs_close:]
                        except Exception:
                            pass  # artisan metadata injection is best-effort
                        n = 1
                    else:
                        # gc-ph not found — image already uploaded; update data-* attrs only
                        new_html = html
                        import re as _re0
                        _gc_img_open = new_html.find('>', gc_img_start) if gc_img_start >= 0 else -1
                        if _gc_img_open > 0:
                            _cur = new_html[gc_img_start:_gc_img_open]
                            if 'data-img=' in _cur:
                                new_html = _re0.sub(r'data-img="[^"]*"', f'data-img="{value}"', new_html, count=1)
                            else:
                                new_html = new_html[:_gc_img_open] + f' data-img="{value}"' + new_html[_gc_img_open:]
                        n = 1
                        # Pull artisan metadata for already-uploaded image
                        try:
                            import os as _os2
                            from sqlalchemy import create_engine as _ce2, text as _sqlt2
                            from sqlalchemy.orm import sessionmaker as _sm2
                            _eng2 = _ce2(_os2.getenv("DATABASE_URL","").replace("postgresql+asyncpg","postgresql+psycopg2"), echo=False)
                            with _sm2(bind=_eng2)() as _db2:
                                _row2 = _db2.execute(_sqlt2("""
                                    SELECT stone_type, color_family, style_category, origin, artisan_name, artisan_story
                                    FROM construction_assets
                                    WHERE page_slug=:p AND block_key=:b
                                    ORDER BY updated_at DESC LIMIT 1
                                """), {"p": page_slug, "b": block_key}).fetchone()
                            if _row2:
                                _stone2 = _row2[0] or ""; _color2 = _row2[1] or ""; _style2 = _row2[2] or ""
                                _origin2 = _row2[3] or ""; _artisan2 = _row2[4] or ""; _story2 = _row2[5] or ""
                                _gi_a = new_html.find('data-cms-key="' + block_key + '"')
                                _gc_a = new_html.find('<div class="gc-img"', _gi_a) if _gi_a >= 0 else -1
                                _gc_a_end = new_html.find('>', _gc_a) if _gc_a >= 0 else -1
                                if _gc_a_end > 0:
                                    _ex_a = new_html[_gc_a:_gc_a_end]
                                    import re as _re_a
                                    for _attr2, _val2 in [("data-stone",_stone2),("data-color",_color2),
                                                          ("data-style",_style2),("data-origin",_origin2),
                                                          ("data-artisan",_artisan2),("data-story",_story2)]:
                                        if _val2:
                                            _safe2 = _val2.replace(chr(34), "&quot;")
                                            if _attr2 in _ex_a:
                                                new_html = _re_a.sub(rf'{_attr2}="[^"]*"', f'{_attr2}="{_safe2}"', new_html, count=1)
                                            else:
                                                _gc_a_end2 = new_html.find('>', _gc_a)
                                                new_html = new_html[:_gc_a_end2] + f' {_attr2}="{_safe2}"' + new_html[_gc_a_end2:]
                                                _gc_a_end2 += len(f' {_attr2}="{_safe2}"')
                                _meta_a = new_html.find('<div class="gc-meta">', _gi_a) if _gi_a >= 0 else -1
                                if _meta_a >= 0:
                                    _tags_a = "".join(f'<span class="tag">{t}</span>' for t in [_stone2,_style2,_color2] if t)
                                    _tgs_a = new_html.find('<div class="gc-meta-tags">', _meta_a)
                                    if _tgs_a >= 0 and _tags_a:
                                        _tgs_open_a3 = new_html.find('>', _tgs_a)
                                        if _tgs_open_a3 >= _tgs_a and _tgs_open_a3 < _tgs_a+40:
                                            _tgs_cl = new_html.find('</div>', _tgs_open_a3+1)
                                            if _tgs_cl >= 0:
                                                new_html = new_html[:_tgs_open_a3+1] + _tags_a + new_html[_tgs_cl:]
                        except Exception:
                            pass
                elif gc_img_start >= 0:
                    # No gc-ph in range — image already uploaded; update data-* attrs and artisan data
                    import re as _re0
                    new_html = html
                    _gc_img_open = new_html.find('>', gc_img_start)
                    if _gc_img_open > 0:
                        _cur0 = new_html[gc_img_start:_gc_img_open]
                        if 'data-img=' in _cur0:
                            # Scope to gc-img opening tag only — other cards share the same doc
                            _tag0 = new_html[gc_img_start:_gc_img_open]
                            _tag0 = _re0.sub(r'data-img="[^"]*"', f'data-img="{value}"', _tag0, count=1)
                            new_html = new_html[:gc_img_start] + _tag0 + new_html[_gc_img_open:]
                        else:
                            new_html = new_html[:_gc_img_open] + f' data-img="{value}"' + new_html[_gc_img_open:]
                    # Also update <img src= inside gc-img — SCOPED to THIS card only
                    # Find the closing </div> of gc-img (first </div> after the opening >)
                    _gc_img_content_end = new_html.find('</div>', _gc_img_open)
                    _img_tag_pos = new_html.find('<img ', _gc_img_open)
                    if _img_tag_pos >= 0 and _gc_img_content_end >= 0 and _img_tag_pos < _gc_img_content_end:
                        _img_end = new_html.find('>', _img_tag_pos)
                        _img_str = new_html[_img_tag_pos:_img_end+1]
                        _img_str = _re0.sub(r'src="[^"]*"', f'src="{value}"', _img_str, count=1)
                        new_html = new_html[:_img_tag_pos] + _img_str + new_html[_img_end+1:]
                    # Artisan metadata injection
                    try:
                        import os as _os2
                        from sqlalchemy import create_engine as _ce2, text as _sqlt2
                        from sqlalchemy.orm import sessionmaker as _sm2
                        _eng2 = _ce2(_os2.getenv("DATABASE_URL","").replace("postgresql+asyncpg","postgresql+psycopg2"), echo=False)
                        with _sm2(bind=_eng2)() as _db2:
                            _row2 = _db2.execute(_sqlt2("""
                                SELECT stone_type, color_family, style_category, origin, artisan_name, artisan_story
                                FROM construction_assets
                                WHERE page_slug=:p AND block_key=:b
                                ORDER BY updated_at DESC LIMIT 1
                            """), {"p": page_slug, "b": block_key}).fetchone()
                        if _row2:
                            _stone2 = _row2[0] or ""; _color2 = _row2[1] or ""; _style2 = _row2[2] or ""
                            _origin2 = _row2[3] or ""; _artisan2 = _row2[4] or ""; _story2 = _row2[5] or ""
                            _gi_a = new_html.find('data-cms-key="' + block_key + '"')
                            _gc_a = new_html.find('<div class="gc-img"', _gi_a) if _gi_a >= 0 else -1
                            _gc_a_end = new_html.find('>', _gc_a) if _gc_a >= 0 else -1
                            if _gc_a_end > 0:
                                import re as _re_a
                                for _attr2, _val2 in [("data-stone",_stone2),("data-color",_color2),
                                                      ("data-style",_style2),("data-origin",_origin2),
                                                      ("data-artisan",_artisan2),("data-story",_story2)]:
                                    if _val2:
                                        _safe2 = _val2.replace(chr(34), "&quot;")
                                        _cur2 = new_html[_gc_a:_gc_a_end]
                                        _end_a = new_html.find('>', _gc_a)
                                        if _attr2 in _cur2:
                                            # Replace ONLY within the gc-img opening tag
                                            tag_str = new_html[_gc_a:_end_a]
                                            tag_str = _re_a.sub(rf'{_attr2}="[^"]*"', f'{_attr2}="{_safe2}"', tag_str, count=1)
                                            new_html = new_html[:_gc_a] + tag_str + new_html[_end_a:]
                                        else:
                                            new_html = new_html[:_end_a] + f' {_attr2}="{_safe2}"' + new_html[_end_a:]
                                            _gc_a_end += len(f' {_attr2}="{_safe2}"')
                            _meta_a = new_html.find('<div class="gc-meta">', _gi_a) if _gi_a >= 0 else -1
                            if _meta_a >= 0:
                                _tags_a = "".join(f'<span class="tag">{t}</span>' for t in [_stone2,_style2,_color2] if t)
                                _tgs_a = new_html.find('<div class="gc-meta-tags">', _meta_a)
                                if _tgs_a >= 0 and _tags_a:
                                    _tgs_cl = new_html.find('</div>', _tgs_a + 26)
                                    _tgs_open_a2 = new_html.find('>', _tgs_a)
                                    if _tgs_open_a2 >= _tgs_a and _tgs_open_a2 < _tgs_a+40:
                                        _tgs_cl = new_html.find('</div>', _tgs_open_a2+1)
                                        if _tgs_cl >= 0:
                                            new_html = new_html[:_tgs_open_a2+1] + _tags_a + new_html[_tgs_cl:]
                    except Exception:
                        pass
                    n = 1
                else:
                    new_html = html
        if n == 0:
            # Material grid placeholder: data-cms-key is directly on the .mat-ph div
            key_pos_mp = html.find(f'data-cms-key="{block_key}"')
            if key_pos_mp >= 0:
                tag_start_mp = html.rfind("<", 0, key_pos_mp)
                tm_mp = re.match(r"<([a-zA-Z][a-zA-Z0-9]*)", html[tag_start_mp:])
                if tm_mp and tm_mp.group(1) == "div" and 'class="mat-ph"' in html[tag_start_mp:key_pos_mp+50]:
                    open_tag_end_mp = html.find(">", key_pos_mp)
                    close_start_mp = html.find("</div>", open_tag_end_mp)
                    if open_tag_end_mp > 0 and close_start_mp > 0:
                        safe_alt_mp = (alt_text or "").replace('"', "&quot;")
                        img_tag_mp = f'<img src="{value}" alt="{safe_alt_mp}" style="width:100%;height:100%;object-fit:cover;" loading="lazy">'
                        new_html = html[:open_tag_end_mp+1] + img_tag_mp + html[close_start_mp:]
                        n = 1
        return new_html, n > 0
    key_search = f'data-cms-key="{block_key}"'
    key_pos = html.find(key_search)
    if key_pos < 0: return html, False
    tag_start = html.rfind("<", 0, key_pos)
    tm = re.match(r"<([a-zA-Z][a-zA-Z0-9]*)", html[tag_start:])
    if not tm: return html, False
    tag_name = tm.group(1)
    open_tag_end = html.find(">", key_pos)
    if open_tag_end < 0: return html, False
    close_tag = f"</{tag_name}>"
    close_start = html.find(close_tag, open_tag_end)
    if close_start < 0: return html, False
    return html[:open_tag_end+1] + value + html[close_start:], True

def _upload_html(s3, s3_key, html):
    s3.put_object(Bucket=NB_BUCKET, Key=s3_key, Body=html.encode("utf-8"),
        ContentType="text/html", CacheControl="no-cache,no-store,must-revalidate")

def _invalidate(cf, slug, s3_key):
    cf_path = "/" if s3_key == "index.html" else f"/{slug.strip('/')}/"
    try:
        cf.create_invalidation(DistributionId=CF_DIST,
            InvalidationBatch={"Paths":{"Quantity":1,"Items":[cf_path]},
                               "CallerReference":str(int(time.time()))})
    except: pass

def _patch_seo(html, title=None, description=None, canonical=None):
    changed = []
    if title:
        html = re.sub(r"<title>.*?</title>", f"<title>{title}</title>", html, flags=re.DOTALL)
        changed.append("title")
    if description:
        esc = description.replace('"','&quot;')
        new_meta = f'<meta name="description" content="{esc}">'
        if re.search(r'<meta name="description"', html):
            html = re.sub(r'<meta name="description"[^>]*>', new_meta, html)
        else:
            html = html.replace("</head>", f"{new_meta}\n</head>")
        changed.append("description")
    if canonical:
        esc = canonical.replace('"','&quot;')
        new_c = f'<link rel="canonical" href="{esc}">'
        if re.search(r'<link rel="canonical"', html):
            html = re.sub(r'<link rel="canonical"[^>]*>', new_c, html)
        else:
            html = html.replace("</head>", f"{new_c}\n</head>")
        changed.append("canonical")
    return html, changed

@router.post("/publish")
async def publish_seo(payload: SeoPublishRequest, _: bool = Depends(require_admin)):
    if payload.tenant_id != "nexabuilder":
        return {"status":"skipped","message":"unapiscina.com uses swap.js — mark published in CMS"}
    slug = payload.page_slug.strip("/") or "home"
    s3_key = slug_to_s3_key(slug)
    s3 = _get_s3(); html = _fetch_html(s3, s3_key); changed = []
    html, seo_changed = _patch_seo(html, payload.seo_title, payload.seo_description, payload.canonical)
    changed.extend(seo_changed)
    if payload.push_content_blocks:
        import sys; sys.path.insert(0,'/var/www/nexabuilder/backend/current')
        try:
            db_url = os.getenv("DATABASE_URL","")
            from sqlalchemy import select, create_engine
            from sqlalchemy.orm import sessionmaker
            from app.models.content_block import ContentBlock
            engine = create_engine(db_url.replace("postgresql+asyncpg","postgresql+psycopg2"),echo=False)
            with sessionmaker(bind=engine)() as session:
                blocks = session.execute(
                    select(ContentBlock).where(
                        ContentBlock.tenant_id==payload.tenant_id,
                        ContentBlock.page_slug==slug,
                        ContentBlock.is_published==True,
                    )
                ).scalars().all()
                for block in blocks:
                    ct = str(block.content_type).split(".")[-1]
                    if ct in ("text","html","image_url") and block.value:
                        new_html, patched = _patch_content_block(html, block.block_key, block.value, ct, block.alt_text or "", slug)
                        if patched: html = new_html; changed.append(f"block:{block.block_key}")
        except Exception as e:
            changed.append(f"content_blocks_skipped:{str(e)[:50]}")
    if not changed:
        return {"status":"noop","message":"No fields to update"}
    _upload_html(s3, s3_key, html)
    _invalidate(_get_cf(), slug, s3_key)
    return {"status":"published","page":slug,"s3_key":s3_key,"updated":changed}

@router.post("/publish-content")
async def publish_content_block(payload: dict, _: bool = Depends(require_admin)):
    if payload.get("tenant_id") != "nexabuilder":
        return {"status":"skipped"}
    slug = payload.get("page_slug","").strip("/") or "home"
    s3_key = slug_to_s3_key(slug)
    s3 = _get_s3(); html = _fetch_html(s3, s3_key)
    new_html, patched = _patch_content_block(html, payload.get("block_key",""), payload.get("value",""), payload.get("content_type","text"), payload.get("alt_text","") or "", slug)
    if not patched:
        return {"status":"noop","message":f"Block '{payload.get('block_key')}' not found in HTML"}
    _upload_html(s3, s3_key, new_html)
    _invalidate(_get_cf(), slug, s3_key)
    return {"status":"published","page":slug,"block_key":payload.get("block_key")}


# ═══════════════════════════════════════════════════════════════════════════════
# AUTO-INJECT: detect page type → inject data-cms-key → seed DB in one call
# ═══════════════════════════════════════════════════════════════════════════════

_SL_KEY_MAP = {
    "all services":      "services_section_label",
    "service coverage":  "services_section_label",
    "why nexabuilder":   "why_section_label",
    "cities covered":    "cities_section_label",
    "material categories": "categories_section_label",
    "available items":   "items_section_label",
    "related materials": "related_section_label",
    "how it works":      "how_section_label",
    "our mission":       "mission_section_label",
    "what we stand for": "values_section_label",
    "our brand":         "brand_section_label",
    "service area":      "area_section_label",
    "cost guides":       "guides_section_label",
    "about our pricing": "pricing_section_label",
}

_H2_KEY_MAP = {
    "home improvement services":   "services_headline",
    "what we cover":               "services_headline",
    "cslb-verified, matched":      "why_headline",
    "all 34 cities":               "cities_headline",
    "all 88 cities":               "cities_headline",
    "cities in":                   "cities_headline",
    "browse by material":          "categories_headline",
    "custom materials":            "how_headline",
    "making home improvement":     "mission_headline",
    "the nexabuilder difference":  "values_headline",
    "serving southern california": "area_headline",
    "what are you planning":       "guides_headline",
    "where these numbers":         "pricing_headline",
}

def auto_inject_cms_keys(html: str, slug: str):
    """Inject data-cms-key attributes into HTML. Returns (new_html, keys_added)."""
    existing = re.findall(r'data-cms-key="([^"]+)"', html)
    if existing:
        return html, existing  # already done

    injected = []
    parts = slug.strip("/").split("/")
    template = (
        "home"              if slug in ("home","") else
        "service"           if parts[0]=="services" and len(parts)>1 else
        "location_city"     if parts[0]=="locations" and len(parts)>=3 else
        "_stone_gallery_page" if parts[0]=="materials" and len(parts)==3 and parts[1]=="stone" else
        "generic"
    )

    # Eyebrow
    if '<div class="eyebrow">' in html:
        html = html.replace('<div class="eyebrow">', '<div class="eyebrow" data-cms-key="hero_eyebrow">', 1)
        injected.append("hero_eyebrow")

    # H1
    if '<h1 ' in html:
        html = html.replace('<h1 ', '<h1 data-cms-key="hero_headline" ', 1)
        injected.append("hero_headline")
    elif '<h1>' in html:
        html = html.replace('<h1>', '<h1 data-cms-key="hero_headline">', 1)
        injected.append("hero_headline")

    # Section labels (.sl)
    body_start = html.find('<main')
    body = html[body_start:] if body_start >= 0 else html
    for m in re.finditer(r'(<div[^>]*class="sl"[^>]*>)(.*?)(</div>)', body, re.DOTALL):
        opening, content, closing = m.group(1), m.group(2), m.group(3)
        if 'data-cms-key' in opening:
            continue
        label = re.sub(r'<[^>]+>', '', content).strip().lower()
        key = next((v for k,v in _SL_KEY_MAP.items() if k in label), None)
        if key:
            new_opening = opening[:-1] + f' data-cms-key="{key}">'
            old_full = opening + content + closing
            new_full = new_opening + content + closing
            if old_full in html:
                html = html.replace(old_full, new_full, 1)
                injected.append(key)

    # H2 headlines
    for m in re.finditer(r'<h2([^>]*)>(.*?)</h2>', html, re.DOTALL):
        attrs, content = m.group(1), m.group(2)
        if 'data-cms-key' in attrs:
            continue
        text = re.sub(r'<[^>]+>', '', content).strip().lower()
        key = next((v for k,v in _H2_KEY_MAP.items() if k in text), None)
        if key:
            old = f'<h2{attrs}>{content}</h2>'
            new = f'<h2{attrs} data-cms-key="{key}">{content}</h2>'
            if old in html:
                html = html.replace(old, new, 1)
                injected.append(key)

    # Service page hero_body
    if template == "service":
        m_body = re.search(r'(<p style="color:rgba[(]255,255,255,[.]85[)][^"]*">)', html)
        if m_body and 'data-cms-key' not in m_body.group(1):
            old = m_body.group(1)
            new = old[:-1] + ' data-cms-key="hero_body">'
            html = html.replace(old, new, 1)
            injected.append("hero_body")

    return html, list(dict.fromkeys(injected))

class AutoInjectRequest(BaseModel):
    tenant_id: str
    page_slug: str
    overwrite: bool = False

@router.post("/auto-inject")
async def auto_inject(payload: AutoInjectRequest, _: bool = Depends(require_admin)):
    """
    One-shot endpoint for new pages:
    1. Fetch the live HTML from S3
    2. Detect page type and inject data-cms-key attributes
    3. Upload the modified HTML back to S3
    4. Seed the DB with the extracted content values
    5. Invalidate CloudFront
    Returns a full report of what was done.
    """
    if payload.tenant_id != "nexabuilder":
        return {"status": "skipped", "message": "Only nexabuilder.com pages need injection"}

    slug = payload.page_slug.strip("/") or "home"
    s3_key = slug_to_s3_key(slug)
    s3 = _get_s3()
    cf = _get_cf()

    # Step 1: fetch HTML
    html = _fetch_html(s3, s3_key)

    # Step 2: inject keys
    new_html, keys_added = auto_inject_cms_keys(html, slug)
    html_changed = new_html != html

    if html_changed:
        # Step 3: upload
        _upload_html(s3, s3_key, new_html)

    # Step 4: extract values and seed DB
    blocks_seeded = []
    if keys_added:
        import sys; sys.path.insert(0, '/var/www/nexabuilder/backend/current')
        try:
            db_url = os.getenv("DATABASE_URL","")
            from sqlalchemy import select as _select, text as _text, create_engine as _ce
            from sqlalchemy.orm import sessionmaker as _sm
            _engine = _ce(db_url.replace("postgresql+asyncpg","postgresql+psycopg2"), echo=False)
            conflict = "DO UPDATE SET content_type=EXCLUDED.content_type, value=EXCLUDED.value, is_published=true" if payload.overwrite else "DO NOTHING"
            with _sm(bind=_engine)() as session:
                for key in keys_added:
                    ct, value = _extract_block_value(new_html, key)
                    if value:
                        session.execute(_text(
                            "INSERT INTO content_blocks (tenant_id,page_slug,block_key,content_type,value,is_published,version,updated_by) "
                            "VALUES (:t,:p,:k,:ct,:v,true,1,'auto_inject') "
                            f"ON CONFLICT (tenant_id,page_slug,block_key) {conflict}"
                        ), {"t": payload.tenant_id, "p": slug, "k": key, "ct": ct, "v": value})
                        blocks_seeded.append({"key": key, "type": ct, "value": value[:40]})
                session.commit()
        except Exception as e:
            return {"status": "partial", "html_updated": html_changed, "keys": keys_added,
                    "db_error": str(e)[:200]}

    # Step 5: CF invalidate
    if html_changed:
        _invalidate(cf, slug, s3_key)

    # Also extract SEO head tags and seed them
    seo_seeded = []
    try:
        db3 = None
        import sys as _sys; _sys.path.insert(0, '/var/www/nexabuilder/backend/current')
        from sqlalchemy import text as _sqlt3, create_engine as _ce3
        from sqlalchemy.orm import sessionmaker as _sm3
        _engine3 = _ce3(os.getenv("DATABASE_URL","").replace("postgresql+asyncpg","postgresql+psycopg2"),echo=False)
        conflict3 = "DO UPDATE SET value=EXCLUDED.value, content_type=EXCLUDED.content_type" if payload.overwrite else "DO NOTHING"
        _t3 = re.search(r"<title>(.*?)</title>", new_html, re.DOTALL)
        _tg_desc3 = re.search(r"<meta[^>]+name=[^>]*description[^>]*>", new_html, re.IGNORECASE)
        _tg_can3  = re.search(r"<link[^>]+canonical[^>]+>", new_html, re.IGNORECASE)
        _dv3 = re.search('content="([^"]*)"', _tg_desc3.group() if _tg_desc3 else '')
        _cv3 = re.search('href="([^"]*)"', _tg_can3.group() if _tg_can3 else '')
        _seo_pairs3 = [
            (_t3.group(1).strip() if _t3 else None, "seo_title"),
            (_dv3.group(1).strip() if _dv3 else None, "seo_description"),
            (_cv3.group(1).strip() if _cv3 else None, "canonical"),
        ]
        with _sm3(bind=_engine3)() as _s3:
            for _val3, _key3 in _seo_pairs3:
                if _val3:
                    _s3.execute(_sqlt3(
                        "INSERT INTO content_blocks (tenant_id,page_slug,block_key,content_type,value,is_published,version,updated_by) "
                        "VALUES (:t,:p,:k,'text',:v,true,1,'auto_inject_seo') "
                        f"ON CONFLICT (tenant_id,page_slug,block_key) {conflict3}"
                    ), {"t":payload.tenant_id,"p":slug,"k":_key3,"v":_val3})
                    seo_seeded.append(_key3)
            _s3.commit()
    except Exception:
        pass

    return {
        "status": "done",
        "page": slug,
        "s3_key": s3_key,
        "html_updated": html_changed,
        "keys_injected": keys_added,
        "blocks_seeded": len(blocks_seeded),
        "seo_seeded": seo_seeded,
        "blocks": blocks_seeded,
    }
