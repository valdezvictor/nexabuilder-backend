# app/models/zipcode.py

from sqlalchemy import Integer, String, Float
from sqlalchemy.orm import mapped_column, relationship
from app.db import Base


class ZipCode(Base):
    __tablename__ = "zipcodes"

    id = mapped_column(Integer, primary_key=True)
    zip = mapped_column(String(10), unique=True, nullable=False)
    city = mapped_column(String(100), nullable=True)
    state = mapped_column(String(2), nullable=True)
    county = mapped_column(String(100), nullable=True)

    latitude = mapped_column(Float, nullable=True)
    longitude = mapped_column(Float, nullable=True)
    timezone = mapped_column(String(50), nullable=True)

    leads = relationship("Lead", back_populates="zip")
