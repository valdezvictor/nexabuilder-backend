from fastapi import Header, HTTPException
from app.models.tenant import Tenant
from app.db import get_sessionmaker

async def get_tenant(host: str = Header(None)):
    if not host:
        raise HTTPException(status_code=400, detail="Missing Host header")

    domain = host.split(":")[0]

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await db.execute(
            Tenant.__table__.select().where(Tenant.domain == domain)
        )
        tenant = result.first()

        if not tenant:
            raise HTTPException(status_code=404, detail="Unknown tenant")

        return tenant

