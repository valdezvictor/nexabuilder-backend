"""
seo_meta_manager.py — Unified SEO Meta Manager for NexaBuilder Admin
Router prefix: /admin/seo-manager

Surfaces ALL content pages (cities, materials, guides, services) with their
title/meta health status and provides inline-edit + CDM-generate capabilities.

Endpoints:
  GET  /admin/seo-manager/audit          — full page inventory with SEO status
  GET  /admin/seo-manager/issues         — only pages with issues
  PUT  /admin/seo-manager/city/{county}/{slug}   — save city title/meta
  PUT  /admin/seo-manager/guide/{slug}           — save guide title/meta
  PUT  /admin/seo-manager/material/{slug}        — save material SEO description
  POST /admin/seo-manager/city/{county}/{slug}/regenerate  — trigger CDM regen
"""

import os, logging
from typing import Optional, List, Dict

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/seo-manager", tags=["SEO Meta Manager"])

ADMIN_KEY = os.getenv("CMS_ADMIN_KEY", "")


def _require_admin(key: str):
    if not key or key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg", "postgresql+psycopg2"),
        pool_pre_ping=True
    )
    return sessionmaker(bind=engine)()


def _classify_title(title):
    if not title: return "missing"
    if len(title) > 60: return "too_long"
    if len(title) < 10: return "too_short"
    return "ok"


def _classify_meta(meta):
    if not meta: return "missing"
    if len(meta) > 160: return "too_long"
    if len(meta) < 50: return "too_short"
    return "ok"


def _has_issue(ts, ms):
    return ts != "ok" or ms != "ok"


def _get_city_pages(db):
    from sqlalchemy import text as sqlt
    rows = db.execute(sqlt("""
        SELECT county_slug, slug, name,
               h1_title AS title, meta_description AS meta,
               is_published, updated_at
        FROM nb_cities ORDER BY county_slug, name
    """)).fetchall()
    pages = []
    for r in rows:
        d = dict(r._mapping)
        t = _classify_title(d.get("title"))
        m = _classify_meta(d.get("meta"))
        pages.append({
            "page_type": "city",
            "id": f"city::{d['county_slug']}::{d['slug']}",
            "url": f"/contractors/{d['county_slug']}/{d['slug']}",
            "name": d["name"],
            "county": d["county_slug"],
            "slug": d["slug"],
            "title": d.get("title") or "",
            "title_len": len(d.get("title") or ""),
            "title_status": t,
            "meta": d.get("meta") or "",
            "meta_len": len(d.get("meta") or ""),
            "meta_status": m,
            "is_published": bool(d.get("is_published")),
            "updated_at": str(d.get("updated_at") or ""),
            "can_regenerate": True,
            "title_dup_count": 0,
            "meta_dup_count": 0,
        })
    return pages


def _get_guide_pages(db):
    from sqlalchemy import text as sqlt
    try:
        rows = db.execute(sqlt("""
            SELECT slug, title, meta_description AS meta,
                   category, status, updated_at
            FROM seo_guide_pages ORDER BY category, title
        """)).fetchall()
    except Exception:
        return []
    pages = []
    for r in rows:
        d = dict(r._mapping)
        t = _classify_title(d.get("title"))
        m = _classify_meta(d.get("meta"))
        pages.append({
            "page_type": "guide",
            "id": f"guide::{d['slug']}",
            "url": f"/guides/{d['slug']}",
            "name": d.get("title") or d["slug"],
            "county": "",
            "slug": d["slug"],
            "title": d.get("title") or "",
            "title_len": len(d.get("title") or ""),
            "title_status": t,
            "meta": d.get("meta") or "",
            "meta_len": len(d.get("meta") or ""),
            "meta_status": m,
            "is_published": d.get("status") == "published",
            "updated_at": str(d.get("updated_at") or ""),
            "can_regenerate": False,
            "title_dup_count": 0,
            "meta_dup_count": 0,
        })
    return pages


def _get_material_pages(db):
    from sqlalchemy import text as sqlt
    try:
        rows = db.execute(sqlt("""
            SELECT slug, display_name, seo_description AS meta,
                   category, availability, updated_at
            FROM materials_catalog ORDER BY category, display_name
        """)).fetchall()
    except Exception:
        return []
    pages = []
    for r in rows:
        d = dict(r._mapping)
        title = d.get("display_name") or ""
        t = _classify_title(title)
        m = _classify_meta(d.get("meta"))
        pages.append({
            "page_type": "material",
            "id": f"material::{d['slug']}",
            "url": f"/materials/{d['slug']}",
            "name": title,
            "county": "",
            "slug": d["slug"],
            "title": title,
            "title_len": len(title),
            "title_status": t,
            "meta": d.get("meta") or "",
            "meta_len": len(d.get("meta") or ""),
            "meta_status": m,
            "is_published": d.get("availability") != "discontinued",
            "updated_at": str(d.get("updated_at") or ""),
            "can_regenerate": False,
            "title_dup_count": 0,
            "meta_dup_count": 0,
        })
    return pages


def _flag_duplicates(pages):
    from collections import Counter
    tc = Counter(p["title"] for p in pages if p["title"])
    mc = Counter(p["meta"]  for p in pages if p["meta"])
    for p in pages:
        if p["title"] and tc[p["title"]] > 1:
            p["title_status"] = "duplicate"
            p["title_dup_count"] = tc[p["title"]]
        if p["meta"] and mc[p["meta"]] > 1:
            p["meta_status"] = "duplicate"
            p["meta_dup_count"] = mc[p["meta"]]
    return pages


@router.get("/audit")
def get_audit(
    page_type: Optional[str] = Query(None),
    issues_only: bool = Query(False),
    search: Optional[str] = Query(None),
    x_admin_key: str = Header(...),
):
    _require_admin(x_admin_key)
    db = _db()
    try:
        pages: List[Dict] = []
        pt = (page_type or "all").lower()
        if pt in ("all", "city"):     pages += _get_city_pages(db)
        if pt in ("all", "guide"):    pages += _get_guide_pages(db)
        if pt in ("all", "material"): pages += _get_material_pages(db)

        pages = _flag_duplicates(pages)
        if issues_only:
            pages = [p for p in pages if _has_issue(p["title_status"], p["meta_status"])]
        if search:
            q = search.lower()
            pages = [p for p in pages if q in p["name"].lower() or q in p["slug"]]

        by_type: Dict = {}
        ic = dict(missing_title=0, missing_meta=0, too_long_title=0,
                  too_long_meta=0, duplicate_title=0, duplicate_meta=0)
        for p in pages:
            by_type[p["page_type"]] = by_type.get(p["page_type"], 0) + 1
            if p["title_status"] == "missing":   ic["missing_title"]   += 1
            if p["meta_status"]  == "missing":   ic["missing_meta"]    += 1
            if p["title_status"] == "too_long":  ic["too_long_title"]  += 1
            if p["meta_status"]  == "too_long":  ic["too_long_meta"]   += 1
            if p["title_status"] == "duplicate": ic["duplicate_title"] += 1
            if p["meta_status"]  == "duplicate": ic["duplicate_meta"]  += 1

        return {"total": len(pages), "by_type": by_type, "issue_counts": ic, "pages": pages}
    finally:
        db.close()


@router.get("/issues")
def get_issues(page_type: Optional[str] = Query(None), x_admin_key: str = Header(...)):
    return get_audit(page_type=page_type, issues_only=True, search=None, x_admin_key=x_admin_key)


class CityMetaUpdate(BaseModel):
    h1_title: Optional[str] = None
    meta_description: Optional[str] = None


@router.put("/city/{county}/{slug}")
def update_city_meta(county: str, slug: str, body: CityMetaUpdate, x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    from sqlalchemy import text as sqlt
    db = _db()
    try:
        row = db.execute(sqlt("SELECT id FROM nb_cities WHERE county_slug=:c AND slug=:s"),
                         {"c": county, "s": slug}).fetchone()
        if not row:
            raise HTTPException(404, "City not found")
        updates, params = [], {"c": county, "s": slug}
        if body.h1_title is not None:
            updates.append("h1_title = :title"); params["title"] = body.h1_title.strip()
        if body.meta_description is not None:
            updates.append("meta_description = :meta"); params["meta"] = body.meta_description.strip()
        if not updates:
            raise HTTPException(400, "Nothing to update")
        updates.append("updated_at = NOW()")
        db.execute(sqlt(f"UPDATE nb_cities SET {', '.join(updates)} WHERE county_slug=:c AND slug=:s"), params)
        db.commit()
        return {"ok": True, "county": county, "slug": slug}
    finally:
        db.close()


class GuideMetaUpdate(BaseModel):
    title: Optional[str] = None
    meta_description: Optional[str] = None


@router.put("/guide/{slug}")
def update_guide_meta(slug: str, body: GuideMetaUpdate, x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    from sqlalchemy import text as sqlt
    db = _db()
    try:
        row = db.execute(sqlt("SELECT id FROM seo_guide_pages WHERE slug=:s"), {"s": slug}).fetchone()
        if not row:
            raise HTTPException(404, "Guide not found")
        updates, params = [], {"s": slug}
        if body.title is not None:
            updates.append("title = :title"); params["title"] = body.title.strip()
        if body.meta_description is not None:
            updates.append("meta_description = :meta"); params["meta"] = body.meta_description.strip()
        if not updates:
            raise HTTPException(400, "Nothing to update")
        updates.append("updated_at = NOW()")
        db.execute(sqlt(f"UPDATE seo_guide_pages SET {', '.join(updates)} WHERE slug=:s"), params)
        db.commit()
        return {"ok": True, "slug": slug}
    finally:
        db.close()


class MaterialMetaUpdate(BaseModel):
    seo_description: Optional[str] = None


@router.put("/material/{slug}")
def update_material_meta(slug: str, body: MaterialMetaUpdate, x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    from sqlalchemy import text as sqlt
    db = _db()
    try:
        row = db.execute(sqlt("SELECT id FROM materials_catalog WHERE slug=:s"), {"s": slug}).fetchone()
        if not row:
            raise HTTPException(404, "Material not found")
        if body.seo_description is None:
            raise HTTPException(400, "Nothing to update")
        db.execute(sqlt("UPDATE materials_catalog SET seo_description=:meta, updated_at=NOW() WHERE slug=:s"),
                   {"meta": body.seo_description.strip(), "s": slug})
        db.commit()
        return {"ok": True, "slug": slug}
    finally:
        db.close()


@router.post("/city/{county}/{slug}/regenerate")
async def regenerate_city(county: str, slug: str, x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    import httpx
    try:
        async with httpx.AsyncClient(timeout=150) as client:
            resp = await client.post(
                f"http://localhost:8000/admin/cities/{county}/{slug}/cdm-generate",
                headers={"Authorization": "Bearer batch-internal"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return {"ok": True, "status": data.get("status"), "score": data.get("score")}
            raise HTTPException(resp.status_code, f"CDM error: {resp.text[:200]}")
    except httpx.TimeoutException:
        raise HTTPException(504, "CDM regeneration timed out")
