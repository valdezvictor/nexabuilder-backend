from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.scrape_run import ScrapeRun
from app.models.contractor import Contractor
from app.models.license import License

async def start_run(session: AsyncSession, state_code: str, source: str | None = None) -> int:
    """
    Create a new scrape run entry.
    Returns the run ID.
    """
    run = ScrapeRun(state_code=state_code, source=source)

    session.add(run)
    await session.flush()  # ensures run.id is populated

    return run.id

# --- Finish Run -------
async def finish_run(
    session: AsyncSession,
    run_id: int,
    success: bool,
    items_fetched: int = 0,
    error_message: str | None = None,
):
    """
    Mark a scrape run as finished.
    """
    stmt = select(ScrapeRun).where(ScrapeRun.id == run_id)
    result = await session.execute(stmt)
    run = result.scalar_one_or_none()

    if not run:
        return False

    run.success = success
    run.items_fetched = items_fetched
    run.error_message = error_message

    return True

# --- Contractor Licenses --------
async def save_licenses(
    session: AsyncSession,
    run_id: int,
    state_code: str,
    licenses: list[dict],
):
    """
    Insert or update contractor licenses for a given state.
    Each license dict must contain:
      - license_number
      - status
      - contractor_name
    """

    for lic in licenses:
        license_number = lic.get("license_number")
        contractor_name = lic.get("contractor_name")
        status = lic.get("status")

        # 1. Find or create contractor
        stmt = select(Contractor).where(Contractor.name == contractor_name)
        result = await session.execute(stmt)
        contractor = result.scalar_one_or_none()

        if not contractor:
            contractor = Contractor(name=contractor_name)
            session.add(contractor)
            await session.flush()

        # 2. Upsert license
        stmt = select(License).where(
            License.state_code == state_code,
            License.license_number == license_number,
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            existing.status = status
            existing.contractor_id = contractor.id
            existing.scrape_run_id = run_id
        else:
            new_license = License(
                contractor_id=contractor.id,
                state_code=state_code,
                license_number=license_number,
                status=status,
                contractor_name=contractor_name,
                scrape_run_id=run_id,
            )
            session.add(new_license)

# --- Contractors By State ------
async def get_contractors_by_state(session: AsyncSession, state_code: str):
    """
    Fetch contractor licenses for a given state.
    Returns a list of dicts.
    """
    stmt = (
        select(License)
        .where(License.state_code == state_code)
        .order_by(License.id.asc())
    )

    result = await session.execute(stmt)
    rows = result.scalars().all()

    return [
        {
            "id": lic.id,
            "state_code": lic.state_code,
            "license_number": lic.license_number,
            "status": lic.status,
            "contractor_name": lic.contractor_name,
        }
        for lic in rows
    ]
