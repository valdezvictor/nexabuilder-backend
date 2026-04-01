# app/services/ingestion/personnel_loader.py

import csv
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contractor, License
from app.services.ingestion.normalization import clean_str, parse_date


async def ingest_personnel(db: AsyncSession, csv_path: str) -> None:
    path = Path(csv_path)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lic_no = clean_str(row.get("LIC-NO"))
            if not lic_no:
                continue

            stmt = select(License).where(
                License.license_number == lic_no,
                License.state_code == "CA",
            )
            result = await db.execute(stmt)
            license_obj = result.scalar_one_or_none()
            if not license_obj:
                continue

            # Here we would map to ContractorPersonnel once the model exists
            # contractor = license_obj.contractor
            # ...

        await db.commit()
