from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
from app.db import get_db


router = APIRouter()


# Define Pydantic model for Contractor output
class ContractorOut(BaseModel):
    id: int
    external_id: str | None = None

    business_name: str
    legal_name: str | None = None

    state_code: str
    license_number: str
    license_status: str | None = None
    license_type_id: int | None = None

    years_in_business: int | None = None

    website: str | None = None
    phone_primary: str | None = None
    phone_secondary: str | None = None
    email_primary: str | None = None
    email_secondary: str | None = None

    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    postal_code: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# Extend contractors search view
@router.get("/contractors", response_model=List[ContractorOut])
def search_contractors(
    state: Optional[str] = None,
    trade: Optional[str] = None,
    has_email: Optional[bool] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    qs = db.query(Contractor)

    if state:
        qs = qs.filter(Contractor.state_code == state.upper())

    if has_email is True:
        qs = qs.filter(Contractor.primary_email.isnot(None))
    elif has_email is False:
        qs = qs.filter(Contractor.primary_email.is_(None))

    # trade filtering comes later when your trade M2M is ready

    return qs.limit(limit).all()


# Extend contractor detail view
@router.get("/contractors/{contractor_id}", response_model=ContractorOut)
def contractor_detail(contractor_id: int, db: Session = Depends(get_db)):
    contractor = db.query(Contractor).filter(Contractor.id == contractor_id).first()
    if not contractor:
        raise HTTPException(status_code=404, detail="Contractor not found")
    return contractor

# Extend contractor lead detail view
@router.put("/{contractor_id}/zip-preferences")
def update_zip_preferences(contractor_id: int, payload: dict, db: Session = Depends(get_db)):
    contractor = db.query(Contractor).get(contractor_id)
    if not contractor:
        raise HTTPException(status_code=404, detail="Contractor not found")

    contractor.preferred_zips = payload.get("preferred_zips", [])
    contractor.zip_radius_override = payload.get("zip_radius_override")

    db.commit()
    db.refresh(contractor)
    return contractor
