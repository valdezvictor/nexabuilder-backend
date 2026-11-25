"""
NexaBuilder location pages router — SQLAlchemy async engine
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException
from sqlalchemy import text
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates" / "public"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

NAV_MATRIX = {
    "home": {"home"}, "locations": {"locations"}, "county": {"locations"},
    "city": {"locations"}, "services": {"services"}, "service": {"services"},
    "guides": {"guides"}, "guide": {"guides"}, "blog": {"blog"},
    "post": {"blog"}, "get_quote": {"get_quote"},
    "es_county": {"locations"}, "es_city": {"locations"},
}

def nav_active(page_id, nav_key):
    return "active" if nav_key in NAV_MATRIX.get(page_id, set()) else ""

templates.env.globals["nav_active"] = nav_active

def _compute_lang_hrefs(path):
    if path.startswith("/es/"):
        return path, "/" + path[4:]
    return "/es" + path, path

def base_context(request, page_id, lang="en", **extra):
    path = request.url.path
    es_href, en_href = _compute_lang_hrefs(path)
    ctx = {
        "request": request, "page_id": page_id, "lang": lang,
        "es_href": es_href, "en_href": en_href,
        "canonical": f"https://nexabuilder.com{path}",
        "hreflang_en": f"https://nexabuilder.com{en_href}",
        "hreflang_es": f"https://nexabuilder.com{es_href}",
        "current_year": datetime.now().year,
    }
    ctx.update(extra)
    return ctx

router = APIRouter(tags=["public-pages"])

@router.get("/locations/", response_class=HTMLResponse)
async def locations_index(request: Request):
    from app.db import engine
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT slug, name, city_count, meta_description FROM nb_counties ORDER BY name")
            )
            counties = [dict(r) for r in result.mappings().fetchall()]
    except Exception:
        counties = []
    ctx = base_context(request, "locations",
        title="Find Local Contractors by Location | NexaBuilder",
        meta_description="Find licensed contractors in your area.",
        counties=counties)
    return templates.TemplateResponse("locations/index.html", ctx)

@router.get("/locations/{county_slug}/", response_class=HTMLResponse)
async def county_page(request: Request, county_slug: str):
    from app.db import engine
    county = None
    cities = []
    contractor_count = 0
    try:
        async with engine.connect() as conn:
            row = await conn.execute(
                text("SELECT * FROM nb_counties WHERE slug = :slug"),
                {"slug": county_slug}
            )
            county = dict(row.mappings().first() or {}) or None
            if county:
                cr = await conn.execute(
                    text("SELECT slug, name, h1_title, contractor_count FROM nb_cities WHERE county_slug = :slug ORDER BY name"),
                    {"slug": county_slug}
                )
                cities = [dict(r) for r in cr.mappings().fetchall()]
                ccr = await conn.execute(
                    text("SELECT COUNT(*) FROM contractors WHERE LOWER(REPLACE(county,' ','-')) = :slug OR LOWER(county) = LOWER(:name)"),
                    {"slug": county_slug, "name": county["name"]}
                )
                contractor_count = ccr.scalar() or 0
    except Exception:
        pass
    if not county:
        raise HTTPException(status_code=404, detail="County not found")
    h1 = county.get("h1_title") or f"Find Contractors in {county['name']} County"
    meta_desc = county.get("meta_description") or f"Connect with licensed contractors in {county['name']} County, CA."
    ctx = base_context(request, "county",
        title=f"Contractors in {county['name']} County | NexaBuilder",
        meta_description=meta_desc, county=county, cities=cities,
        contractor_count=contractor_count, h1=h1)
    return templates.TemplateResponse("locations/county.html", ctx)

@router.get("/locations/{county_slug}/{city_slug}/", response_class=HTMLResponse)
async def city_page(request: Request, county_slug: str, city_slug: str):
    from app.db import engine
    county = None
    city = None
    contractors = []
    contractor_count = 0
    try:
        async with engine.connect() as conn:
            cr = await conn.execute(
                text("SELECT * FROM nb_counties WHERE slug = :slug"),
                {"slug": county_slug}
            )
            county = dict(cr.mappings().first() or {}) or None
            if county:
                cir = await conn.execute(
                    text("SELECT * FROM nb_cities WHERE county_slug = :county AND slug = :city AND is_published = TRUE"),
                    {"county": county_slug, "city": city_slug}
                )
                city = dict(cir.mappings().first() or {}) or None
                contr = await conn.execute(
                    text("""SELECT id, business_name, city, county, phone, email,
                                   license_no, classifications, primary_status
                            FROM contractors
                            WHERE LOWER(REPLACE(city,' ','-')) = :city
                              AND (LOWER(REPLACE(county,' ','-')) = :county
                                   OR LOWER(county) = LOWER(:county_name))
                              AND primary_status = 'Active'
                            ORDER BY business_name LIMIT 20"""),
                    {"city": city_slug, "county": county_slug, "county_name": county["name"]}
                )
                contractors = [dict(r) for r in contr.mappings().fetchall()]
                ccr = await conn.execute(
                    text("SELECT COUNT(*) FROM contractors WHERE LOWER(REPLACE(city,' ','-')) = :city AND primary_status = 'Active'"),
                    {"city": city_slug}
                )
                contractor_count = ccr.scalar() or 0
    except Exception:
        pass
    if not county:
        raise HTTPException(status_code=404, detail="County not found")
    city_name = city["name"] if city else city_slug.replace("-", " ").title()
    h1 = (city.get("h1_title") if city else None) or f"Contractors in {city_name}, {county['name']}"
    meta_desc = (city.get("meta_description") if city else None) or f"Find licensed contractors in {city_name}, {county['name']} County, CA."
    body_html = (city.get("body_html") if city else None) or ""
    ctx = base_context(request, "city",
        title=f"{h1} | NexaBuilder", meta_description=meta_desc,
        county=county, city=city, city_name=city_name, city_slug=city_slug,
        contractors=contractors, contractor_count=contractor_count,
        h1=h1, body_html=body_html)
    return templates.TemplateResponse("locations/city.html", ctx)

@router.get("/es/locations/{county_slug}/", response_class=HTMLResponse)
async def es_county_page(request: Request, county_slug: str):
    from app.db import engine
    county = None
    cities = []
    try:
        async with engine.connect() as conn:
            row = await conn.execute(
                text("SELECT * FROM nb_counties WHERE slug = :slug"),
                {"slug": county_slug}
            )
            county = dict(row.mappings().first() or {}) or None
            if county:
                cr = await conn.execute(
                    text("SELECT slug, name FROM nb_cities WHERE county_slug = :slug ORDER BY name"),
                    {"slug": county_slug}
                )
                cities = [dict(r) for r in cr.mappings().fetchall()]
    except Exception:
        pass
    if not county:
        raise HTTPException(status_code=404)
    h1 = county.get("h1_title") or f"Contratistas en el Condado de {county['name']}"
    meta_desc = county.get("meta_description_es") or f"Encuentra contratistas con licencia en el Condado de {county['name']}, CA."
    ctx = base_context(request, "es_county", lang="es",
        title=f"Contratistas en {county['name']} | NexaBuilder",
        meta_description=meta_desc, county=county, cities=cities, h1=h1)
    return templates.TemplateResponse("locations/county_es.html", ctx)


# No-trailing-slash aliases (redirect_slashes=False on app)
@router.get("/locations", response_class=HTMLResponse, include_in_schema=False)
async def _loc_ns(request: Request):
    return await locations_index(request)

@router.get("/locations/{county_slug}", response_class=HTMLResponse, include_in_schema=False)
async def _cty_ns(request: Request, county_slug: str):
    return await county_page(request, county_slug)

@router.get("/locations/{county_slug}/{city_slug}", response_class=HTMLResponse, include_in_schema=False)
async def _city_ns(request: Request, county_slug: str, city_slug: str):
    return await city_page(request, county_slug, city_slug)

@router.get("/es/locations/{county_slug}", response_class=HTMLResponse, include_in_schema=False)
async def _es_cty_ns(request: Request, county_slug: str):
    return await es_county_page(request, county_slug)
