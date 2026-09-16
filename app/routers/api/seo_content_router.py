"""
seo_content_router.py — NexaBuilder SEO Content Engine
Implements the async queue+poll pattern from TS-SEO-AI-002:
  POST /api/seo-content/discover      — seed GSC/Bing keywords into seo_topic_discoveries
  POST /api/seo-content/generate      — queue article generation job (returns immediately)
  GET  /api/seo-content/status/{id}   — poll job status
  GET  /api/seo-content/topics        — list discovered topics
  GET  /api/seo-content/profiles      — list writing profiles
  POST /api/seo-content/sync-gsc      — pull GSC keywords into discovery table
"""
import os, logging, asyncio
from datetime import datetime
from fastapi import APIRouter, Header, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import text as sqlt

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/seo-content", tags=["SEO Content Engine"])

ADMIN_KEY = os.getenv("CMS_ADMIN_KEY", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SITE_ID = "nexabuilder"

# Banned terms per spec section 4A
BANNED_TERMS = [
    "delve", "testament", "furthermore", "landscape", "tapestry",
    "leverage", "underscore", "crucial", "vital", "in conclusion",
    "it is worth noting", "it's worth noting", "notably", "seamlessly",
    "game-changer", "game changer", "revolutionize"
]


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


# ── Pydantic models ───────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    topic: str
    discovery_id: int | None = None
    profile_id: int = 1
    top_queries: list = []
    content_type: str = "comparison_article"
    primary_keyword: str = ""


class SeedRequest(BaseModel):
    seed_keywords: list[str] = []   # Extra seed keywords to add manually


# ── Helper: build banned terms filter ────────────────────────────────────────

def _banned_terms_rule():
    return "STRICTLY AVOID these overused AI phrases: " + ", ".join(BANNED_TERMS)


# ── 1. Seed topic discoveries from GSC ───────────────────────────────────────

@router.post("/sync-gsc")
async def sync_gsc_to_discoveries(x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    db = _db()
    try:
        # Pull from gsc_keywords table (already populated by GSC sync)
        rows = db.execute(sqlt("""
            SELECT DISTINCT query, SUM(impressions) as impressions,
                   SUM(clicks) as clicks, AVG(position)::numeric(6,1) as avg_position
            FROM gsc_keywords
            WHERE date_range IN ('last_28_days', 'last_28_days_bing')
            GROUP BY query
            ORDER BY impressions DESC
            LIMIT 100
        """)).fetchall()

        inserted = 0
        for row in rows:
            q = row[0] or ""
            if not q.strip():
                continue
            # Classify intent
            intent = "QUESTION" if any(w in q.lower() for w in ["how","what","why","when","who","which"]) \
                else "COMPARISON" if any(w in q.lower() for w in ["vs","versus","best","top","compare"]) \
                else "PREPOSITION" if any(w in q.lower() for w in ["near me","in ","for ","with "]) \
                else "SOCIAL"
            try:
                db.execute(sqlt("""
                    INSERT INTO seo_topic_discoveries
                      (tenant_id, seed_keyword, discovered_query, intent_category,
                       impressions, clicks, avg_position, source)
                    VALUES (:tid, :seed, :q, :intent, :imp, :clicks, :pos, 'gsc')
                    ON CONFLICT (tenant_id, discovered_query) DO UPDATE SET
                      impressions=GREATEST(seo_topic_discoveries.impressions, :imp),
                      clicks=GREATEST(seo_topic_discoveries.clicks, :clicks),
                      avg_position=:pos
                """), {
                    "tid": SITE_ID, "seed": q.split()[0] if q else "",
                    "q": q, "intent": intent,
                    "imp": int(row[1] or 0), "clicks": int(row[2] or 0),
                    "pos": float(row[3] or 0)
                })
                inserted += 1
            except Exception as e:
                log.warning(f"Discovery insert error: {e}")

        db.commit()
        total = db.execute(sqlt(
            "SELECT COUNT(*) FROM seo_topic_discoveries WHERE tenant_id='nexabuilder'"
        )).scalar()
        return {"status": "ok", "synced": inserted, "total_discoveries": total}
    finally:
        db.close()


# ── 2. List discovered topics ─────────────────────────────────────────────────

@router.get("/topics")
async def list_topics(limit: int = 50, unprocessed_only: bool = False,
                      x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    db = _db()
    try:
        where = "WHERE tenant_id='nexabuilder'"
        if unprocessed_only:
            where += " AND is_processed_to_article=FALSE"
        rows = db.execute(sqlt(f"""
            SELECT id, discovered_query, intent_category, impressions, clicks,
                   avg_position, is_processed_to_article, source, created_at
            FROM seo_topic_discoveries
            {where}
            ORDER BY impressions DESC, trend_velocity_score DESC
            LIMIT :limit
        """), {"limit": limit}).fetchall()
        return {"topics": [dict(r._mapping) for r in rows]}
    finally:
        db.close()


# ── 3. List writing profiles ──────────────────────────────────────────────────

@router.get("/profiles")
async def list_profiles(x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    db = _db()
    try:
        rows = db.execute(sqlt(
            "SELECT id, profile_name, writing_style, target_audience FROM ai_writing_profiles ORDER BY id"
        )).fetchall()
        return {"profiles": [dict(r._mapping) for r in rows]}
    finally:
        db.close()


# ── 4. Queue article generation (returns immediately) ────────────────────────

@router.post("/generate")
async def generate_article(payload: GenerateRequest,
                            bg: BackgroundTasks,
                            x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    if not ANTHROPIC_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    db = _db()
    try:
        # Get writing profile
        profile = db.execute(sqlt(
            "SELECT * FROM ai_writing_profiles WHERE id=:id"
        ), {"id": payload.profile_id}).fetchone()
        if not profile:
            profile = db.execute(sqlt("SELECT * FROM ai_writing_profiles LIMIT 1")).fetchone()

        # Create the generation job record
        result = db.execute(sqlt("""
            INSERT INTO ai_generated_articles
              (discovery_id, writing_profile_id, title, slug, status,
               primary_keyword, content_type, source, created_at)
            VALUES (:did, :pid, :title, :slug, 'QUEUED',
                    :kw, :ct, 'local', NOW())
            RETURNING id
        """), {
            "did": payload.discovery_id,
            "pid": payload.profile_id,
            "title": payload.topic[:255],
            "slug": payload.topic.lower().replace(" ","-")[:80],
            "kw": payload.primary_keyword or payload.topic,
            "ct": payload.content_type,
        })
        job_id = result.fetchone()[0]
        db.commit()

        # Queue the actual generation as a background task
        profile_dict = dict(profile._mapping) if profile else {}
        bg.add_task(
            _run_generation,
            job_id=job_id,
            topic=payload.topic,
            profile=profile_dict,
            top_queries=payload.top_queries,
            content_type=payload.content_type,
            primary_keyword=payload.primary_keyword or payload.topic,
        )

        return {
            "status": "QUEUED",
            "job_id": job_id,
            "message": "Article generation queued. Poll /api/seo-content/status/{job_id} for progress.",
            "poll_url": f"/api/seo-content/status/{job_id}"
        }
    finally:
        db.close()


# ── 5. Poll job status ────────────────────────────────────────────────────────

@router.get("/status/{job_id}")
async def get_status(job_id: int, x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    db = _db()
    try:
        row = db.execute(sqlt("""
            SELECT id, status, title, slug, primary_keyword,
                   meta_description, body_html, blog_article_id,
                   generation_tokens, error_message,
                   created_at, completed_at
            FROM ai_generated_articles WHERE id=:id
        """), {"id": job_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")
        d = dict(row._mapping)
        # Calculate progress percentage based on status
        progress = {"QUEUED":5,"GENERATING":50,"DRAFT":100,"PUBLISHED":100,"FAILED":0}.get(d["status"],0)
        d["progress"] = progress
        return d
    finally:
        db.close()


# ── 6. Background generation task ────────────────────────────────────────────

async def _run_generation(job_id, topic, profile,
                          top_queries, content_type="comparison_article",
                          primary_keyword=""):
    import httpx as _httpx, re as _re2
    kw = (primary_keyword or topic).strip()
    db = _db()
    try:
        db.execute(sqlt(
            "UPDATE ai_generated_articles SET status='GENERATING', content_type=:ct WHERE id=:id"
        ), {"id": job_id, "ct": content_type})
        db.commit()

        article_data = None
        source = "local"

        # ── CDM-first ─────────────────────────────────────────────────────────
        try:
            async with _httpx.AsyncClient(timeout=90) as c:
                resp = await c.post(
                    "https://api.techcial.com/v1/content/generate",
                    headers={"x-api-key": "24dejulio_internal",
                             "content-type": "application/json"},
                    json={"brand_id": "nexabuilder", "content_type": content_type,
                          "topic": topic, "primary_keyword": kw, "language": "en"},
                )
            if resp.status_code == 200:
                d = resp.json()
                if d.get("body_html"):
                    article_data = {
                        "title": d.get("title", topic[:255]),
                        "slug": d.get("slug", kw.lower().replace(" ", "-")[:80]),
                        "body_html": d["body_html"],
                        "meta_description": (d.get("meta_description") or "")[:160],
                        "cdm_request_id": d.get("request_id"),
                        "word_count": d.get("word_count"),
                    }
                    source = "cdm"
                    log.info(f"Job {job_id} — CDM generation succeeded ({d.get('word_count')} words)")
        except Exception as cdm_err:
            log.warning(f"Job {job_id} — CDM failed ({cdm_err}), falling back to local")

        # ── Local fallback ─────────────────────────────────────────────────────
        if not article_data:
            source = "local"
            guidelines = (profile.get("custom_system_guidelines") or "")
            banned = (profile.get("banned_terms") or "")

            STRUCTURES = {
                "comparison_article": (
                    "1. Opening 40-60 word DIRECT ANSWER (featured snippet).\n"
                    "2. > **Quick Answer:** [2 sentences]\n"
                    "3. Quick Summary bullets (4-5 points)\n"
                    "4. 3-4 H2 sections, each opens with 1-2 sentence summary\n"
                    "5. Cost/comparison table with real SoCal 2024-2025 data\n"
                    "6. H2: FAQ — exactly 4 Q&As (40-80 words each)\n"
                    "7. Conclusion + CTA to /get-quote/\n"
                    "TARGET: 900-1200 words. Complete every section."
                ),
                "guide_page": (
                    "1. Opening 40-60 word DIRECT ANSWER\n"
                    "2. > **Quick Answer:** [2 sentences]\n"
                    "3. H2: What It Is (100-150 words)\n"
                    "4. H2: Requirements & Costs (CSLB license, permit, cost range)\n"
                    "5. H2: How to Hire (verification checklist)\n"
                    "6. H2: FAQ — 4 Q&As (40-80 words each)\n"
                    "7. Conclusion + CTA\n"
                    "TARGET: 800-1100 words."
                ),
                "faq_page": (
                    "1. Opening 40-60 word direct answer\n"
                    "2. 6-8 Q&A pairs, 40-80 words each, self-contained\n"
                    "3. Conclusion + CTA\n"
                    "TARGET: 700-900 words."
                ),
                "location_page": (
                    "1. Opening 40-60 word direct answer for this city\n"
                    "2. H2: [Service] in [City] overview\n"
                    "3. H2: Cost ranges table (SoCal 2024-2025)\n"
                    "4. H2: Why NexaBuilder in [city]\n"
                    "5. H2: FAQ — 3 city-specific Q&As\n"
                    "6. CTA paragraph\n"
                    "TARGET: 600-900 words."
                ),
            }
            structure = STRUCTURES.get(content_type, STRUCTURES["comparison_article"])

            system_parts = [
                "You are the NexaBuilder.com Senior Editorial Director.",
                "",
                guidelines or (
                    "Write for Southern California homeowners. "
                    "CSLB citations required. Include SoCal cost ranges. "
                    "Grade 8 readability. Active voice. "
                    "3-5 internal links to /services/, /locations/los-angeles/, /get-quote/."
                ),
            ]
            if banned:
                system_parts.append(f"BANNED: {banned}")
            system_parts.append(
                "Also avoid: delve, testament, furthermore, landscape, "
                "tapestry, leverage, comprehensive, seamlessly, game-changer."
            )
            system_prompt = "\n".join(system_parts)

            user_lines = [
                f"Write a complete {content_type.replace('_', ' ')} article.",
                f"TOPIC: {topic}",
                f"PRIMARY KEYWORD: {kw}",
                "",
                "STRUCTURE:",
                structure,
                "",
                "OUTPUT FORMAT — return exactly:",
                "TITLE: [under 70 chars]",
                "SLUG: [lowercase-hyphenated]",
                "META: [under 155 chars]",
                "BODY:",
                "[full HTML — h1/h2/p/ul/table — no html/head/body wrappers]",
            ]
            user_msg = "\n".join(user_lines)

            async with _httpx.AsyncClient(timeout=90) as c:
                r = await c.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": ANTHROPIC_KEY,
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": "claude-sonnet-4-6", "max_tokens": 6000,
                          "system": system_prompt,
                          "messages": [{"role": "user", "content": user_msg}]},
                )
            raw = r.json()["content"][0]["text"].strip() if r.status_code == 200 else ""

            def _ex(pat, text, default=""):
                m = _re2.search(pat, text, _re2.MULTILINE | _re2.DOTALL)
                return m.group(1).strip() if m else default

            body_sec = _re2.sub(r"^.*?^BODY:\s*\n?", "", raw, count=1,
                                flags=_re2.DOTALL | _re2.MULTILINE).strip()
            body_html = _re2.sub(r"^```[a-z]*\n?|```$", "", body_sec,
                                 flags=_re2.MULTILINE).strip()
            article_data = {
                "title": _ex(r"^TITLE:\s*(.+?)$", raw, topic[:255]),
                "slug": _ex(r"^SLUG:\s*(.+?)$", raw,
                             kw.lower().replace(" ", "-")[:80]),
                "body_html": body_html,
                "meta_description": _ex(r"^META:\s*(.+?)$", raw, "")[:160],
                "cdm_request_id": None,
                "word_count": len(_re2.sub(r"<[^>]+>", " ", body_html).split()),
            }

        # ── Persist ─────────────────────────────────────────────────────────
        if article_data and article_data.get("body_html"):
            db.execute(sqlt(
                "UPDATE ai_generated_articles SET "
                "status='DRAFT', title=:title, slug=:slug, body_html=:body, "
                "meta_description=:meta, primary_keyword=:kw, "
                "cdm_request_id=:cdm_rid, word_count=:wc, "
                "source=:src, content_type=:ct, completed_at=NOW() "
                "WHERE id=:id"
            ), {
                "title": article_data["title"],
                "slug": article_data["slug"],
                "body": article_data["body_html"],
                "meta": article_data["meta_description"],
                "kw": kw,
                "cdm_rid": article_data.get("cdm_request_id"),
                "wc": article_data.get("word_count"),
                "src": source,
                "ct": content_type,
                "id": job_id,
            })
            db.execute(sqlt(
                "UPDATE seo_topic_discoveries SET is_processed_to_article=TRUE "
                "WHERE id=(SELECT discovery_id FROM ai_generated_articles WHERE id=:id)"
            ), {"id": job_id})
            db.commit()
            log.info(f"Job {job_id} DONE via {source}: {article_data['title'][:60]}")
        else:
            raise ValueError("Empty body from CDM and local fallback")

    except Exception as e:
        log.error(f"Job {job_id} FAILED: {e}")
        try:
            db.execute(sqlt(
                "UPDATE ai_generated_articles SET status='FAILED', "
                "error_message=:err WHERE id=:id"
            ), {"err": str(e)[:500], "id": job_id})
            db.commit()
        except Exception:
            pass
    finally:
        db.close()


class _DeployReq(BaseModel):
    slug: str = ""

@router.post("/deploy")
async def deploy_article_static(payload: _DeployReq, bg: BackgroundTasks,
                                 x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    bg.add_task(_run_static_deploy)
    return {"status": "deploying"}

async def _run_static_deploy():
    import subprocess as _sp, os as _os
    try:
        script = "/home/ec2-user/deploy_blog.py"
        if _os.path.exists(script):
            result = _sp.run(["python3", script], capture_output=True, timeout=180)
            log.info(f"Blog deploy: {result.stdout.decode()[-200:]}")
        # CloudFront invalidation for all blog paths
        _sp.run([
            "aws", "cloudfront", "create-invalidation",
            "--distribution-id", "EDLQAZ1IS2WIG",
            "--paths", "/blog/*", "/blog/",
            "--region", "us-east-1"
        ], capture_output=True, timeout=30)
        log.info("Blog CF invalidation triggered")
    except Exception as e:
        log.warning(f"Static deploy error: {e}")


# ── CDM Review Integration ────────────────────────────────────────────────────

class CDMReviewRequest(BaseModel):
    job_id: int

@router.post("/review/{job_id}")
async def review_article_via_cdm(job_id: int, x_admin_key: str = Header(...)):
    """Submit article to CDM for 0-100 quality scoring."""
    import httpx as _hx
    _require_admin(x_admin_key)
    db = _db()
    try:
        row = db.execute(sqlt("""
            SELECT title, body_html, meta_description, slug, primary_keyword, content_type, verified_complete
            FROM ai_generated_articles WHERE id = :id
        """), {"id": job_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Article not found")
        d = dict(row._mapping)

        # Strip HTML tags for CDM review (CDM expects markdown/text)
        import re as _re
        # Extract Quick Answer block before stripping HTML so CDM can find it
        _raw_html = d.get('body_html') or ''
        _qa_match = _re.search(r'(?:blockquote|div)[^>]*>.*?Quick\s*Answer[:\s]*(.*?)(?:</blockquote>|</div>|<h[1-6])', _raw_html, _re.I | _re.S)
        _qa_text = _re.sub(r'<[^>]+>', ' ', _qa_match.group(1)).strip() if _qa_match else ''
        _qa_words = len(_qa_text.split()) if _qa_text else 0
        body_text = _re.sub(r'<[^>]+>', ' ', _raw_html)
        from app.link_extractor import extract_internal_links, build_link_section
        _found_links = extract_internal_links(_raw_html)
        body_text = _re.sub(r'\s+', ' ', body_text).strip()
        # Prepend structured Quick Answer marker so CDM scoring can find it
        if _qa_text:
            body_text = f'## Quick Answer ({_qa_words} words)' + chr(10) + _qa_text + chr(10) + chr(10) + body_text
            body_text += build_link_section(_found_links)

        cdm_payload = {
            "brand_id": "nexabuilder",
            "domain": "nexabuilder.com",
            "content_type": "comparison_article",
            "title": d.get("title") or "",
            "slug": d.get("slug") or "",
            "body_markdown": body_text[:12000],
            "meta_description": d.get("meta_description") or "",
            "target_query": d.get("primary_keyword") or "",
            "language": "en",
            "verified_complete": bool(d.get("verified_complete") or False),
        }

        async with _hx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://api.techcial.com/v1/content/submit",
                headers={"x-api-key": "24dejulio_internal", "content-type": "application/json"},
                json=cdm_payload
            )

        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"CDM error: {resp.status_code}")

        result = resp.json()
        overall_score = result.get("overall_score", 0)
        passed = result.get("passed", False)
        notes = result.get("notes", "")

        # Store review notes back on the article
        db.execute(sqlt("""
            UPDATE ai_generated_articles
            SET review_notes = :notes, last_review_score = :score, last_review_at = NOW()
            WHERE id = :id
        """), {"notes": notes, "score": overall_score, "id": job_id})
        db.commit()

        return {
            "job_id": job_id,
            "overall_score": overall_score,
            "passed": passed,
            "recommendation": result.get("recommendation"),
            "scores": result.get("scores", {}),
            "notes": notes,
            "cdm_request_id": result.get("request_id"),
            "source": "cdm"
        }
    finally:
        db.close()


@router.get("/cdm-metrics")
async def get_cdm_metrics(x_admin_key: str = Header(...)):
    """Fetch NexaBuilder metrics from CDM."""
    import httpx as _hx
    _require_admin(x_admin_key)
    try:
        async with _hx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.techcial.com/v1/metrics/nexabuilder",
                headers={"x-api-key": "24dejulio_internal"}
            )
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    raise HTTPException(status_code=502, detail="CDM unreachable")

@router.get("/articles")
async def list_generated_articles(x_admin_key: str = Header(...)):
    """List all generated articles with their current status."""
    _require_admin(x_admin_key)
    db = _db()
    try:
        rows = db.execute(sqlt("""
            SELECT id, discovery_id, writing_profile_id, blog_article_id,
                   title, slug, primary_keyword, status,
                   generation_tokens, created_at, completed_at,
                   LEFT(meta_description, 160) AS meta_description,
                   CASE WHEN body_html IS NOT NULL THEN true ELSE false END AS has_body,
                   LEFT(body_html, 2000) AS body_preview
            FROM ai_generated_articles
            WHERE content_type != 'service_page' AND (source IS NULL OR source != 'page_generator')
            ORDER BY created_at DESC
            LIMIT 100
        """)).fetchall()
        return {"articles": [dict(r._mapping) for r in rows]}
    finally:
        db.close()

class ImportTopicRequest(BaseModel):
    discovered_query: str
    seed_keyword: str
    intent_category: str = "QUESTION"
    topic_type: str = "article"
    aeo_angle: str = ""
    vertical: str = ""

@router.post("/import-topic")
async def import_topic(payload: ImportTopicRequest, x_admin_key: str = Header(...)):
    """Import a topic from keyword research into seo_topic_discoveries."""
    _require_admin(x_admin_key)
    db = _db()
    try:
        existing = db.execute(sqlt(
            "SELECT id FROM seo_topic_discoveries "
            "WHERE tenant_id='nexabuilder' AND discovered_query=:q"
        ), {"q": payload.discovered_query}).fetchone()
        if existing:
            return {"success": True, "topic_id": existing[0], "duplicate": True,
                    "message": "Topic already in queue"}
        row = db.execute(sqlt("""
            INSERT INTO seo_topic_discoveries
              (tenant_id, seed_keyword, discovered_query, intent_category,
               impressions, clicks, avg_position, source)
            VALUES ('nexabuilder', :seed, :q, :intent, 0, 0, 0, :src)
            RETURNING id
        """), {
            "seed": payload.seed_keyword,
            "q":    payload.discovered_query,
            "intent": payload.intent_category,
            "src":  "keyword_research:" + payload.topic_type,
        }).fetchone()
        db.commit()
        return {"success": True, "topic_id": row[0], "duplicate": False,
                "message": "Added to SEO topic queue"}
    finally:
        db.close()

class CreateAEOPageRequest(BaseModel):
    question: str
    aeo_angle: str
    seed_keyword: str
    vertical: str = "pool"
    site_id: str = "nexabuilder"
    language: str = "en"

@router.post("/create-aeo-page")
async def create_aeo_page(payload: CreateAEOPageRequest, x_admin_key: str = Header(...)):
    """Generate a short-form AEO answer page from a PAA question + AEO angle."""
    _require_admin(x_admin_key)
    import anthropic as _ant, re as _re

    slug = _re.sub(r"[^a-z0-9]+", "-",
        payload.question.lower()[:80]).strip("-")

    system_prompt = (
        "You are a Southern California home improvement content specialist. "
        "Write ultra-concise AEO pages designed to win Google featured snippets and voice search. "
        "Format: H1 question, then EXACTLY the provided answer as the first paragraph (do not alter it), "
        "then 2-3 short supporting paragraphs (150 words total), then a brief CTA. "
        "Include 2-3 internal links to /services/, /locations/los-angeles/, or /get-quote/. "
        "Cite CSLB requirements where relevant. Active voice, Grade 8 reading level."
    )
    user_prompt = (
        "Create an AEO page.\n"
        "Question (use as H1): " + payload.question + "\n"
        "Opening answer (use word-for-word as first paragraph): " + payload.aeo_angle + "\n"
        "Vertical: " + payload.vertical + "\n"
        "Target keyword: " + payload.seed_keyword + "\n\n"
        "Return ONLY the HTML body content (h1, p, ul tags). No html/head/body wrappers."
    )

    client = _ant.AsyncAnthropic()
    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=900,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    )
    body_html = msg.content[0].text.strip()
    meta_desc = payload.aeo_angle[:157] + "..." if len(payload.aeo_angle) > 157 else payload.aeo_angle

    db = _db()
    try:
        row = db.execute(sqlt("""
            INSERT INTO ai_generated_articles
              (title, slug, body_html, meta_description, primary_keyword,
               status, generation_tokens, writing_profile_id)
            VALUES (:title, :slug, :body, :meta, :kw, 'DRAFT', :tokens, 1)
            RETURNING id
        """), {
            "title":  payload.question,
            "slug":   slug,
            "body":   body_html,
            "meta":   meta_desc,
            "kw":     payload.seed_keyword,
            "tokens": msg.usage.input_tokens + msg.usage.output_tokens,
        }).fetchone()
        db.commit()
        article_id = row[0]
        # Mark as processed in discoveries
        db.execute(sqlt("""
            INSERT INTO seo_topic_discoveries
              (tenant_id, seed_keyword, discovered_query, intent_category,
               impressions, clicks, avg_position, source, is_processed_to_article)
            VALUES ('nexabuilder', :seed, :q, 'QUESTION', 0, 0, 0, 'aeo_page', true)
            ON CONFLICT (tenant_id, discovered_query) DO UPDATE SET is_processed_to_article=true
        """), {"seed": payload.seed_keyword, "q": payload.question})
        db.commit()
        return {
            "success":    True,
            "article_id": article_id,
            "slug":       slug,
            "title":      payload.question,
        }
    finally:
        db.close()

class ArticleUpdate(BaseModel):
    title:            str = None
    slug:             str = None
    body_html:        str = None
    meta_description: str = None

class StatusUpdate(BaseModel):
    status: str

class AutoFixRequest(BaseModel):
    cdm_notes: str = ""
    cdm_score: int = 0


@router.get("/articles/{article_id}")
async def get_article_by_id(article_id: int, x_admin_key: str = Header(...)):
    """Fetch a single generated article by ID including full body_html."""
    _require_admin(x_admin_key)
    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT id, discovery_id, writing_profile_id, blog_article_id, "
            "title, slug, primary_keyword, status, meta_description, "
            "body_html, json_ld_schema, generation_tokens, quality_score, "
            "cdm_request_id, content_type, word_count, source, "
            "created_at, completed_at, error_message "
            "FROM ai_generated_articles WHERE id=:id"
        ), {"id": article_id}).fetchone()
        if not row:
            raise HTTPException(404, "Article not found")
        return dict(row._mapping)
    finally:
        db.close()

@router.put("/articles/{article_id}")
async def update_article(article_id: int, payload: ArticleUpdate,
                         x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    db = _db()
    try:
        db.execute(sqlt(
            "UPDATE ai_generated_articles SET "
            "title=COALESCE(:title,title), slug=COALESCE(:slug,slug), "
            "body_html=COALESCE(:body,body_html), "
            "meta_description=COALESCE(:meta,meta_description) "
            "WHERE id=:id"
        ), {"title":payload.title,"slug":payload.slug,
            "body":payload.body_html,"meta":payload.meta_description,"id":article_id})
        db.commit()
        return {"success":True,"article_id":article_id}
    finally:
        db.close()

@router.patch("/articles/{article_id}/status")
async def update_article_status(article_id: int, payload: StatusUpdate,
                                 x_admin_key: str = Header(...),
                                 bg: BackgroundTasks = None):
    _require_admin(x_admin_key)
    allowed = {"DRAFT","REVIEW","PUBLISHED","FAILED"}
    if payload.status not in allowed:
        raise HTTPException(422, f"Status must be one of: {allowed}")
    db = _db()
    try:
        db.execute(sqlt("UPDATE ai_generated_articles SET status=:s WHERE id=:id"),
                   {"s":payload.status,"id":article_id})
        if payload.status == "PUBLISHED":
            db.execute(sqlt("UPDATE ai_generated_articles SET published_at=NOW() WHERE id=:id AND published_at IS NULL"),
                       {"id":article_id})
            # Editorial linter
            try:
                import subprocess as _l_sp, tempfile as _l_tf, os as _l_os
                with _l_tf.NamedTemporaryFile(mode='w',suffix='.html',delete=False) as _lf:
                    _lbody=db.execute(sqlt('SELECT body_html FROM ai_generated_articles WHERE id=:id'),{'id':article_id}).fetchone()
                    _lf.write(_lbody.body_html if _lbody and _lbody.body_html else '')
                    _ltmp=_lf.name
                _lr=_l_sp.run(['/var/www/nexabuilder/backend/current/venv/bin/python','/var/www/nexabuilder/editorial/validate_editorial.py',_ltmp,'--inventory','/var/www/nexabuilder/editorial/editorial-inventory.json'],capture_output=True,text=True,timeout=10)
                log.info(f'Linter [{article_id}] rc={_lr.returncode}: {_lr.stdout.strip()[:200]}')
                _l_os.unlink(_ltmp)
            except Exception as _le: log.warning(f'Linter error: {_le}')
        db.commit()
        if payload.status == "PUBLISHED" and bg:
            bg.add_task(_run_static_deploy)
        return {"success":True,"article_id":article_id,"status":payload.status}
    finally:
        db.close()

@router.post("/autofix/{article_id}")
async def autofix_article(article_id: int, payload: AutoFixRequest,
                           x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    import anthropic as _ant, re as _re
    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT title,body_html,meta_description,primary_keyword "
            "FROM ai_generated_articles WHERE id=:id"
        ),{"id":article_id}).fetchone()
        if not row: raise HTTPException(404,"Article not found")
        d = dict(row._mapping)
    finally:
        db.close()
    body_text = _re.sub(r"<[^>]+>"," ",d.get("body_html") or "")
    nl = chr(10)
    link_list = (
        "[verify CSLB license](/guides/verify-cslb-license), "
        "[get free quotes](/get-quote/), "
        "[Los Angeles contractors](/locations/los-angeles/), "
        "[pool installation](/services/pool-installation/), "
        "[roofing contractors](/services/roofing-contractors/)"
    )
    fix_prompt = (
        "You are a Southern California contractor content editor." + nl
        + "Rewrite the article to fix the CDM review issues. Keep all facts." + nl
        + "Add 3-5 of these internal links: " + link_list + nl
        + "Structure: H1, Quick Answer box (40-60w), H2 sections, FAQ (4 Q&As), CTA." + nl
        + "Return ONLY full HTML body. No html/head/body wrappers." + nl
        + "CDM SCORE: " + str(payload.cdm_score) + "/100" + nl
        + "CDM NOTES:" + nl + payload.cdm_notes[:1200] + nl
        + "TITLE: " + (d.get("title") or "") + nl
        + "KEYWORD: " + (d.get("primary_keyword") or "") + nl
        + "CURRENT BODY:" + nl + body_text[:2500]
    )
    client = _ant.AsyncAnthropic()
    msg = await client.messages.create(
        model="claude-sonnet-4-6", max_tokens=4000,
        messages=[{"role":"user","content":fix_prompt}]
    )
    new_body = msg.content[0].text.strip()
    db2 = _db()
    try:
        db2.execute(sqlt(
            "UPDATE ai_generated_articles SET body_html=:body,status='DRAFT' WHERE id=:id"
        ),{"body":new_body,"id":article_id})
        db2.commit()
    finally:
        db2.close()
    return {"success":True,"article_id":article_id,"body_html":new_body,
            "tokens":msg.usage.input_tokens+msg.usage.output_tokens}

@router.get("/check-duplicate")
async def check_duplicate_proxy(title: str, brand_id: str = "nexabuilder",
                                  x_admin_key: str = Header(...)):
    """Proxy to CDM duplicate detection before article generation."""
    _require_admin(x_admin_key)
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"https://api.techcial.com/v1/content/check-duplicate"
                f"?title={title}&brand_id={brand_id}",
                headers={"x-api-key": "24dejulio_internal"}
            )
        if r.status_code == 200:
            return r.json()
        return {"is_duplicate": False, "similarity_score": 0, "action": "ALLOW", "closest_matches": []}
    except Exception:
        return {"is_duplicate": False, "similarity_score": 0, "action": "ALLOW", "closest_matches": []}

@router.post("/suggest-meta/{article_id}")
async def suggest_meta(article_id: int, x_admin_key: str = Header(...)):
    """Use Claude to suggest SEO title, meta description, and snippet."""
    import anthropic as _ant, re as _re
    _require_admin(x_admin_key)
    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT title, body_html, meta_description, primary_keyword, slug "
            "FROM ai_generated_articles WHERE id=:id"
        ), {"id": article_id}).fetchone()
        if not row:
            raise HTTPException(404, "Article not found")
        d = dict(row._mapping)
        body_text = _re.sub(r"<[^>]+>", " ", d.get("body_html") or "")
        body_text = _re.sub(r"\s+", " ", body_text).strip()[:3000]
        existing = db.execute(sqlt(
            "SELECT meta_description FROM ai_generated_articles "
            "WHERE status=\'PUBLISHED\' AND id!=:id AND meta_description IS NOT NULL"
        ), {"id": article_id}).fetchall()
        existing_list = chr(10).join([r[0][:80] for r in existing if r[0]])
        nl = chr(10)
        prompt = (
            "You are an SEO editor for NexaBuilder.com, a Southern California licensed contractor platform." + nl
            + "Primary keyword: " + (d.get("primary_keyword") or "") + nl
            + "Current title: " + (d.get("title") or "") + nl
            + "Body excerpt: " + body_text[:800] + nl
            + "Existing published metas (must NOT duplicate):" + nl + existing_list + nl + nl
            + "Write exactly 3 lines:" + nl
            + "TITLE: (50-60 chars, include keyword, targets SoCal homeowners)" + nl
            + "META: (140-155 chars, direct benefit, no fluff, unique from list)" + nl
            + "SNIPPET: (40-60 words, direct answer to the homeowner question)"
        )
        client = _ant.AsyncAnthropic()
        msg = await client.messages.create(model="claude-sonnet-4-6", max_tokens=300,
            messages=[{"role": "user", "content": prompt}])
        text = msg.content[0].text.strip()
        title = meta = snippet = ""
        for line in text.split(nl):
            if line.startswith("TITLE:"): title = line[6:].strip()
            elif line.startswith("META:"): meta = line[5:].strip()
            elif line.startswith("SNIPPET:"): snippet = line[8:].strip()
        return {
            "article_id": article_id,
            "suggested_title": title,
            "suggested_meta": meta,
            "suggested_snippet": snippet,
            "char_counts": {"title": len(title), "meta": len(meta), "snippet_words": len(snippet.split())}
        }
    finally:
        db.close()

@router.post("/apply-suggestions/{article_id}")
async def apply_suggestions(article_id: int, x_admin_key: str = Header(...)):
    """Apply targeted fixes from CDM review notes. Patches body, not a full rewrite."""
    import anthropic as _ant, re as _re2
    _require_admin(x_admin_key)
    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT title, body_html, primary_keyword, review_notes, last_review_score "
            "FROM ai_generated_articles WHERE id=:id"
        ), {"id": article_id}).fetchone()
        if not row:
            raise HTTPException(404, "Article not found")
        d = dict(row._mapping)
    finally:
        db.close()
    review_notes = d.get("review_notes") or ""
    score = d.get("last_review_score") or 0
    if not review_notes:
        raise HTTPException(400, "No review notes found. Run AI Review first.")
    body_html = d.get("body_html") or ""
    nl = chr(10)
    # Detect content_type to use correct link strategy
    content_type = d.get("content_type", "comparison_article")
    is_service_page = content_type == "service_page" or d.get("source") == "page_generator"
    if is_service_page:
        # Service pages: link to sibling services + location pages + CSLB guide
        # Do NOT link to blog/informational content — keeps users in conversion flow
        link_list = ("[verify any contractor license at CSLB.ca.gov](/guides/verify-cslb-license/), "
            "[get free contractor quotes](/get-quote/), "
            "[all Southern California services](/services/), "
            "[licensed pool contractors](/services/pool-installation/), "
            "[licensed roofing contractors](/services/roofing/), "
            "[licensed electricians](/services/electrical/), "
            "[licensed plumbers](/services/plumbing/), "
            "[HVAC contractors](/services/hvac-contractors/), "
            "[landscaping contractors](/services/landscaping/), "
            "[Los Angeles contractors](/locations/los-angeles/), "
            "[Orange County contractors](/locations/orange-county/), "
            "[San Diego contractors](/locations/san-diego/)")
    else:
        link_list = ("[verify CSLB license](/guides/verify-cslb-license/), "
            "[get free contractor quotes](/get-quote/), "
            "[pool installation](/services/pool-installation/), "
            "[licensed roofing contractors](/services/roofing/), "
            "[licensed electricians](/services/electrical/), "
            "[Los Angeles home improvement](/locations/los-angeles/), "
            "[Orange County contractors](/locations/orange-county/), "
            "[home remodeling contractors](/services/home-remodeling/), "
            "[San Diego contractors](/locations/san-diego/)")
    # Deterministic Quick Answer word-count fix BEFORE calling Claude
    _qa_re = _re2.compile(
        r'(<(?:blockquote|div)[^>]*>\s*<p[^>]*>\s*<strong>Quick\s*Answer:\s*</strong>)(.*?)(</p>\s*</(?:blockquote|div)>)',
        _re2.I | _re2.S
    )
    _qa_m = _qa_re.search(body_html)
    if _qa_m and ("quick answer word" in review_notes.lower() or "word count" in review_notes.lower()):
        _qa_inner = _re2.sub(r"<[^>]+>", " ", _qa_m.group(2)).strip()
        _qa_words = _qa_inner.split()
        if len(_qa_words) > 60:
            _qa_trimmed = " ".join(_qa_words[:55])
            body_html = _qa_re.sub(
                lambda m: m.group(1) + " " + _qa_trimmed + m.group(3),
                body_html, count=1
            )
        elif len(_qa_words) < 40:
            pass  # Claude will expand it per the review notes
    page_type_note = ("This is a SERVICE/CONVERSION page — users are ready to hire a contractor. ""Link ONLY to sibling service pages, location pages, or the CSLB verification guide. ""Do NOT link to blog articles or informational content. ""3 well-placed links is optimal for a conversion page. ""Do not add unnecessary links just to hit a count." if is_service_page else "")
    prompt = ("You are a Southern California home improvement content editor working on NexaBuilder.com." + nl + page_type_note + nl + "PATCH the article below based on the CDM review notes. Make ONLY the specific fixes noted." + nl+ "Preserve all existing content, structure, voice, and facts not mentioned in the notes." + nl+ "If the article is contractor-facing, keep that voice — do not convert it to homeowner-facing." + nl+ "Fix each bullet point in the review notes exactly as specified. Do not add unrequested changes." + nl + "Add 3 targeted internal links: " + link_list + nl + nl + "CURRENT CDM SCORE: " + str(score) + "/100" + nl + "REVIEW NOTES (address each point):" + nl + review_notes[:4000] + nl + nl + "TITLE: " + (d.get("title") or "") + nl + "KEYWORD: " + (d.get("primary_keyword") or "") + nl + nl + "CURRENT ARTICLE HTML:" + nl + body_html[:12000] + nl + nl + "Return ONLY the patched full HTML body. No markdown fences. No html/head/body wrappers.")
    client = _ant.AsyncAnthropic()
    msg = await client.messages.create(model="claude-sonnet-4-6", max_tokens=6000, messages=[{"role": "user", "content": prompt}])
    new_body = msg.content[0].text.strip()
    new_body = _re2.sub(r"^```[a-z]*\n?|```$", "", new_body, flags=_re2.IGNORECASE|_re2.MULTILINE).strip()
    db2 = _db()
    try:
        db2.execute(sqlt("UPDATE ai_generated_articles SET body_html=:body WHERE id=:id"), {"body": new_body, "id": article_id})
        db2.commit()
    finally:
        db2.close()
    return {"success": True, "article_id": article_id, "body_html": new_body, "mode": "patch", "tokens": msg.usage.input_tokens + msg.usage.output_tokens}

@router.post("/verify-complete/{article_id}")
async def verify_complete(article_id: int, x_admin_key: str = Header(...)):
    """Mark article as verified complete."""
    _require_admin(x_admin_key)
    db = _db()
    try:
        result = db.execute(sqlt(
            "UPDATE ai_generated_articles SET verified_complete=TRUE WHERE id=:id RETURNING id"
        ), {"id": article_id}).fetchone()
        if not result: raise HTTPException(404, "Article not found")
        db.commit()
        return {"success": True, "article_id": article_id, "verified_complete": True}
    finally:
        db.close()

@router.patch('/articles/{article_id}/meta')
def save_article_meta(article_id: int, payload: dict, x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    db = _db()
    try:
        fields = {}
        if 'title' in payload: fields['title'] = payload['title']
        if 'meta_title' in payload: fields['meta_title'] = payload['meta_title']
        if 'meta_description' in payload: fields['meta_description'] = payload['meta_description']
        if not fields: raise HTTPException(400, 'No fields to update')
        set_clause = ', '.join([f"{k}=:{k}" for k in fields])
        fields['id'] = article_id
        db.execute(sqlt(f'UPDATE ai_generated_articles SET {set_clause} WHERE id=:id'), fields)
        db.commit()
        return {'success': True, 'article_id': article_id, 'updated': list(fields.keys())}
    finally:
        db.close()

@router.get('/cslb-lookup')
def cslb_lookup(license_no: str = None, business_name: str = None, city: str = None):
    """Public CSLB license lookup from NexaBuilder contractor DB."""
    if not license_no and not business_name:
        raise HTTPException(400, 'Provide license_no or business_name')
    db = _db()
    try:
        conds = ['1=1']
        params = {}
        if license_no:
            conds.append('UPPER(license_no) = :lic')
            params['lic'] = license_no.strip().upper()
        if business_name:
            conds.append('UPPER(business_name) LIKE :biz')
            params['biz'] = '%' + business_name.strip().upper() + '%'
        if city:
            conds.append('UPPER(city) LIKE :city')
            params['city'] = '%' + city.strip().upper() + '%'
        where = ' AND '.join(conds)
        rows = db.execute(sqlt(
            f'SELECT license_no, business_name, primary_status, secondary_status, '
            f'classifications, city, state, zip_code, expiration_date, '
            f'bond_amount, bond_company, issue_date '
            f'FROM contractors WHERE {where} LIMIT 10'
        ), params).fetchall()
        results = []
        for r in rows:
            d = dict(r._mapping)
            for k in ['expiration_date', 'issue_date']:
                if d.get(k): d[k] = str(d[k])
            results.append(d)
        return {
            'results': results,
            'count': len(results),
            'source': 'NexaBuilder contractor database (CSLB-sourced). Verify at CSLB.ca.gov.',
            'cslb_url': 'https://www.cslb.ca.gov/OnlineServices/CheckLicenseII/CheckLicense.aspx'
        }
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Service / Location Page Generator
# Generates dedicated landing pages for CSLB license class + geo queries
# e.g. "c-10 electrical contractors southern california"
# ─────────────────────────────────────────────────────────────────────────────

class ServicePageRequest(BaseModel):
    query:           str           # GSC query e.g. "c-10 electrical contractors southern california"
    page_type:       str = "service_license"  # service_license | location | vertical
    vertical:        str = "general"
    ai_context:      str = ""      # SEO Intelligence output — Quick Wins + Content Gaps
    cslb_license:    str = ""      # e.g. "C-10"
    license_name:    str = ""      # e.g. "Electrical"
    city:            str = ""      # if location page
    impressions:     int = 0
    avg_position:    float = 0

@router.post("/generate-service-page")
async def generate_service_page(
    payload: ServicePageRequest,
    x_admin_key: str = Header(...)
):
    """Generate a service or location page optimized for CSLB license + geo queries."""
    _require_admin(x_admin_key)
    import anthropic as _ant, re as _re

    slug = _re.sub(r"[^a-z0-9]+", "-",
        payload.query.lower()[:80]).strip("-")

    # Build system prompt based on page type
    system_prompt = (
        "You are a Southern California contractor matching platform content specialist. "
        "Write service landing pages for NexaBuilder.com that rank for CSLB license class + "
        "geographic queries. Pages must: (1) immediately signal CSLB verification credibility, "
        "(2) list service areas with city-level specificity, (3) explain what the license class "
        "covers in plain language, (4) answer the top homeowner questions about hiring this trade, "
        "(5) include a strong CTA to /get-quote/. "
        "Tone: confident, direct, SoCal-specific. Grade 8 reading level. Active voice. "
        "Return ONLY the HTML body content — h1, h2, p, ul, table tags. No wrappers."
    )

    # Build context from SEO Intelligence if available
    ai_context_block = ""
    if payload.ai_context:
        ai_context_block = f"""
SEO Intelligence context (use to inform content decisions):
{payload.ai_context[:1500]}
"""

    license_block = ""
    if payload.cslb_license:
        license_block = f"""
CSLB License Class: {payload.cslb_license} ({payload.license_name})
Include: what work this license covers, why CSLB verification matters, 
how NexaBuilder pre-verifies all contractors.
"""

    city_block = f"Primary city/region: {payload.city}" if payload.city else         "Geographic scope: Southern California (LA County, Orange County, San Diego County, Riverside County, San Bernardino County)"

    user_prompt = f"""Generate a service landing page.

Target query: {payload.query}
Vertical: {payload.vertical}
{city_block}
{license_block}
Current GSC position: {payload.avg_position:.1f} with {payload.impressions} impressions, 0 clicks.
Goal: earn the click from searchers who already know what this license class means.
{ai_context_block}

Page structure required:
1. H1 — include the license class verbatim, geographic area, and CSLB verification signal
2. Opening paragraph — 40-60 word direct answer matching search intent exactly
3. H2: "What {payload.cslb_license or payload.vertical.title()} Contractors Do" — scope of work, 3-4 bullet points
4. H2: "Southern California Service Areas" — specific counties and 6-8 cities
5. H2: "How NexaBuilder Verifies {payload.cslb_license or ''} Contractors" — CSLB check, insurance, background
6. H2: "Common Questions" — 3 Q&A pairs matching searcher intent
7. CTA section — link to /get-quote/ with benefit-focused copy

Internal links: include 2-3 links to /services/, /guides/verify-cslb-license/, /get-quote/
Word count target: 600-800 words.
Return ONLY the HTML body content."""

    client = _ant.AsyncAnthropic()
    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    )
    body_html = msg.content[0].text.strip()

    # Build title and meta
    title = payload.query.title()
    if payload.cslb_license and payload.cslb_license.upper() not in title.upper():
        title = f"{payload.cslb_license} {payload.license_name} Contractors Southern California"
    meta_desc = (
        f"Find CSLB-verified {payload.cslb_license} {payload.license_name} contractors "
        f"in Southern California. Licensed, background-checked, free quotes."
    )[:157]

    db = _db()
    try:
        row = db.execute(sqlt("""
            INSERT INTO ai_generated_articles
              (title, slug, body_html, meta_description, primary_keyword,
               status, generation_tokens, writing_profile_id, content_type, source)
            VALUES (:title, :slug, :body, :meta, :kw, 'DRAFT', :tokens, 1, 'service_page', 'page_generator')
            RETURNING id
        """), {
            "title":  title[:255],
            "slug":   slug,
            "body":   body_html,
            "meta":   meta_desc,
            "kw":     payload.query,
            "tokens": msg.usage.input_tokens + msg.usage.output_tokens,
        }).fetchone()
        db.commit()
        page_id = row[0]

        # Mark in topic discoveries if it came from there
        db.execute(sqlt("""
            INSERT INTO seo_topic_discoveries
              (tenant_id, seed_keyword, discovered_query, intent_category,
               impressions, clicks, avg_position, source, is_processed_to_article)
            VALUES ('nexabuilder', :seed, :q, 'service_page',
                    :imp, 0, :pos, 'page_generator', true)
            ON CONFLICT (tenant_id, discovered_query)
            DO UPDATE SET is_processed_to_article=true
        """), {
            "seed": (payload.cslb_license or payload.query.split()[0]),
            "q":    payload.query,
            "imp":  payload.impressions,
            "pos":  payload.avg_position,
        })
        db.commit()

        return {
            "success": True,
            "page_id": page_id,
            "slug":    slug,
            "title":   title,
            "word_count": len(_re.sub(r"<[^>]+>", " ", body_html).split()),
        }
    finally:
        db.close()


# ── Service Pages List ─────────────────────────────────────────────────────────
@router.get("/pages")
async def list_service_pages(x_admin_key: str = Header(...)):
    """List all service pages (content_type=service_page) with status and deploy info."""
    _require_admin(x_admin_key)
    db = _db()
    try:
        rows = db.execute(sqlt("""
            SELECT id, title, slug, primary_keyword, status,
                   content_type, source, generation_tokens,
                   created_at, completed_at, published_at,
                   LEFT(meta_description, 160) AS meta_description,
                   last_review_score, verified_complete,
                   CASE WHEN body_html IS NOT NULL THEN true ELSE false END AS has_body
            FROM ai_generated_articles
            WHERE content_type = 'service_page' OR source = 'page_generator'
            ORDER BY created_at DESC
        """)).fetchall()
        return {"pages": [dict(r._mapping) for r in rows], "total": len(rows)}
    finally:
        db.close()



# ── Service Page AI Chat ───────────────────────────────────────────────────────
from pydantic import BaseModel as _BM

class ChatRequest(_BM):
    question: str
    cslb_license: str = ""
    license_name: str = ""
    vertical: str = "general"
    page_path: str = ""

@router.post("/ai-chat/service-page")
async def service_page_chat(payload: ChatRequest, x_admin_key: str = Header(None)):
    """Answer homeowner questions about hiring contractors — powers the on-page AI chat widget."""
    import anthropic as _ant
    nl = "\n"
    system = (
        "You are a knowledgeable home improvement advisor for NexaBuilder.com, "
        "Southern California\'s CSLB-verified contractor matching platform. "
        f"The homeowner is on a page about {payload.cslb_license} ({payload.license_name}) contractors. "
        "Answer concisely (2-4 sentences max) in a helpful, direct tone. "
        "CRITICAL: Plain prose only — no markdown, no **bold**, no [links](url), no bullet points. " "Never send users to external sites including cslb.ca.gov — NexaBuilder pre-verifies every license so users never need to check manually. " "Keep answers to 2-3 sentences. End with a natural nudge to get a free quote on NexaBuilder. "
        "If asked about cost, give SoCal-specific ranges. "
        "If asked about permits, cite the relevant California code or jurisdiction. "
        "If the user is ready to hire, encourage them to get a free quote through NexaBuilder. "
        "Never recommend specific competitors. "
        "Do not make up license numbers or contractor names."
    )
    # Topic guard — only answer construction/NexaBuilder questions
    _ALLOWED_TOPICS = {
        'contractor','license','cslb','permit','cost','price','quote','hire',
        'electrical','plumbing','roofing','pool','hvac','landscaping','remodel',
        'construction','home improvement','adu','inspection','insurance','bond',
        'estimate','project','install','repair','replace','build','nexabuilder',
        'southern california','los angeles','orange county','san diego','socal',
        'c-10','c-27','c-36','c-39','c-53','c-20','verified','background check',
        'general contractor','subcontractor','licensed','unlicensed',
        'kitchen','bathroom','addition','solar','painting','flooring','drywall',
        'foundation','concrete','fence','deck','patio','garage','roof','drain',
        'water heater','panel','wiring','circuit','duct','ac','furnace',
        'irrigation','sod','hardscape','tree','grading','excavation',
    }
    q_lower = payload.question.lower()
    topic_hit = any(kw in q_lower for kw in _ALLOWED_TOPICS)

    if not topic_hit:
        return {
            "answer": (
                f"I can only answer questions about hiring {payload.cslb_license} "
                f"{payload.license_name} contractors in Southern California, "
                "CSLB license verification, project costs, permits, and NexaBuilder services. "
                "What would you like to know about your home improvement project?"
            ),
            "tokens": 0,
            "off_topic": True
        }

    user_msg = (
        f"The homeowner is on nexabuilder.com{payload.page_path} "
        f"looking for {payload.cslb_license} {payload.license_name} contractors in Southern California.{nl}"
        f"Their question: {payload.question}"
    )
    client = _ant.AsyncAnthropic()
    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system=system,
        messages=[{"role": "user", "content": user_msg}]
    )
    answer = msg.content[0].text.strip()
    return {
        "answer": answer,
        "tokens": msg.usage.input_tokens + msg.usage.output_tokens
    }



# ── Service Page One-Click Deploy ─────────────────────────────────────────────
@router.post("/deploy-service-page/{article_id}")
async def deploy_service_page_endpoint(
    article_id: int,
    bg: BackgroundTasks,
    x_admin_key: str = Header(...)
):
    """Trigger deploy of a service page via svc_deploy.py. Runs as background task."""
    _require_admin(x_admin_key)
    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT id, slug, content_type FROM ai_generated_articles WHERE id=:id"
        ), {"id": article_id}).fetchone()
        if not row:
            raise HTTPException(404, "Page not found")
        if row[2] != "service_page":
            raise HTTPException(400, "Not a service page")
    finally:
        db.close()

    async def _run_deploy(aid: int):
        import subprocess as _sp
        result = _sp.run(
            ["python3","-c",
             "import sys; sys.path.insert(0,'venv/lib/python3.10/site-packages'); "
             "exec(open('/home/ec2-user/svc_deploy.py').read()); "
             f"run({aid})"],
            capture_output=True, timeout=120,
            cwd="/var/www/nexabuilder/backend/current")
        log.info(f"Deploy {aid}: {result.stdout.decode()[-200:]} {result.stderr.decode()[-100:]}")

    bg.add_task(_run_deploy, article_id)
    return {
        "status": "deploying",
        "article_id": article_id,
        "message": f"Deploying page {article_id}. Check status in 30-60 seconds."
    }



# -- Page Performance Recovery Engine --
@router.get('/recovery/candidates')
async def get_recovery_candidates(
    min_impressions: int = 30,
    max_ctr: float = 0.02,
    min_position: float = 8.0,
    max_position: float = 70.0,
    x_admin_key: str = Header(...)
):
    _require_admin(x_admin_key)
    db = _db()
    try:
        rows = db.execute(sqlt(
            'SELECT page, SUM(impressions) AS ti, SUM(clicks) AS tc,'
            ' ROUND(AVG(ctr)::numeric*100,2) AS ctr_pct,'
            ' ROUND(AVG(position)::numeric,1) AS pos,'
            ' MAX(synced_at) AS ls,'
            ' array_agg(query ORDER BY impressions DESC)'
            ' FILTER (WHERE query IS NOT NULL) AS qs'
            ' FROM gsc_keywords WHERE page IS NOT NULL AND impressions > 0'
            ' GROUP BY page'
            ' HAVING SUM(impressions)>=:min_imp'
            ' AND ROUND(AVG(ctr)::numeric*100,2)<=:max_ctr'
            ' AND AVG(position) BETWEEN :min_pos AND :max_pos'
            ' ORDER BY SUM(impressions) DESC LIMIT 50'
        ), {'min_imp':min_impressions,'max_ctr':max_ctr*100,'min_pos':min_position,'max_pos':max_position}).fetchall()
        out = []
        for row in rows:
            pg=row[0]; imp=int(row[1]); cl=int(row[2]); ct=float(row[3]); pos=float(row[4]); qs=list(row[6] or [])[:5]
            pri = 'critical' if pos<=15 and ct<1 else ('high' if (pos<=30 and imp>=100) or imp>=200 else 'medium')
            ft = []
            if pos<=20 and ct<1: ft.append('title_meta')
            if pos>30: ft.append('content_depth')
            if pos<=30 and ct<1: ft.append('schema')
            slug = pg.rstrip('/').split('/')[-1]
            da = db.execute(sqlt('SELECT id,title,status,last_review_score FROM ai_generated_articles WHERE slug=:s LIMIT 1'),{'s':slug}).fetchone()
            out.append({'url':pg,'slug':slug,'impressions':imp,'clicks':cl,'ctr':ct,'position':pos,'priority':pri,'fix_types':ft,'top_queries':qs,'in_cms':da is not None,'article_id':da[0] if da else None,'article_title':da[1] if da else None,'cdm_score':da[3] if da else None})
        return {'candidates':out,'total':len(out)}
    finally: db.close()


@router.post('/recovery/review-page')
async def recovery_review_page(payload: dict, x_admin_key: str = Header(...)):
    _require_admin(x_admin_key)
    import anthropic as _ant, re as _re, boto3 as _b3
    page_url=payload.get('url',''); article_id=payload.get('article_id'); top_queries=payload.get('top_queries',[])
    BKT='nexabuilder-root-site-979841141166-us-west-1-an'
    body_html=''; title=''; meta_desc=''
    if article_id:
        db=_db()
        try:
            row=db.execute(sqlt('SELECT title,body_html,meta_description FROM ai_generated_articles WHERE id=:id'),{'id':article_id}).fetchone()
            if row: title,body_html,meta_desc=row[0] or '',row[1] or '',row[2] or ''
        finally: db.close()
    else:
        pth=page_url.replace('https://www.nexabuilder.com','').replace('https://nexabuilder.com','').strip('/')
        s3k=(pth+'/index.html') if pth else 'index.html'
        try:
            s3=_b3.client('s3',region_name='us-west-1')
            ph=s3.get_object(Bucket=BKT,Key=s3k)['Body'].read().decode('utf-8')
            bm=_re.search(r'<(?:main|article)[^>]*>(.*?)</(?:main|article)>',ph,_re.S|_re.I)
            body_html=bm.group(1) if bm else ph
            tm=_re.search(r'<title>(.*?)</title>',ph,_re.S|_re.I)
            title=_re.sub(r'<[^>]+>','',tm.group(1)).strip() if tm else ''
            mm=_re.search(r'name=["\']description["\'][^>]+content=["\']([^"\']{1,300})',ph,_re.I)
            if not mm: mm=_re.search(r'content=["\']([^"\']{1,300})["\'][^>]+name=["\']description',ph,_re.I)
            meta_desc=mm.group(1).strip()[:155] if mm else ''
        except Exception as e: return {'error':str(e),'s3_key':s3k}
    if not body_html: return {'error':'No content found'}
    # Strip nav, footer, scripts, styles before extraction — reduces tokens ~60%
    clean=body_html
    for tag in ['nav','footer','script','style','noscript']:
        clean=_re.sub(rf'<{tag}[^>]*>.*?</{tag}>','',clean,flags=_re.S|_re.I)
    plain=_re.sub(r'<[^>]+',' ',clean)
    plain=_re.sub(r'\s+',' ',plain).strip()[:6000]
    qblock=('GSC queries:\n'+chr(10).join('  - '+q for q in top_queries[:5])) if top_queries else ''
    prompt=('Review this underperforming NexaBuilder page.\n'
        f'PAGE: {page_url}\nTITLE: {title}\nMETA: {meta_desc}\n{qblock}\nCONTENT:\n{plain}\n\n'
        'Output EXACTLY:\nSCORE: [n/100]\nREADER_VALUE: [n/20]\nFACTUAL_ACCURACY: [n/20]\n'
        'SEARCH_INTENT: [n/15]\nEEAT: [n/15]\nSTRUCTURE: [n/10]\nREADABILITY: [n/10]\n'
        'INTERNAL_LINKS: [n/10]\nCTR_DIAGNOSIS: [why no clicks, 1-2 sentences]\n'
        'TITLE_REWRITE: [new title under 60 chars]\nMETA_REWRITE: [new meta under 155 chars]\n'
        'FIXES:\n- [fix1]\n- [fix2]\n- [fix3]\n- [fix4]')
    client=_ant.AsyncAnthropic()
    msg=await client.messages.create(model='claude-sonnet-4-6',max_tokens=1200,
        system='You are a senior SEO editor specializing in CTR recovery. Plain text only, no markdown.',
        messages=[{'role':'user','content':prompt}])
    raw=msg.content[0].text.strip()
    def ext(pat,txt,dflt=''):
        m=_re.search(pat,txt,_re.I|_re.M)
        return m.group(1).strip() if m else dflt
    score=int(ext(r'SCORE:\s*(\d+)',raw) or 0)
    ctr_diag=ext(r'CTR_DIAGNOSIS:\s*(.{1,400})',raw)
    title_rw=ext(r'TITLE_REWRITE:\s*(.{1,200})',raw)
    meta_rw=ext(r'META_REWRITE:\s*(.{1,300})',raw)
    fixes=[l.lstrip('-. ').strip() for l in raw.split('\n') if l.strip().startswith('-') and len(l.strip())>5]
    scores={dim.lower():int(ext(rf'{dim}:\s*(\d+)',raw) or 0) for dim in ['READER_VALUE','FACTUAL_ACCURACY','SEARCH_INTENT','EEAT','STRUCTURE','READABILITY','INTERNAL_LINKS']}
    if article_id and score:
        db=_db()
        try:
            note=f'RECOVERY\nCTR: {ctr_diag}\nTITLE: {title_rw}\nMETA: {meta_rw}\nFIXES:\n'+chr(10).join(f'- {f}' for f in fixes)
            db.execute(sqlt('UPDATE ai_generated_articles SET last_review_score=:s,review_notes=:n WHERE id=:id'),{'s':score,'n':note,'id':article_id})
            db.commit()
        finally: db.close()
    return {'url':page_url,'title':title,'meta':meta_desc,'score':score,'scores':scores,'ctr_diagnosis':ctr_diag,'title_rewrite':title_rw,'meta_rewrite':meta_rw,'fixes':fixes,'tokens':msg.usage.input_tokens+msg.usage.output_tokens}


@router.post('/recovery/apply-ctr-fix/{article_id}')
async def recovery_apply_ctr_fix(article_id:int,payload:dict,x_admin_key:str=Header(...)):
    _require_admin(x_admin_key)
    db=_db()
    try:
        fields={}
        if payload.get('title'): fields['title']=payload['title']
        if payload.get('meta_description'): fields['meta_description']=payload['meta_description']
        if not fields: raise HTTPException(400,'No fields to update')
        set_clause=', '.join(f'{k}=:{k}' for k in fields)
        fields['id']=article_id
        db.execute(sqlt(f'UPDATE ai_generated_articles SET {set_clause} WHERE id=:id'),fields)
        db.commit()
        return {'success':True,'updated':list(fields.keys()),'article_id':article_id}
    finally: db.close()

@router.post('/recovery/apply-s3-fix')
async def recovery_apply_s3_fix(payload: dict, x_admin_key: str = Header(...)):
    # Patch title and meta directly in an S3 static HTML file, then CF invalidate
    _require_admin(x_admin_key)
    import re as _re, boto3 as _b3, time as _t
    page_url      = payload.get('url', '')
    new_title     = payload.get('title', '')
    new_meta      = payload.get('meta_description', '')
    top_queries   = payload.get('top_queries', [])
    review_score  = payload.get('review_score')
    review_notes  = payload.get('review_notes', '')
    BKT = 'nexabuilder-root-site-979841141166-us-west-1-an'
    CF  = 'EDLQAZ1IS2WIG'
    # Derive S3 key from URL
    path = page_url.replace('https://www.nexabuilder.com','').replace('https://nexabuilder.com','').strip('/')
    s3_key = (path + '/index.html') if path else 'index.html'
    s3 = _b3.client('s3', region_name='us-west-1')
    # Fetch current page
    try:
        obj = s3.get_object(Bucket=BKT, Key=s3_key)
        html = obj['Body'].read().decode('utf-8')
    except Exception as e:
        raise HTTPException(404, f'S3 page not found: {s3_key} — {e}')
    original_title = ''
    original_meta  = ''
    # Extract originals for the audit record
    tm = _re.search(r'<title>(.*?)</title>', html, _re.S|_re.I)
    if tm: original_title = _re.sub(r'<[^>]+>','',tm.group(1)).strip()
    mm = _re.search(r'<meta[^>]+name=.{0,1}description.{0,1}[^>]+/?>', html, _re.I)
    if mm: original_meta = mm.group(0)
    patched = html
    # Patch title
    if new_title:
        patched = _re.sub(r'<title>.*?</title>', f'<title>{new_title}</title>', patched, flags=_re.S|_re.I, count=1)
    # Patch or inject meta description
    if new_meta:
        new_meta_tag = f'<meta name="description" content="{new_meta}">'
        if _re.search(r'<meta[^>]+name=.{0,1}description.{0,1}', patched, _re.I):
            patched = _re.sub(r'<meta[^>]+name=.{0,1}description.{0,1}[^>]+/?>', new_meta_tag, patched, flags=_re.I, count=1)
        else:
            # Inject after <title> tag
            patched = _re.sub(r'(</title>)', r'\1\n  ' + new_meta_tag, patched, flags=_re.I, count=1)
    if patched == html:
        raise HTTPException(400, 'No changes applied — title/meta tags not found or nothing to update')
    # Write back to S3
    s3.put_object(Bucket=BKT, Key=s3_key, Body=patched.encode('utf-8'),
                  ContentType='text/html', CacheControl='public, max-age=3600')
    # CF invalidation
    cf_cli = _b3.client('cloudfront', region_name='us-east-1')
    try:
        slug_path = '/' + s3_key.replace('/index.html','')
        cf_cli.create_invalidation(DistributionId=CF, InvalidationBatch={
            'Paths': {'Quantity': 2, 'Items': [slug_path + '/', slug_path + '/index.html']},
            'CallerReference': str(int(_t.time()))
        })
        cf_invalidated = True
    except Exception:
        cf_invalidated = False
    # Save to page_recovery audit table
    db = _db()
    try:
        db.execute(sqlt(
            "INSERT INTO page_recovery (page_url, s3_key, original_title, original_meta, "
            "applied_title, applied_meta, review_score, review_notes, last_reviewed, last_applied) "
            "VALUES (:url, :s3k, :ot, :om, :at, :am, :rs, :rn, NOW(), NOW()) "
            "ON CONFLICT (page_url) DO UPDATE SET "
            "applied_title=:at, applied_meta=:am, review_score=:rs, review_notes=:rn, last_applied=NOW()"
        ), {'url': page_url, 's3k': s3_key, 'ot': original_title, 'om': original_meta[:500],
            'at': new_title, 'am': new_meta, 'rs': review_score, 'rn': review_notes[:2000]})
        db.commit()
    finally:
        db.close()
    return {
        'success': True,
        's3_key': s3_key,
        'cf_invalidated': cf_invalidated,
        'original_title': original_title,
        'applied_title': new_title,
        'applied_meta': new_meta,
        'message': f'Title and meta patched on S3. CF invalidated: {cf_invalidated}. Live in ~2 minutes.'
    }


@router.post('/recovery/apply-full-fix')
async def recovery_apply_full_fix(payload: dict, x_admin_key: str = Header(...)):
    # Fetch live page, run AI patch for all specific fixes, write back to S3
    _require_admin(x_admin_key)
    import anthropic as _ant, re as _re, boto3 as _b3, time as _t
    page_url    = payload.get('url', '')
    fixes       = payload.get('fixes', [])
    title       = payload.get('title', '')
    meta        = payload.get('meta_description', '')
    top_queries = payload.get('top_queries', [])
    review_score= payload.get('review_score')
    BKT = 'nexabuilder-root-site-979841141166-us-west-1-an'
    CF  = 'EDLQAZ1IS2WIG'
    path = page_url.replace('https://www.nexabuilder.com','').replace('https://nexabuilder.com','').strip('/')
    s3_key = (path + '/index.html') if path else 'index.html'
    s3 = _b3.client('s3', region_name='us-west-1')
    try:
        obj = s3.get_object(Bucket=BKT, Key=s3_key)
        html = obj['Body'].read().decode('utf-8')
    except Exception as e:
        raise HTTPException(404, f'Page not found on S3: {s3_key}')
    if not fixes:
        raise HTTPException(400, 'No fixes provided')
    fixes_block = chr(10).join(f'{i+1}. {f}' for i,f in enumerate(fixes))
    queries_block = ', '.join(top_queries[:5]) if top_queries else ''
    prompt = (
        f'You are patching a live HTML service page for NexaBuilder.com.\n'
        f'Page URL: {page_url}\n'
        f'Top GSC queries: {queries_block}\n\n'
        f'Apply ALL of these specific fixes to the HTML below:\n{fixes_block}\n\n'
        f'RULES:\n'
        f'1. Return ONLY the complete patched HTML — no explanation, no markdown, no code fences.\n'
        f'2. Make SURGICAL changes only — preserve all CSS, JS, nav, footer, chat widget, existing content.\n'
        f'3. For trust signals: find the hero stats bar (class hero-stats or similar) and add/update contractor count, years in business, and CSLB badge.\n'
        f'4. For internal links: add a section with city/county links (Orange County, Los Angeles, Irvine, Anaheim, Long Beach) near the bottom content area, before the footer. Use nexabuilder.com/locations/ URL pattern.\n'
        f'5. For FAQ: add one new FAQ entry about verifying a CSLB contractor license at the end of the existing FAQ list. Format must match existing FAQ HTML structure exactly.\n'
        f'6. For the CTA: find the primary get-quote form or CTA button. Add a ZIP code input field BEFORE the submit button with placeholder="Enter your ZIP code" and name="zip". If there is no form, wrap the CTA button in a minimal inline form.\n'
        f'7. Do NOT change the title, meta, nav, footer, or any CSS/JS — those are handled separately.\n'
        f'8. Keep total page size within 10% of original ({len(html):,} chars).\n\n'
        f'HTML TO PATCH:\n{html}'
    )
    client = _ant.AsyncAnthropic()
    msg = await client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=16000,
        messages=[{'role':'user','content':prompt}]
    )
    patched_html = msg.content[0].text.strip()
    # Strip any accidental markdown fences
    if patched_html.startswith('```'):
        patched_html = _re.sub(r'^```[a-z]*\n?', '', patched_html)
        patched_html = _re.sub(r'\n?```$', '', patched_html)
    # Apply title/meta on top if provided
    if title:
        patched_html = _re.sub(r'<title>.*?</title>', f'<title>{title}</title>', patched_html, flags=_re.S|_re.I, count=1)
    if meta:
        new_meta_tag = f'<meta name="description" content="{meta}">'
        if _re.search(r'<meta[^>]+name=.{0,1}description', patched_html, _re.I):
            patched_html = _re.sub(r'<meta[^>]+name=.{0,1}description[^>]+/?>', new_meta_tag, patched_html, flags=_re.I, count=1)
        else:
            patched_html = _re.sub(r'(</title>)', r'\1\n  ' + new_meta_tag, patched_html, flags=_re.I, count=1)
    if not patched_html.strip().startswith('<!') and not patched_html.strip().startswith('<html'):
        raise HTTPException(500, f'AI returned non-HTML response (first 100 chars: {patched_html[:100]})')
    s3.put_object(Bucket=BKT, Key=s3_key, Body=patched_html.encode('utf-8'),
                  ContentType='text/html', CacheControl='public, max-age=3600')
    cf_cli = _b3.client('cloudfront', region_name='us-east-1')
    try:
        slug_path = '/' + s3_key.replace('/index.html','')
        cf_cli.create_invalidation(DistributionId=CF, InvalidationBatch={
            'Paths': {'Quantity': 2, 'Items': [slug_path+'/', slug_path+'/index.html']},
            'CallerReference': str(int(_t.time()))
        })
        cf_invalidated = True
    except Exception:
        cf_invalidated = False
    db = _db()
    try:
        db.execute(sqlt(
            'INSERT INTO page_recovery (page_url, s3_key, applied_title, applied_meta, review_score, review_notes, last_reviewed, last_applied) '
            'VALUES (:url, :s3k, :at, :am, :rs, :rn, NOW(), NOW()) '
            'ON CONFLICT (page_url) DO UPDATE SET applied_title=:at, applied_meta=:am, review_score=:rs, review_notes=:rn, last_applied=NOW()'
        ), {'url':page_url,'s3k':s3_key,'at':title,'am':meta,'rs':review_score,'rn':fixes_block})
        db.commit()
    finally:
        db.close()
    return {
        'success': True,
        's3_key': s3_key,
        'cf_invalidated': cf_invalidated,
        'original_size': len(html),
        'patched_size': len(patched_html),
        'fixes_applied': len(fixes),
        'tokens': msg.usage.input_tokens + msg.usage.output_tokens,
        'message': f'All {len(fixes)} fixes applied to live page. CF invalidated: {cf_invalidated}. Live in ~2 minutes.'
    }
