# app/models/trade.py

from sqlalchemy import Integer, String, Table, Column, ForeignKey
from sqlalchemy.orm import mapped_column, relationship
from app.db import Base


# Association table for Contractor ↔ Trade
contractor_trades = Table(
    "contractor_trades",
    Base.metadata,
    Column("contractor_id", ForeignKey("contractors.id"), primary_key=True),
    Column("trade_id", ForeignKey("trades.id"), primary_key=True),
)


class Trade(Base):
    __tablename__ = "trades"

    id = mapped_column(Integer, primary_key=True)
    name = mapped_column(String(100), unique=True, nullable=False)

    leads = relationship("Lead", back_populates="trade")
    contractors = relationship("Contractor", secondary="contractor_trades", back_populates="trades")
