# app/services/ingestion/normalization.py

from __future__ import annotations

from datetime import datetime
from typing import Optional, List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.models import Contractor, License, Trade, ZipCode
from app.models.trade import contractor_trades
from app.enums.enrichment import EntityType, JobType
from app.models.enrichment_job import EnrichmentJob


DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y"]


def parse_date(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def clean_str(value: str | None) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    return v or None


async def get_or_create_zipcode(db: AsyncSession, row: dict) -> Optional[ZipCode]:
    zip_str = clean_str(row.get("ZIPCode"))
    if not zip_str:
        return None

    stmt = select(ZipCode).where(ZipCode.zip == zip_str)
    result = await db.execute(stmt)
    zipcode = result.scalar_one_or_none()

    if not zipcode:
        zipcode = ZipCode(
            zip=zip_str,
            city=clean_str(row.get("City")),
            state=clean_str(row.get("State")),
            county=clean_str(row.get("County")),
        )
        db.add(zipcode)
        await db.flush()

    return zipcode


async def get_or_create_contractor(db: AsyncSession, row: dict) -> Contractor:
    name = clean_str(row.get("BusinessName"))
    phone = clean_str(row.get("BusinessPhone"))

    stmt = select(Contractor).where(
        Contractor.name == name,
        Contractor.phone == phone,
    )
    result = await db.execute(stmt)
    contractor = result.scalar_one_or_none()

    if not contractor:
        contractor = Contractor(
            name=name,
            legal_name=clean_str(row.get("FullBusinessName")),
            dba_name=clean_str(row.get("BUS-NAME-2")),
            entity_type=clean_str(row.get("BusinessType")),
            phone=phone,
            address_line1=clean_str(row.get("MailingAddress")),
            city=clean_str(row.get("City")),
            state=clean_str(row.get("State")),
            postal_code=clean_str(row.get("ZIPCode")),
            county=clean_str(row.get("County")),
        )
        db.add(contractor)
        await db.flush()

    return contractor


async def get_or_create_license(
    db: AsyncSession,
    row: dict,
    contractor: Contractor,
) -> License:
    license_number = clean_str(row.get("LicenseNo"))
    state_code = clean_str(row.get("State")) or "CA"

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
        )
        db.add(license_obj)
        await db.flush()

    # Update fields from master file
    license_obj.status = clean_str(row.get("PrimaryStatus"))
    license_obj.contractor_name = clean_str(row.get("BusinessName"))
    license_obj.issue_date = parse_date(row.get("IssueDate"))
    license_obj.expiration_date = parse_date(row.get("ExpirationDate"))
    license_obj.classification = clean_str(row.get("Classifications(s)"))
    license_obj.workers_comp_status = clean_str(row.get("WorkersCompCoverageType"))
    license_obj.bond_amount = int(row["CBAmount"].strip()) if row.get("CBAmount") and row["CBAmount"].strip().isdigit() else None

    # Workers comp fields from master (will be overridden by workerscompdata later)
    license_obj.workers_comp_insurer = clean_str(row.get("WCInsuranceCompany"))
    license_obj.workers_comp_policy = clean_str(row.get("WCPolicyNumber"))
    license_obj.workers_comp_effective = parse_date(row.get("WCEffectiveDate"))
    license_obj.workers_comp_expiration = parse_date(row.get("WCExpirationDate"))

    return license_obj


async def get_or_create_trades(db: AsyncSession, row: dict) -> List[Trade]:
    raw = row.get("Classifications(s)") or ""
    parts = [p.strip() for p in raw.split("|") if p.strip()]
    trades: List[Trade] = []

    for code in parts:
        stmt = select(Trade).where(Trade.name == code)
        result = await db.execute(stmt)
        trade = result.scalar_one_or_none()
        if not trade:
            trade = Trade(name=code)
            db.add(trade)
            await db.flush()
        trades.append(trade)

    return trades


from sqlalchemy import select
from sqlalchemy.orm import selectinload

async def link_contractor_trades(
    db: AsyncSession,
    contractor: Contractor,
    trades: List[Trade],
) -> None:
    # Reload contractor with trades preloaded (async-safe)
    result = await db.execute(
        select(Contractor)
        .options(selectinload(Contractor.trades))
        .where(Contractor.id == contractor.id)
    )
    contractor_with_trades = result.scalar_one()

    # Existing trades in DB
    existing_trade_ids = {t.id for t in contractor_with_trades.trades}

    # Deduplicate incoming trades
    incoming_trade_ids = set()
    unique_trades = []
    for t in trades:
        if t.id not in incoming_trade_ids:
            incoming_trade_ids.add(t.id)
            unique_trades.append(t)

    # Append only trades that are not already linked
    for trade in unique_trades:
        if trade.id not in existing_trade_ids:
            contractor_with_trades.trades.append(trade)

    await db.flush()



async def enqueue_enrichment_jobs_for_master_row(
    db: AsyncSession,
    contractor: Contractor,
    license_obj: License,
) -> None:
    # Geocode contractor address
    db.add(
        EnrichmentJob(
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
    )

    # Verify license
    db.add(
        EnrichmentJob(
            entity_type=EntityType.LICENSE.value,
            entity_id=license_obj.id,
            job_type=JobType.VERIFY_LICENSE.value,
            status="pending",
            payload={
                "state_code": license_obj.state_code,
                "license_number": license_obj.license_number,
            },
        )
    )

    # Discover website/email (to be implemented using your enrichment_providers)
    db.add(
        EnrichmentJob(
            entity_type=EntityType.CONTRACTOR.value,
            entity_id=contractor.id,
            job_type=JobType.DISCOVER_WEBSITE.value,
            status="pending",
            payload={
                "name": contractor.name,
                "city": contractor.city,
                "state": contractor.state,
                "license_number": license_obj.license_number,
            },
        )
    )

    await db.flush()
