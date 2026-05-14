# app/routers/api/enrichment.py
# Email enrichment pipeline for contractor records
# Stage 1: Apollo API | Stage 2: DuckDuckGo search | Stage 3: Website scraper

import os, re, time, requests, psycopg2
from fastapi import APIRouter, Depends, BackgroundTasks
from app.core.auth import get_current_user

router = APIRouter(prefix="/api/enrichment", tags=["Enrichment"])

DB_CONFIG = {
    "host": "nexabuilder-prod-db.cyfiieky5gzb.us-west-1.rds.amazonaws.com",
    "port": 5432, "database": "postgres",
    "user": "nexabuilder_admin", "password": "NexaDB2026Prod!"
}

# Email confidence scoring
EMAIL_CONFIDENCE = {
    "info": 0.4, "contact": 0.5, "support": 0.5,
    "admin": 0.4, "sales": 0.3, "billing": 0.3,
    "office": 0.5, "hello": 0.4,
}

def score_email(email: str) -> float:
    local = email.split("@")[0].lower()
    for pattern, score in EMAIL_CONFIDENCE.items():
        if pattern in local:
            return score
    # Personal email patterns score higher
    if "." in local or len(local) < 12:
        return 0.8
    return 0.6

def extract_emails_from_text(text: str) -> list:
    pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
    emails = re.findall(pattern, text)
    # Filter out image files and generic noise
    return [e for e in emails if not any(e.endswith(x) for x in ['.png','.jpg','.gif','.css'])]

def search_provider(business_name: str, city: str) -> dict:
    """Stage 2: DuckDuckGo search for website/email."""
    try:
        query = f"{business_name} {city} California contractor email"
        url = "https://api.duckduckgo.com/"
        resp = requests.get(url, params={
            "q": query, "format": "json", "no_html": 1, "skip_disambig": 1
        }, timeout=5)
        data = resp.json()
        abstract = data.get("AbstractText", "")
        emails = extract_emails_from_text(abstract)
        if emails:
            best = max(emails, key=score_email)
            return {"email": best, "confidence": score_email(best), "source": "search"}
    except Exception as e:
        print(f"[SEARCH] {e}")
    return {}

def scrape_website(url: str) -> dict:
    """Stage 3: Scrape website for contact email."""
    try:
        if not url.startswith("http"):
            url = "https://" + url
        resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        emails = extract_emails_from_text(resp.text)
        if not emails:
            # Try /contact page
            contact_url = url.rstrip("/") + "/contact"
            resp2 = requests.get(contact_url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
            emails = extract_emails_from_text(resp2.text)
        if emails:
            best = max(emails, key=score_email)
            return {"email": best, "confidence": score_email(best), "source": "scraper"}
    except Exception as e:
        print(f"[SCRAPER] {e}")
    return {}

def enrich_batch(limit: int = 100) -> dict:
    """Main enrichment engine - runs through provider pipeline."""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    cur.execute("""
        SELECT id, business_name, city, zip_code, website
        FROM contractors
        WHERE email IS NULL AND primary_status = 'CLEAR'
        ORDER BY id
        LIMIT %s
    """, (limit,))
    contractors = cur.fetchall()

    stats = {"processed": 0, "enriched": 0, "skipped": 0,
             "providers": {"search": 0, "scraper": 0}}

    for contractor_id, name, city, zip_code, website in contractors:
        if not name:
            stats["skipped"] += 1
            continue

        result = {}

        # Stage 2: Search (Stage 1 Apollo requires paid key - add later)
        if not result:
            result = search_provider(name, city or "")

        # Stage 3: Scraper (if we have a website)
        if not result and website:
            result = scrape_website(website)

        if result.get("email") and result.get("confidence", 0) >= 0.4:
            cur.execute("""
                UPDATE contractors SET
                    email = %s,
                    email_confidence = %s,
                    enrichment_source = %s,
                    enriched_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s AND email IS NULL
            """, (result["email"], result["confidence"], result["source"], contractor_id))
            conn.commit()
            stats["enriched"] += 1
            stats["providers"][result.get("source", "unknown")] = \
                stats["providers"].get(result.get("source", "unknown"), 0) + 1
        else:
            stats["skipped"] += 1

        stats["processed"] += 1
        time.sleep(0.1)  # Rate limit

    conn.close()
    return stats


@router.post("/run")
async def run_enrichment(
    limit: int = 100,
    background_tasks: BackgroundTasks = None,
    identity: dict = Depends(get_current_user),
):
    """
    Run email enrichment on contractors missing emails.
    Uses: DuckDuckGo search → website scraper pipeline.
    Add Apollo API key to SSM for higher accuracy (Stage 1).
    """
    # Run inline for small batches, background for large
    if limit <= 50:
        stats = enrich_batch(limit)
        return {"status": "complete", "stats": stats}
    else:
        if background_tasks:
            background_tasks.add_task(enrich_batch, limit)
        return {"status": "started", "message": f"Enriching {limit} contractors in background"}


@router.get("/status")
async def enrichment_status(identity: dict = Depends(get_current_user)):
    """Check enrichment progress."""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM contractors WHERE primary_status='CLEAR'")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM contractors WHERE email IS NOT NULL")
    enriched = cur.fetchone()[0]
    cur.execute("SELECT enrichment_source, COUNT(*) FROM contractors WHERE email IS NOT NULL GROUP BY enrichment_source")
    by_source = dict(cur.fetchall())
    conn.close()
    return {
        "total_active": total,
        "enriched": enriched,
        "pending": total - enriched,
        "enrichment_rate": f"{enriched/total*100:.1f}%" if total else "0%",
        "by_source": by_source,
    }


@router.post("/contractors/refresh")
async def refresh_contractors(
    identity: dict = Depends(get_current_user),
):
    """
    Manually trigger CSLB contractor data refresh from S3.
    Also run monthly via EventBridge rule: nexabuilder-contractor-refresh
    """
    import boto3, csv, psycopg2
    from datetime import datetime

    DB_CONFIG = {
        "host": "nexabuilder-prod-db.cyfiieky5gzb.us-west-1.rds.amazonaws.com",
        "port": 5432, "database": "postgres",
        "user": "nexabuilder_admin", "password": "NexaDB2026Prod!"
    }

    try:
        s3 = boto3.client("s3", region_name="us-west-1")
        s3.download_file("nexabuilder-prod-bucket", "contractors/MasterLicenseData.csv", "/tmp/refresh.csv")

        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        rows = []
        with open("/tmp/refresh.csv", encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                lic = row.get("LicenseNo","").strip()
                if not lic: continue
                rows.append((lic, row.get("BusinessName","").strip()[:200] or None,
                    row.get("PrimaryStatus","").strip()[:50] or None,
                    row.get("ExpirationDate","").strip() or None,
                    row.get("Classifications(s)","").strip()[:200] or None))

        SQL = """
            INSERT INTO contractors (license_no, business_name, primary_status, classifications)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (license_no) DO UPDATE SET
                business_name=EXCLUDED.business_name,
                primary_status=EXCLUDED.primary_status,
                classifications=EXCLUDED.classifications,
                updated_at=NOW()
        """
        for i in range(0, len(rows), 2000):
            cur.executemany(SQL, [(r[0],r[1],r[2],r[4]) for r in rows[i:i+2000]])
            conn.commit()

        cur.execute("SELECT COUNT(*) FROM contractors")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM contractors WHERE primary_status='CLEAR'")
        active = cur.fetchone()[0]
        conn.close()

        return {
            "status": "complete",
            "processed": len(rows),
            "total_in_db": total,
            "active": active,
            "refreshed_at": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
