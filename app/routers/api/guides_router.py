from fastapi import APIRouter, Header, HTTPException, BackgroundTasks
from sqlalchemy import text as sqlt
from sqlalchemy.orm import Session
import os as _os2
def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        _os2.getenv('DATABASE_URL', '').replace('postgresql+asyncpg', 'postgresql+psycopg2'),
        pool_pre_ping=True
    )
    return sessionmaker(bind=engine)()
import httpx as _hx
import logging, re as _re, subprocess as _sp, os as _os

log = logging.getLogger("guides")
router = APIRouter()
ADMIN_KEY = "GidhUSbSVmhSzpY8Xd7gfBEJJYB-ycHKz5j-JxEYSpU"
CDM_KEY = "24dejulio_internal"
CDM_URL = "https://api.techcial.com"
BUCKET = "nexabuilder-root-site-979841141166-us-west-1-an"


def _require_admin(key: str):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/guides")
def list_guides(x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    db = _db()
    try:
        rows = db.execute(sqlt(
            "SELECT id,slug,title,category,status,word_count,quality_score,"
            "needs_hero_image,needs_diagram,published_at,created_at,updated_at "
            "FROM seo_guide_pages ORDER BY category,title"
        )).fetchall()
        return [dict(r._mapping) for r in rows]
    finally:
        db.close()


@router.get("/guides/{slug}")
def get_guide(slug: str, x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT * FROM seo_guide_pages WHERE slug=:s"
        ), {"s": slug}).fetchone()
        if not row:
            raise HTTPException(404, "Guide not found")
        return dict(row._mapping)
    finally:
        db.close()


@router.post("/guides/{slug}/generate")
async def generate_guide(slug: str, x_admin_key: str = Header(...), bg: BackgroundTasks = None):
    _require_admin(x_admin_key)
    db = _db()
    try:
        row = db.execute(sqlt("SELECT * FROM seo_guide_pages WHERE slug=:s"), {"s": slug}).fetchone()
        if not row:
            raise HTTPException(404, "Guide not found")
        g = dict(row._mapping)
        db.execute(sqlt("UPDATE seo_guide_pages SET status='GENERATING',updated_at=NOW() WHERE slug=:s"), {"s": slug})
        db.commit()
    finally:
        db.close()

    async def _do_generate():
        db2 = _db()
        try:
            prompt = (
                f"Write a comprehensive, SEO-optimized guide page for NexaBuilder.com titled: {g['title']}. "
                f"Category: {g['category']}. "
                "Write for Southern California homeowners. "
                "Include: H1, Quick Answer (40-60 words), 3-4 H2 sections with detailed content, "
                "cost data table where relevant, 4 FAQ questions with answers, "
                "internal links to /get-quote/ (anchor: get free contractor quotes) and /guides/verify-cslb-license/. "
                "Cite BPC 7159 for any contract/payment content. "
                "Include CSLB License #1127866 as the NexaBuilder verification example. "
                "Return as clean HTML body only (no <html>/<head>/<body> tags). "
                "Target 900-1200 words."
            )
            import anthropic as _ant
            client = _ant.AsyncAnthropic()
            msg = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4000,
                system="You are a senior content writer for NexaBuilder.com, a Southern California contractor matching platform. Write authoritative, SEO-optimized guide content for homeowners.",
                messages=[{"role": "user", "content": prompt}]
            )
            html_body = msg.content[0].text.strip()
            word_count = len(_re.sub(r"<[^>]+>", " ", html_body).split())
            db2.execute(sqlt(
                "UPDATE seo_guide_pages SET "
                "generated_html_body=:body, word_count=:wc, "
                "status='DRAFT', updated_at=NOW() WHERE slug=:s"
            ), {"body": html_body, "wc": word_count, "s": slug})
            db2.commit()
            log.info(f"Guide generated: {slug} ({word_count} words)")
        except Exception as e:
            log.error(f"Guide generate error {slug}: {e}")
            db2.execute(sqlt("UPDATE seo_guide_pages SET status='STUB',updated_at=NOW() WHERE slug=:s"), {"s": slug})
            db2.commit()
        finally:
            db2.close()

    bg.add_task(_do_generate)
    return {"status": "GENERATING", "slug": slug}


@router.post("/guides/{slug}/review")
async def review_guide(slug: str, x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    db = _db()
    try:
        row = db.execute(sqlt("SELECT * FROM seo_guide_pages WHERE slug=:s"), {"s": slug}).fetchone()
        if not row:
            raise HTTPException(404, "Guide not found")
        g = dict(row._mapping)
        if not g.get("generated_html_body"):
            raise HTTPException(400, "No content to review. Generate first.")
    finally:
        db.close()

    from app.link_extractor import extract_internal_links, build_link_section
    raw_html = g["generated_html_body"]
    found_links = extract_internal_links(raw_html)
    body_text = _re.sub(r"<[^>]+>", " ", raw_html)
    body_text = _re.sub(r"\s+", " ", body_text).strip()
    body_text += build_link_section(found_links)

    async with _hx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{CDM_URL}/v1/content/submit",
            headers={"x-api-key": CDM_KEY, "content-type": "application/json"},
            json={
                "brand_id": "nexabuilder",
                "domain": "nexabuilder.com",
                "content_type": "guide_page",
                "title": g["title"],
                "slug": slug,
                "body_markdown": body_text[:12000],
                "meta_description": g.get("target_meta_description") or "",
                "target_query": g["title"],
                "language": "en",
            }
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"CDM error: {resp.status_code}")

    result = resp.json()
    score = result.get("overall_score", 0)
    notes = result.get("notes", "")

    db3 = _db()
    try:
        db3.execute(sqlt(
            "UPDATE seo_guide_pages SET quality_score=:sc, content_notes=:n, updated_at=NOW() WHERE slug=:s"
        ), {"sc": score, "n": notes, "s": slug})
        db3.commit()
    finally:
        db3.close()

    return {
        "slug": slug, "overall_score": score, "passed": score >= 75,
        "notes": notes, "scores": result.get("scores", {})
    }


@router.post("/guides/{slug}/publish")
async def publish_guide(slug: str, x_admin_key: str = Header(...), bg: BackgroundTasks = None):
    _require_admin(x_admin_key)
    db = _db()
    try:
        row = db.execute(sqlt("SELECT * FROM seo_guide_pages WHERE slug=:s"), {"s": slug}).fetchone()
        if not row:
            raise HTTPException(404, "Guide not found")
        g = dict(row._mapping)
        if not g.get("generated_html_body"):
            raise HTTPException(400, "No content to publish.")
        db.execute(sqlt(
            "UPDATE seo_guide_pages SET status='PUBLISHED', published_at=NOW(), updated_at=NOW() WHERE slug=:s"
        ), {"s": slug})
        db.commit()
    finally:
        db.close()

    if bg:
        bg.add_task(_deploy_guide, slug, g)
    return {"status": "PUBLISHED", "slug": slug, "url": f"/guides/{slug}/"}


async def _deploy_guide(slug: str, g: dict):
    try:
        import subprocess as _sp3
        result = _sp3.run(
            ["/var/www/nexabuilder/backend/current/venv/bin/python",
             "/home/ec2-user/deploy_guide.py", slug],
            capture_output=True, text=True, timeout=90
        )
        log.info(f"Guide deploy [{slug}]: {result.stdout.strip()[-200:]}")
        if result.returncode != 0:
            log.error(f"Guide deploy error: {result.stderr[:200]}")
        _sp3.run([
            "aws", "cloudfront", "create-invalidation",
            "--distribution-id", "EDLQAZ1IS2WIG",
            "--paths", f"/guides/{slug}/", "/guides/",
            "--region", "us-east-1"
        ], capture_output=True, timeout=30)
        log.info(f"Guide live: /guides/{slug}/")
        _sp3.run(["/var/www/nexabuilder/backend/current/venv/bin/python","/home/ec2-user/rebuild_guides_index.py"],capture_output=True,timeout=30)
        log.info("Guides index rebuilt after publish")
    except Exception as e:
        log.error(f"Guide deploy failed [{slug}]: {e}")

@router.post("/guides/bulk-publish")
async def bulk_publish(x_admin_key: str = Header(...), bg: BackgroundTasks = None):
    _require_admin(x_admin_key)
    db = _db()
    try:
        rows = db.execute(sqlt(
            "SELECT slug, title, generated_html_body FROM seo_guide_pages "
            "WHERE quality_score >= 75 AND status != 'PUBLISHED' AND generated_html_body IS NOT NULL"
        )).fetchall()
        guides = [dict(r._mapping) for r in rows]
        for g in guides:
            db.execute(sqlt(
                "UPDATE seo_guide_pages SET status='PUBLISHED', published_at=NOW(), updated_at=NOW() WHERE slug=:s"
            ), {"s": g["slug"]})
        db.commit()
    finally:
        db.close()

    for g in guides:
        if bg:
            bg.add_task(_deploy_guide, g["slug"], g)

    return {"published": len(guides), "slugs": [g["slug"] for g in guides]}
