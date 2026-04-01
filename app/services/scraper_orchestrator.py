import asyncio
from typing import List, Dict
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.scraper import start_run, finish_run, save_licenses
from app.services.registry import get_scraper


async def run_single_state(
    session: AsyncSession,
    state_code: str,
    source: str,
) -> int:
    """
    Runs a single state's scraper with:
    - start_run
    - fetch
    - save
    - finish_run
    """
    run_id = await start_run(session, state_code, source)

    success = False
    items_fetched = 0
    error_message = None

    try:
        scraper_fn = get_scraper(state_code)
        raw = await scraper_fn(state_code)
        items_fetched = len(raw)

        await save_licenses(
            session=session,
            run_id=run_id,
            state_code=state_code,
            licenses=raw,
        )

        success = True

    except Exception as exc:
        error_message = str(exc)

    await finish_run(
        session=session,
        run_id=run_id,
        success=success,
        items_fetched=items_fetched,
        error_message=error_message,
    )

    return run_id

# ---- Run Multiple states --------
async def run_multiple_states(
    session: AsyncSession,
    states: List[str],
    source: str = "scraper",
    concurrency: int = 3,
):
    """
    Runs multiple states concurrently with a semaphore.
    """
    sem = asyncio.Semaphore(concurrency)

    async def worker(state: str):
        async with sem:
            return await run_single_state(session, state, source)

    tasks = [asyncio.create_task(worker(s)) for s in states]
    return await asyncio.gather(*tasks)

