#!/usr/bin/env python3
"""
B7 — Daily Microsoft Clarity pull job
Pulls previous 24h metrics via Clarity Data Export API
(https://www.clarity.ms/export-data/api/v1/project-live-insights)
and upserts into clarity_daily.

Deploy: /var/www/nexabuilder/backend/current/app/scripts/clarity_daily_pull.py
Cron:   5 6 * * * python3 /var/www/nexabuilder/backend/current/app/scripts/clarity_daily_pull.py >> /var/log/clarity_pull.log 2>&1
"""
import asyncio, asyncpg, json, os, sys, logging, ssl, certifi
from datetime import date, datetime, timedelta, timezone
import urllib.request, urllib.error, urllib.parse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("clarity_pull")

CLARITY_API = "https://www.clarity.ms/export-data/api/v1/project-live-insights"
SSL_CTX = ssl.create_default_context(cafile=certifi.where())

def ssm_get(name: str) -> str:
    import subprocess
    r = subprocess.run(
        ["aws", "ssm", "get-parameter", "--name", name,
         "--with-decryption", "--query", "Parameter.Value", "--output", "text"],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"SSM get failed for {name}: {r.stderr.strip()}")
    return r.stdout.strip()

def clarity_get(token: str, **params) -> dict:
    qs = urllib.parse.urlencode(params)
    url = f"{CLARITY_API}?{qs}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
        return json.loads(resp.read().decode())

async def ensure_unique_constraint(conn):
    await conn.execute("""
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname='clarity_daily_report_date_unique'
          ) THEN
            ALTER TABLE clarity_daily
              ADD CONSTRAINT clarity_daily_report_date_unique UNIQUE (report_date);
          END IF;
        END $$;
    """)

async def upsert(conn, report_date: date, sessions: int, payload: dict):
    await conn.execute("""
        INSERT INTO clarity_daily (report_date, pulled_at, sessions, payload)
        VALUES ($1, NOW(), $2, $3)
        ON CONFLICT (report_date)
        DO UPDATE SET pulled_at=NOW(), sessions=$2, payload=$3
    """, report_date, sessions, json.dumps(payload))

async def main():
    report_date = date.today() - timedelta(days=1)
    log.info(f"Pulling Clarity data (numOfDays=1) — reporting as {report_date}")

    token   = ssm_get("/nexabuilder/clarity/api_token")
    db_url  = ssm_get("/nexabuilder/db/DATABASE_URL").replace("postgresql+asyncpg://", "postgresql://")

    results = {}

    # Pull by URL
    try:
        data = clarity_get(token, numOfDays=1, dimension1="Url")
        results["by_url"] = data
        log.info(f"  by_url: OK — {len(data) if isinstance(data, list) else 'dict'}")
    except Exception as e:
        log.warning(f"  by_url failed: {e}")
        results["by_url"] = {"error": str(e)}

    # Pull by device
    try:
        data = clarity_get(token, numOfDays=1, dimension1="Device")
        results["by_device"] = data
        log.info(f"  by_device: OK")
    except Exception as e:
        log.warning(f"  by_device failed: {e}")
        results["by_device"] = {"error": str(e)}

    # Pull by OS
    try:
        data = clarity_get(token, numOfDays=1, dimension1="OS")
        results["by_os"] = data
        log.info(f"  by_os: OK")
    except Exception as e:
        log.warning(f"  by_os failed: {e}")
        results["by_os"] = {"error": str(e)}

    # Extract total sessions from by_url if available
    total_sessions = 0
    by_url_data = results.get("by_url", {})
    if isinstance(by_url_data, list):
        for item in by_url_data:
            total_sessions += item.get("totalSessions", item.get("sessionsCount", 0))
    elif isinstance(by_url_data, dict):
        total_sessions = by_url_data.get("totalSessions", 0)

    payload = {
        "date": report_date.isoformat(),
        "pulled_at": datetime.now(timezone.utc).isoformat(),
        **results
    }

    conn = await asyncpg.connect(db_url)
    await ensure_unique_constraint(conn)
    await upsert(conn, report_date, total_sessions, payload)
    await conn.close()

    log.info(f"✓ Upserted clarity_daily for {report_date} — sessions={total_sessions}")

    # Print /get-quote summary
    print(f"\n=== Clarity summary for {report_date} ===")
    print(json.dumps(payload, indent=2, default=str)[:1500])

asyncio.run(main())
