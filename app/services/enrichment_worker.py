# app/services/enrichment_worker.py

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contractor, License, EnrichmentJob
from app.enums.enrichment import EntityType, JobType

# from contractor_pipeline.services.enrichment_providers import (
#     search_provider,
#     apollo_provider,
#     scraping_provider,
#     pattern_guessing_provider,
# )


async def process_next_job(db: AsyncSession) -> bool:
    stmt = select(EnrichmentJob).where(EnrichmentJob.status == "pending").limit(1)
    result = await db.execute(stmt)
    job = result.scalar_one_or_none()
    if not job:
        return False

    job.status = "processing"

    try:
        if job.job_type == JobType.DISCOVER_WEBSITE.value:
            await _handle_discover_website(db, job)
        elif job.job_type == JobType.DISCOVER_EMAIL.value:
            await _handle_discover_email(db, job)
        elif job.job_type == JobType.GEOCODE_ADDRESS.value:
            await _handle_geocode_address(db, job)
        elif job.job_type == JobType.VERIFY_LICENSE.value:
            await _handle_verify_license(db, job)
        # etc...

        job.status = "success"
    except Exception as e:
        job.status = "failed"
        job.error_message = str(e)

    await db.commit()
    return True


async def _load_entity(db: AsyncSession, job: EnrichmentJob):
    if job.entity_type == EntityType.CONTRACTOR.value:
        return await db.get(Contractor, job.entity_id)
    if job.entity_type == EntityType.LICENSE.value:
        return await db.get(License, job.entity_id)
    return None


async def _handle_discover_website(db: AsyncSession, job: EnrichmentJob) -> None:
    contractor = await _load_entity(db, job)
    if not contractor:
        return

    # Here is where we plug your search_provider:
    # url, confidence, source = search_provider.discover_website(
    #     SimpleNamespace(
    #         business_name=contractor.name,
    #         city=contractor.city,
    #         state_code=contractor.state,
    #         license_number=None,  # or from license
    #     )
    # )

    # For now, just stub:
    url, confidence, source = None, None, None

    job.result = {
        "website": url,
        "confidence": confidence,
        "source": source,
    }

    if url:
        contractor.website = url
        await db.flush()
