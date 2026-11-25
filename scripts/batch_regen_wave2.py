"""
NexaBuilder city-page CDM batch regeneration - Wave 2
Scope: cities where body_html does NOT contain an H1 tag (or is empty/null)
Concurrency: 5 workers
Log: /var/log/nexabuilder_batch_regen_wave2.log

Usage:
  nohup /var/www/nexabuilder/backend/current/venv/bin/python3 \
    /var/www/nexabuilder/scripts/batch_regen_wave2.py \
    >> /var/log/nexabuilder_batch_regen_wave2.log 2>&1 &
"""
import asyncio, asyncpg, aiohttp, json, datetime, sys

API_BASE    = "http://localhost:8000"
CONCURRENCY = 5
LOG_FILE    = "/var/log/nexabuilder_batch_regen_wave2.log"
DB_DSN      = "postgresql://nexabuilder_admin:NexaDB2026Prod!@nexabuilder-prod-db.cyfiieky5gzb.us-west-1.rds.amazonaws.com:5432/postgres"

def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

async def regen_city(session, county, slug, name, sem, idx, total):
    async with sem:
        url = f"{API_BASE}/admin/cities/{county}/{slug}/cdm-generate"
        try:
            async with session.post(
                url,
                headers={"Authorization": "Bearer batch-internal"},
                timeout=aiohttp.ClientTimeout(total=150)
            ) as resp:
                body = await resp.text()
                if resp.status == 200:
                    data = json.loads(body)
                    log(f"[{idx}/{total}] OK  {name} ({county}/{slug}) score={data.get('score')}")
                    return "ok"
                else:
                    log(f"[{idx}/{total}] ERR {name} ({county}/{slug}) HTTP {resp.status}: {body[:120]}")
                    return "error"
        except asyncio.TimeoutError:
            log(f"[{idx}/{total}] TIMEOUT {name} ({county}/{slug})")
            return "timeout"
        except Exception as e:
            log(f"[{idx}/{total}] EXC {name} ({county}/{slug}) {e}")
            return "exception"

async def main():
    conn = await asyncpg.connect(DB_DSN)
    rows = await conn.fetch(
        """SELECT county_slug, slug, name FROM nb_cities
           WHERE body_html NOT ILIKE '%<h1%' OR body_html IS NULL OR body_html = ''
           ORDER BY county_slug, name"""
    )
    await conn.close()

    total = len(rows)
    log(f"=== Wave 2 start: {total} cities to regenerate ===")

    if total == 0:
        log("=== Wave 2: nothing to do, all cities have H1 content ===")
        return

    sem = asyncio.Semaphore(CONCURRENCY)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY + 2)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            regen_city(session, r["county_slug"], r["slug"], r["name"], sem, i+1, total)
            for i, r in enumerate(rows)
        ]
        results = await asyncio.gather(*tasks)

    ok      = results.count("ok")
    errors  = results.count("error")
    timeouts= results.count("timeout")
    excepts = results.count("exception")
    log(f"=== Wave 2 complete: {total} cities | OK={ok} ERR={errors} TIMEOUT={timeouts} EXC={excepts} ===")

if __name__ == "__main__":
    asyncio.run(main())
