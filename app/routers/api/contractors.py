# app/routers/api/contractors.py
# Proper architecture: contractors (CSLB master) LEFT JOIN contractor_accounts (portal layer)
# License format: display as-is from CSLB (numeric) — that IS the CA convention
# Classifications: stored without hyphen in CSLB (C53), strip hyphen on search input
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text
from pydantic import BaseModel
import time as _time
from app.db import get_sessionmaker

router = APIRouter(prefix="/api/admin/contractors", tags=["Admin Contractors"])

# Base SELECT joining contractors + contractor_accounts portal layer
BASE_SELECT = """
    SELECT
        c.id,
        c.license_no,
        c.business_name,
        c.full_business_name,
        c.phone,
        c.email,
        c.city,
        c.state,
        c.county,
        c.zip_code,
        c.classifications,
        c.primary_status,
        c.business_type,
        c.created_at,
        ca.id            AS ca_id,
        ca.license_number AS ca_license_number,
        ca.company_name   AS ca_company_name,
        ca.cslb_verified,
        ca.state_code,
        u.email          AS portal_email,
        CASE
            WHEN ca.id IS NULL THEN 'not_contacted'
            WHEN ca.cslb_verified = true THEN 'active'
            ELSE 'registered'
        END AS portal_status
    FROM contractors c
    LEFT JOIN contractor_accounts ca ON ca.contractor_db_id = c.id
    LEFT JOIN users u ON u.id = ca.user_id
"""

def _row(r) -> dict:
    """Map a joined row to API response dict."""
    # Normalize license display: CSLB stores plain integers, we display as-is
    cslb_license = r[1] or ""
    ca_license = r[15] or ""
    # Use CA portal license if available, else CSLB integer
    display_license = ca_license if ca_license else cslb_license

    # Normalize classifications for display: C53 -> C-53, B -> B etc
    raw_cls = r[10] or ""
    cls_parts = [p.strip() for p in raw_cls.replace("|", " ").split() if p.strip()]
    normalized_cls = []
    for p in cls_parts:
        # If it's like C53, C10, C27 etc — insert hyphen: C-53
        import re
        m = re.match(r'^([A-Za-z]+)(\d+)$', p)
        if m and m.group(1).upper() in ['A','B','C','D']:
            normalized_cls.append(f"{m.group(1).upper()}-{m.group(2)}")
        else:
            normalized_cls.append(p.upper())

    return {
        "id":               r[0],
        "license_no":       cslb_license,
        "license_number":   display_license,
        "business_name":    r[2] or r[3],
        "name":             r[2] or r[3],
        "full_business_name": r[3],
        "phone":            r[4],
        "email":            r[5],
        "city":             r[6],
        "state":            r[7],
        "county":           r[8],
        "zip_code":         r[9],
        "postal_code":      r[9],
        "classifications":  " · ".join(normalized_cls) if normalized_cls else raw_cls,
        "primary_status":   r[11],
        "business_type":    r[12],
        "created_at":       str(r[13]) if r[13] else None,
        "ca_id":            r[14],
        "cslb_verified":    r[17],
        "portal_email":     r[19],
        "portal_status":    r[20],
    }

@router.get("/search")
async def search_contractors(
    q: Optional[str]             = Query(None, description="Smart search: name, license, phone, email, city"),
    classification: Optional[str]= Query(None),
    city: Optional[str]          = Query(None),
    county: Optional[str]        = Query(None),
    zip_code: Optional[str]      = Query(None),
    portal_status: Optional[str] = Query(None),
    limit: int                   = Query(50, le=200),
    offset: int                  = Query(0),
):
    """
    Smart contractor search. All params are optional and combinable.
    q= searches: business_name, license_no, phone, email, city simultaneously.
    classification= accepts both C-53 and C53 formats.
    """
    conds = ["c.primary_status = 'CLEAR'"]
    params: dict = {"limit": limit, "offset": offset}

    if q:
        q_stripped = q.strip()
        p = "%" + q_stripped.upper() + "%"
        # Also try stripping hyphen for classification-style queries
        p_nohyphen = "%" + q_stripped.upper().replace("-", "") + "%"
        conds.append("""(
            UPPER(c.business_name) ILIKE :q
            OR UPPER(c.full_business_name) ILIKE :q
            OR c.license_no ILIKE :q
            OR regexp_replace(c.phone, '[^0-9]', '', 'g') LIKE :q_phone_digits
            OR UPPER(c.email) ILIKE :q
            OR UPPER(c.city) ILIKE :q
            OR UPPER(ca.company_name) ILIKE :q
            OR UPPER(u.email) ILIKE :q
            OR c.classifications ILIKE :q_nohyphen
        )""")
        params["q"] = p
        params["q_plain"] = "%" + q_stripped + "%"
        params["q_nohyphen"] = p_nohyphen
        params["q_phone_digits"] = "%" + "".join(c for c in q_stripped if c.isdigit()) + "%"

    if classification:
        cls_clean = classification.strip().upper().replace("-", "")
        conds.append("c.classifications ILIKE :cls")
        params["cls"] = "%" + cls_clean + "%"

    if city:
        conds.append("UPPER(c.city) ILIKE :city")
        params["city"] = "%" + city.strip().upper() + "%"

    if county:
        conds.append("UPPER(c.county) ILIKE :county")
        params["county"] = "%" + county.strip().upper() + "%"

    if zip_code:
        conds.append("c.zip_code = :zip")
        params["zip"] = zip_code.strip()

    if portal_status:
        if portal_status == "active":
            conds.append("ca.cslb_verified = true")
        elif portal_status == "registered":
            conds.append("ca.id IS NOT NULL AND (ca.cslb_verified IS NULL OR ca.cslb_verified = false)")
        elif portal_status == "not_contacted":
            conds.append("ca.id IS NULL")

    where = " AND ".join(conds)
    sql = f"{BASE_SELECT} WHERE {where} ORDER BY c.business_name LIMIT :limit OFFSET :offset"

    # Count query
    count_sql = f"SELECT COUNT(*) FROM contractors c LEFT JOIN contractor_accounts ca ON ca.contractor_db_id=c.id LEFT JOIN users u ON u.id=ca.user_id WHERE {where}"

    SL = get_sessionmaker()
    async with SL() as db:
        rows = (await db.execute(text(sql), params)).fetchall()
        # Remove limit/offset for count
        count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
        total = (await db.execute(text(count_sql), count_params)).scalar() or 0

    return {"contractors": [_row(r) for r in rows], "total": total}


class RegPayload(BaseModel):
    business_name: str
    legal_name: Optional[str] = None
    entity_type: Optional[str] = None
    email: str
    phone: str
    city: Optional[str] = None
    state: str = "CA"
    postal_code: Optional[str] = None
    license_number: Optional[str] = None
    license_type: Optional[str] = None
    trades: list = []
    service_radius: Optional[int] = 25
    participation_agreement_accepted: bool = False
    participation_agreement_version: str = "2.0"
    track_preference: Optional[str] = "A"

class RegResponse(BaseModel):
    id: Optional[int] = None
    name: str
    email: str
    status: str
    message: str

@router.post("/register", response_model=RegResponse)
async def register_contractor(payload: RegPayload):
    if not payload.participation_agreement_accepted:
        raise HTTPException(status_code=400,
            detail="You must accept the NexaBuilder Contractor Participation Agreement to register.")

    # Normalize license number: strip hyphens, uppercase
    raw_license = (payload.license_number or "").strip()
    if raw_license:
        # Remove any classification prefix (C-53-1089234 -> 1089234 if it starts with letter)
        import re
        # If it's a pure CSLB number (digits only), use as-is
        # If it has letter prefix like C53-1089234, try to extract just the numeric part
        pure_num = re.sub(r'^[A-Za-z0-9\-]+\-(\d{6,7})$', r'\1', raw_license)
        if pure_num.isdigit() and len(pure_num) >= 6:
            license_no_val = pure_num
        else:
            # Store as-is, or generate pending placeholder
            license_no_val = raw_license.upper() if raw_license else "NB-PENDING-" + str(int(_time.time() * 1000))
    else:
        license_no_val = "NB-PENDING-" + str(int(_time.time() * 1000))

    SL = get_sessionmaker()
    async with SL() as db:
        # Check duplicate email
        chk = await db.execute(
            text("SELECT id FROM contractors WHERE LOWER(email)=:e LIMIT 1"),
            {"e": payload.email.lower().strip()}
        )
        if chk.scalar_one_or_none():
            raise HTTPException(status_code=409,
                detail="A contractor account with this email already exists. Use the sign-in link instead.")

        # Also check if CSLB license already exists
        if license_no_val.isdigit():
            lic_chk = await db.execute(
                text("SELECT id FROM contractors WHERE license_no=:ln LIMIT 1"),
                {"ln": license_no_val}
            )
            existing_id = lic_chk.scalar_one_or_none()
            if existing_id:
                # Update existing CSLB record with contact info rather than creating duplicate
                await db.execute(text(
                    "UPDATE contractors SET email=:em, phone=:ph, primary_status='PENDING_REVIEW', updated_at=NOW() WHERE id=:id"
                ), {"em": payload.email.lower().strip(), "ph": payload.phone.strip(), "id": existing_id})
                await db.commit()
                return RegResponse(id=existing_id, name=payload.business_name, email=payload.email,
                    status="pending_review",
                    message="Welcome to NexaBuilder, " + payload.business_name + "! We found your CSLB license in our database. Your account is pending review. Login link will be sent to " + payload.email + " within 1 business day.")

        # Insert new contractor record
        r = await db.execute(text(
            "INSERT INTO contractors (license_no,business_name,full_business_name,email,phone,"
            "city,state,zip_code,business_type,classifications,primary_status,created_at,updated_at) "
            "VALUES (:ln,:bn,:fbn,:em,:ph,:ci,:st,:zc,:bt,:cl,:ps,NOW(),NOW()) RETURNING id"
        ), {
            "ln": license_no_val,
            "bn": payload.business_name.strip(),
            "fbn": payload.legal_name or payload.business_name.strip(),
            "em": payload.email.lower().strip(),
            "ph": payload.phone.strip(),
            "ci": payload.city or "",
            "st": payload.state or "CA",
            "zc": payload.postal_code or "",
            "bt": payload.entity_type or "",
            "cl": (payload.license_type or "").replace("-", "") if payload.license_type else "",
            "ps": "PENDING_REVIEW",
        })
        new_id = r.scalar_one_or_none()
        await db.commit()

    return RegResponse(id=new_id, name=payload.business_name, email=payload.email,
        status="pending_review",
        message="Welcome to NexaBuilder, " + payload.business_name + "! Your account is pending review. "
                "We will send a login link to " + payload.email + " once your CSLB license is verified — typically within 1 business day.")


@router.get("/")
async def list_contractors(limit: int=Query(100,le=500), offset: int=Query(0)):
    SL = get_sessionmaker()
    async with SL() as db:
        rows = (await db.execute(text(
            f"{BASE_SELECT} WHERE c.primary_status='CLEAR' ORDER BY c.business_name LIMIT :limit OFFSET :offset"
        ), {"limit": limit, "offset": offset})).fetchall()
    return [_row(r) for r in rows]


@router.get("/{contractor_id}")
async def get_contractor(contractor_id: int):
    SL = get_sessionmaker()
    async with SL() as db:
        row = (await db.execute(text(
            f"{BASE_SELECT} WHERE c.id=:id LIMIT 1"
        ), {"id": contractor_id})).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Contractor not found")
    return _row(row)


@router.patch("/{contractor_id}")
async def update_contractor(contractor_id: int, payload: dict):
    allowed = {"primary_status", "business_name", "full_business_name",
               "email", "phone", "city", "state", "zip_code", "classifications"}
    sets = ", ".join(f"{k}=:{k}" for k in payload if k in allowed)
    if not sets:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    params = {k: v for k, v in payload.items() if k in allowed}
    params["id"] = contractor_id
    SL = get_sessionmaker()
    async with SL() as db:
        await db.execute(text(
            f"UPDATE contractors SET {sets}, updated_at=NOW() WHERE id=:id"
        ), params)
        await db.commit()
    return {"id": contractor_id, "status": "updated"}


@router.delete("/{contractor_id}", status_code=204)
async def delete_contractor(contractor_id: int):
    SL = get_sessionmaker()
    async with SL() as db:
        await db.execute(text("DELETE FROM contractors WHERE id=:id"), {"id": contractor_id})
        await db.commit()
    return
