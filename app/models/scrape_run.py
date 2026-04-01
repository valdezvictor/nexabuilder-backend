from sqlalchemy import Integer, String, Boolean, DateTime, Text, func
from sqlalchemy.orm import mapped_column, relationship
from app.db import Base


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id = mapped_column(Integer, primary_key=True, index=True)
    state_code = mapped_column(String(2), nullable=False, index=True)
    source = mapped_column(String(50), nullable=True)

    started_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at = mapped_column(DateTime(timezone=True), nullable=True)

    success = mapped_column(Boolean, default=False)
    items_fetched = mapped_column(Integer, default=0)
    error_message = mapped_column(Text, nullable=True)

    licenses = relationship("License", back_populates="scrape_run")
