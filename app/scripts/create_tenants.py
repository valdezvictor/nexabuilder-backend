import asyncio
from app.db import get_sessionmaker
from app.models.tenant import Tenant, TenantType

async def create_tenants():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        tenants = [
            Tenant(name="admin-console", domain="admin.nexabuilder.com", type=TenantType.admin),
            Tenant(name="contractor-portal", domain="contractor.nexabuilder.com", type=TenantType.contractor),
            Tenant(name="call-center", domain="call.nexabuilder.com", type=TenantType.agent),
            Tenant(name="partner-portal", domain="partners.nexabuilder.com", type=TenantType.partner),
            Tenant(name="lead-portal", domain="member.nexabuilder.com", type=TenantType.lead),
            Tenant(name="api-root", domain="api.nexabuilder.com", type=TenantType.admin),
        ]

        for tenant in tenants:
            existing = await db.execute(
                Tenant.__table__.select().where(Tenant.domain == tenant.domain)
            )
            existing_row = existing.first()

            if existing_row:
                print(f"Tenant already exists: {tenant.domain}")
            else:
                db.add(tenant)
                print(f"Created tenant: {tenant.domain}")

        await db.commit()
        print("Done.")

if __name__ == "__main__":
    asyncio.run(create_tenants())
