# app/services/ingestion/workers_comp_loader.py

import csv
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import License
from app.services.ingestion.normalization import clean_str, parse_date


async def ingest_workers_comp(db: AsyncSession, csv_path: str) -> None:
    path = Path(csv_path)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            license_no = clean_str(row.get("LicenseNo"))
            if not license_no:
                continue

            stmt = select(License).where(
                License.license_number == license_no,
                License.state_code == "CA",
            )
            result = await db.execute(stmt)
            license_obj = result.scalar_one_or_none()
            if not license_obj:
                continue

            license_obj.workers_comp_status = clean_str(row.get("WorkersCompCoverageType"))
            license_obj.workers_comp_insurer = clean_str(row.get("WCInsuranceCompany"))
            license_obj.workers_comp_policy = clean_str(row.get("WCPolicyNo"))
            license_obj.workers_comp_effective = parse_date(row.get("EffectiveDate"))
            license_obj.workers_comp_expiration = parse_date(row.get("ExpirationDate"))
            # cancellation / suspend dates could be stored in extra fields later

        await db.commit()
