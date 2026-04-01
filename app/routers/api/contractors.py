from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.db import get_db

from app.schemas.contractors import (
    ContractorCreate,
    ContractorUpdate,
    ContractorRead,
    ContractorCoverageBase,
    ContractorCoverageRead,
    ContractorProjectTypeBase,
    ContractorProjectTypeRead,
    ContractorVerticalPreferenceBase,
    ContractorVerticalPreferenceRead,
)

router = APIRouter(prefix="/api/contractors", tags=["Contractors"])

def get_contractor_or_404(db: Session, contractor_id: int):
    contractor = db.query(Contractor).filter(Contractor.id == contractor_id).first()
    if not contractor:
        raise HTTPException(status_code=404, detail="Contractor not found")
    return contractor


@router.get("/", response_model=list[ContractorRead])
def list_contractors(db: Session = Depends(get_db)):
    return db.query(Contractor).all()


@router.get("/{contractor_id}", response_model=ContractorRead)
def get_contractor(contractor_id: int, db: Session = Depends(get_db)):
    contractor = get_contractor_or_404(db, contractor_id)
    return contractor


@router.post("/", response_model=ContractorRead)
def create_contractor(payload: ContractorCreate, db: Session = Depends(get_db)):
    contractor = Contractor(
        business_name=payload.business_name,
        legal_name=payload.legal_name,
        email_primary=payload.email_primary,
        phone_primary=payload.phone_primary,
        postal_code=payload.postal_code,
        state_code=payload.state_code,
        license_number=payload.license_number,
    )
    db.add(contractor)
    db.flush()

    # Coverages
    for c in payload.coverages:
        db.add(ContractorCoverage(contractor_id=contractor.id, postal_code=c.postal_code))

    # Project types
    for p in payload.project_types:
        db.add(ContractorProjectType(contractor_id=contractor.id, project_type=p.project_type))

    # Vertical preferences
    for v in payload.vertical_preferences:
        db.add(ContractorVerticalPreference(contractor_id=contractor.id, vertical_code=v.vertical_code))

    db.commit()
    db.refresh(contractor)
    return contractor


@router.patch("/{contractor_id}", response_model=ContractorRead)
def update_contractor(
    contractor_id: int,
    payload: ContractorUpdate,
    db: Session = Depends(get_db),
):
    contractor = get_contractor_or_404(db, contractor_id)

    for field, value in payload.model_fields_set.items():
        setattr(contractor, field, getattr(payload, field))

    db.commit()
    db.refresh(contractor)
    return contractor


@router.delete("/{contractor_id}", status_code=204)
def delete_contractor(contractor_id: int, db: Session = Depends(get_db)):
    contractor = get_contractor_or_404(db, contractor_id)
    db.delete(contractor)
    db.commit()
    return


# ---- Coverage CRUD ----

@router.get("/{contractor_id}/coverages", response_model=list[ContractorCoverageRead])
def list_coverages(contractor_id: int, db: Session = Depends(get_db)):
    get_contractor_or_404(db, contractor_id)
    return (
        db.query(ContractorCoverage)
        .filter(ContractorCoverage.contractor_id == contractor_id)
        .all()
    )


@router.post("/{contractor_id}/coverages", response_model=ContractorCoverageRead)
def add_coverage(
    contractor_id: int,
    payload: ContractorCoverageBase,
    db: Session = Depends(get_db),
):
    get_contractor_or_404(db, contractor_id)
    coverage = ContractorCoverage(
        contractor_id=contractor_id,
        postal_code=payload.postal_code,
    )
    db.add(coverage)
    db.commit()
    db.refresh(coverage)
    return coverage


@router.delete("/{contractor_id}/coverages/{coverage_id}", status_code=204)
def delete_coverage(
    contractor_id: int,
    coverage_id: int,
    db: Session = Depends(get_db),
):
    get_contractor_or_404(db, contractor_id)
    coverage = (
        db.query(ContractorCoverage)
        .filter(
            ContractorCoverage.id == coverage_id,
            ContractorCoverage.contractor_id == contractor_id,
        )
        .first()
    )
    if not coverage:
        raise HTTPException(status_code=404, detail="Coverage not found")
    db.delete(coverage)
    db.commit()
    return


# ---- Project Types CRUD ----

@router.get("/{contractor_id}/project-types", response_model=list[ContractorProjectTypeRead])
def list_project_types(contractor_id: int, db: Session = Depends(get_db)):
    get_contractor_or_404(db, contractor_id)
    return (
        db.query(ContractorProjectType)
        .filter(ContractorProjectType.contractor_id == contractor_id)
        .all()
    )


@router.post("/{contractor_id}/project-types", response_model=ContractorProjectTypeRead)
def add_project_type(
    contractor_id: int,
    payload: ContractorProjectTypeBase,
    db: Session = Depends(get_db),
):
    get_contractor_or_404(db, contractor_id)
    pt = ContractorProjectType(
        contractor_id=contractor_id,
        project_type=payload.project_type,
    )
    db.add(pt)
    db.commit()
    db.refresh(pt)
    return pt


@router.delete("/{contractor_id}/project-types/{project_type_id}", status_code=204)
def delete_project_type(
    contractor_id: int,
    project_type_id: int,
    db: Session = Depends(get_db),
):
    get_contractor_or_404(db, contractor_id)
    pt = (
        db.query(ContractorProjectType)
        .filter(
            ContractorProjectType.id == project_type_id,
            ContractorProjectType.contractor_id == contractor_id,
        )
        .first()
    )
    if not pt:
        raise HTTPException(status_code=404, detail="Project type not found")
    db.delete(pt)
    db.commit()
    return


# ---- Vertical Preferences CRUD ----

@router.get("/{contractor_id}/vertical-preferences", response_model=list[ContractorVerticalPreferenceRead])
def list_vertical_preferences(contractor_id: int, db: Session = Depends(get_db)):
    get_contractor_or_404(db, contractor_id)
    return (
        db.query(ContractorVerticalPreference)
        .filter(ContractorVerticalPreference.contractor_id == contractor_id)
        .all()
    )


@router.post("/{contractor_id}/vertical-preferences", response_model=ContractorVerticalPreferenceRead)
def add_vertical_preference(
    contractor_id: int,
    payload: ContractorVerticalPreferenceBase,
    db: Session = Depends(get_db),
):
    get_contractor_or_404(db, contractor_id)
    vp = ContractorVerticalPreference(
        contractor_id=contractor_id,
        vertical_code=payload.vertical_code,
    )
    db.add(vp)
    db.commit()
    db.refresh(vp)
    return vp


@router.delete("/{contractor_id}/vertical-preferences/{vp_id}", status_code=204)
def delete_vertical_preference(
    contractor_id: int,
    vp_id: int,
    db: Session = Depends(get_db),
):
    get_contractor_or_404(db, contractor_id)
    vp = (
        db.query(ContractorVerticalPreference)
        .filter(
            ContractorVerticalPreference.id == vp_id,
            ContractorVerticalPreference.contractor_id == contractor_id,
        )
        .first()
    )
    if not vp:
        raise HTTPException(status_code=404, detail="Vertical preference not found")
    db.delete(vp)
    db.commit()
    return
