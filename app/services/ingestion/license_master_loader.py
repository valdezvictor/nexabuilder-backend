# app/services/ingestion/license_master_loader.py

import csv
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ingestion.normalization import (
    get_or_create_zipcode,
    get_or_create_contractor,
    get_or_create_license,
    get_or_create_trades,
    link_contractor_trades,
    enqueue_enrichment_jobs_for_master_row,
)


async def iter_csv_rows(path: Path) -> AsyncIterator[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


async def ingest_license_master(db: AsyncSession, csv_path: str) -> None:
    path = Path(csv_path)
    async for row in iter_csv_rows(path):
        # 1) zipcode
        zipcode = await get_or_create_zipcode(db, row)

        # 2) contractor
        contractor = await get_or_create_contractor(db, row)

        # 3) license
        license_obj = await get_or_create_license(db, row, contractor)

        # 4) trades
        trades = await get_or_create_trades(db, row)
        await link_contractor_trades(db, contractor, trades)

        # 5) enrichment jobs
        await enqueue_enrichment_jobs_for_master_row(db, contractor, license_obj)

    await db.commit()
