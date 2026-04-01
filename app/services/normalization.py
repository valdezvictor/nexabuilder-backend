# app/services/normalization.py

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import Contractor, License, Trade, ZipCode
from app.enums.enrichment import EntityType, JobType
from app.models.enrichment_job import EnrichmentJob


async def normalize_cslb_record(db: AsyncSession, scrape_run_id: int, raw: dict) -> None:
    """
    Normalize a single CSLB record into contractors, licenses, trades, zipcodes,
    and enqueue enrichment jobs.
    """
    # 1) Upsert zipcode
    zipcode = await _get_or_create_zipcode(db, raw)

    # 2) Upsert contractor
    contractor = await _get_or_create_contractor(db, raw, zipcode)

    # 3) Upsert license
    license_obj = await _get_or_create_license(db, raw, contractor, scrape_run_id)

    # 4) Upsert trade(s)
    trades = await _get_or_create_trades(db, raw)

    # 5) Link contractor ↔ trades
    await _link_contractor_trades(db, contractor, trades)

    # 6) Enqueue enrichment jobs
    await _enqueue_enrichment_jobs(db, contractor, license_obj, zipcode, trades)


async def _get_or_create_zipcode(db: AsyncSession, raw: dict) -> ZipCode:
    zip_str = (raw.get("zip") or "").strip()
    if not zip_str:
        return None

    stmt = select(ZipCode).where(ZipCode.zip == zip_str)
    result = await db.execute(stmt)
    zipcode = result.scalar_one_or_none()

    if not zipcode:
        zipcode = ZipCode(
            zip=zip_str,
            city=raw.get("city"),
            state=raw.get("state_code"),
        )
        db.add(zipcode)
        await db.flush()

    return zipcode


async def _get_or_create_contractor(db: AsyncSession, raw: dict, zipcode: ZipCode | None) -> Contractor:
    name = (raw.get("contractor_name") or "").strip()
    phone = (raw.get("phone") or "").strip() or None

    stmt = select(Contractor).where(Contractor.name == name, Contractor.phone == phone)
    result = await db.execute(stmt)
    contractor = result.scalar_one_or_none()

    if not contractor:
        contractor = Contractor(
            name=name,
            phone=phone,
            email=raw.get("email"),
            website=raw.get("website"),
            city=raw.get("city"),
            state=raw.get("state_code"),
            postal_code=raw.get("zip"),
        )
        db.add(contractor)
        await db.flush()

    return contractor


async def _get_or_create_license(
    db: AsyncSession,
    raw: dict,
    contractor: Contractor,
    scrape_run_id: int,
) -> License:
    state_code = raw.get("state_code")
    license_number = raw.get("license_number")

    stmt = select(License).where(
        License.state_code == state_code,
        License.license_number == license_number,
    )
    result = await db.execute(stmt)
    license_obj = result.scalar_one_or_none()

    if not license_obj:
        license_obj = License(
            contractor_id=contractor.id,
            state_code=state_code,
            license_number=license_number,
            status=raw.get("status"),
            contractor_name=raw.get("contractor_name"),
            scrape_run_id=scrape_run_id,
        )
        db.add(license_obj)
        await db.flush()

    return license_obj


async def _get_or_create_trades(db: AsyncSession, raw: dict) -> list[Trade]:
    # Placeholder: depends on how CSLB exposes classifications/trades
    return []


async def _link_contractor_trades(db: AsyncSession, contractor: Contractor, trades: list[Trade]) -> None:
    for trade in trades:
        if trade not in contractor.trades:
            contractor.trades.append(trade)
    await db.flush()


async def _enqueue_enrichment_jobs(
    db: AsyncSession,
    contractor: Contractor,
    license_obj: License,
    zipcode: ZipCode | None,
    trades: list[Trade],
) -> None:
    # Example: geocode contractor address
    job = EnrichmentJob(
        entity_type=EntityType.CONTRACTOR.value,
        entity_id=contractor.id,
        job_type=JobType.GEOCODE_ADDRESS.value,
        status="pending",
        payload={
            "address_line1": contractor.address_line1,
            "city": contractor.city,
            "state": contractor.state,
            "postal_code": contractor.postal_code,
        },
    )
    db.add(job)

    # Example: verify license
    job2 = EnrichmentJob(
        entity_type=EntityType.LICENSE.value,
        entity_id=license_obj.id,
        job_type=JobType.VERIFY_LICENSE.value,
        status="pending",
        payload={
            "state_code": license_obj.state_code,
            "license_number": license_obj.license_number,
        },
    )
    db.add(job2)

    await db.flush()
