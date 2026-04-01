# app/schemas/contractors.py

from typing import List, Optional
from pydantic import BaseModel, EmailStr


class ContractorCoverageBase(BaseModel):
    postal_code: str


class ContractorProjectTypeBase(BaseModel):
    project_type: str


class ContractorVerticalPreferenceBase(BaseModel):
    vertical_code: str


class ContractorBase(BaseModel):
    business_name: str
    legal_name: Optional[str] = None
    email_primary: EmailStr
    phone_primary: str
    postal_code: str
    state_code: str
    license_number: str


class ContractorCreate(ContractorBase):
    coverages: List[ContractorCoverageBase] = []
    project_types: List[ContractorProjectTypeBase] = []
    vertical_preferences: List[ContractorVerticalPreferenceBase] = []


class ContractorUpdate(BaseModel):
    business_name: Optional[str] = None
    legal_name: Optional[str] = None
    email_primary: Optional[EmailStr] = None
    phone_primary: Optional[str] = None
    postal_code: Optional[str] = None
    state_code: Optional[str] = None
    license_number: Optional[str] = None


class ContractorRead(ContractorBase):
    id: int

    class Config:
        from_attributes = True


class ContractorCoverageRead(ContractorCoverageBase):
    id: int

    class Config:
        from_attributes = True


class ContractorProjectTypeRead(ContractorProjectTypeBase):
    id: int

    class Config:
        from_attributes = True


class ContractorVerticalPreferenceRead(ContractorVerticalPreferenceBase):
    id: int

    class Config:
        from_attributes = True
