import os
import ssl
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from sqlalchemy import text
from app.core.config import settings

Base = declarative_base()

# --- SSL Context for AWS RDS ---
ssl_context = ssl.create_default_context(cafile="/tmp/rds-ca-bundle.pem")
ssl_context.check_hostname = True
ssl_context.verify_mode = ssl.CERT_REQUIRED

# --- Async Engine (GLOBAL, SINGLE SOURCE OF TRUTH) ---
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=5,
    max_overflow=10,
    connect_args={"ssl": ssl_context},
)

# --- Async Session Factory ---
AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession
)

# --- Dependency for FastAPI ---
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# --- For scripts (like create_admin_user) ---
def get_sessionmaker():
    return AsyncSessionLocal

# --- Optional: Test DB connection ---
async def test_connection():
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        return result.scalar_one()
