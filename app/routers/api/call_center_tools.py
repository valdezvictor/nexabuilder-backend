from fastapi import APIRouter, Depends
from app.core.auth import get_current_user
from app.db import get_sessionmaker
from sqlalchemy import text

router = APIRouter()

@router.get("/call-scripts")
async def list_scripts(user: dict = Depends(get_current_user)):
    """Return all active call scripts."""
    S = get_sessionmaker()
    async with S() as db:
        r = await db.execute(text(
            "SELECT script_key, label, content FROM call_scripts "
            "WHERE is_active = TRUE ORDER BY id"
        ))
        return [
            {"key": row[0], "label": row[1], "content": row[2]}
            for row in r.fetchall()
        ]

@router.get("/call-scripts/{key}")
async def get_script(key: str, user: dict = Depends(get_current_user)):
    """Return a specific call script by key."""
    S = get_sessionmaker()
    async with S() as db:
        r = await db.execute(text(
            "SELECT script_key, label, content FROM call_scripts WHERE script_key = :k"
        ), {"k": key})
        row = r.fetchone()
        if not row:
            from fastapi import HTTPException
            raise HTTPException(404, "Script not found")
        return {"key": row[0], "label": row[1], "content": row[2]}

@router.get("/phone-lookup")
async def phone_lookup(phone: str, user: dict = Depends(get_current_user)):
    """
    Look up a phone number in the DB.
    Returns: contractor, lead, or member data if found.
    Normalizes phone format before lookup.
    """
    import re
    # Strip non-digits
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        digits = "1" + digits  # assume US

    S = get_sessionmaker()
    async with S() as db:
        # Check contractors first
        r = await db.execute(text(
            "SELECT id, license_no, business_name, city, county, "
            "classifications, primary_status, phone "
            "FROM contractors "
            "WHERE regexp_replace(phone, '[^0-9]', '', 'g') LIKE :digits "
            "AND primary_status = 'CLEAR' LIMIT 1"
        ), {"digits": "%" + digits[-10:] + "%"})
        contractor = r.fetchone()
        if contractor:
            return {
                "type": "contractor",
                "found": True,
                "data": {
                    "id": contractor[0],
                    "license_no": contractor[1],
                    "business_name": contractor[2],
                    "city": contractor[3],
                    "county": contractor[4],
                    "classifications": contractor[5],
                    "status": contractor[6],
                    "phone": contractor[7],
                },
                "prefill": {
                    "first_name": contractor[2].split()[0] if contractor[2] else "",
                    "last_name":  " ".join(contractor[2].split()[1:]) if contractor[2] else "",
                    "phone": phone,
                    "vertical": _classify_to_vertical(contractor[5] or ""),
                }
            }

        # Check leads (homeowners who have called before)
        r2 = await db.execute(text(
            "SELECT id, first_name, last_name, email, phone, "
            "vertical, project_type, city, postal_code, lead_status "
            "FROM leads "
            "WHERE regexp_replace(phone, '[^0-9]', '', 'g') LIKE :digits "
            "ORDER BY id DESC LIMIT 1"
        ), {"digits": "%" + digits[-10:] + "%"})
        lead = r2.fetchone()
        if lead:
            return {
                "type": "lead",
                "found": True,
                "data": {
                    "id": lead[0], "first_name": lead[1], "last_name": lead[2],
                    "email": lead[3], "phone": lead[4], "vertical": lead[5],
                    "project_type": lead[6], "city": lead[7],
                    "postal_code": lead[8], "status": lead[9]
                },
                "prefill": {
                    "first_name": lead[1] or "", "last_name": lead[2] or "",
                    "email": lead[3] or "", "phone": lead[4] or "",
                    "vertical": lead[5] or "", "project_type": lead[6] or "",
                    "city": lead[7] or "", "postal_code": lead[8] or "",
                }
            }

    return {"type": "unknown", "found": False, "data": None, "prefill": {}}

def _classify_to_vertical(classifications: str) -> str:
    """Map CSLB classification to NexaBuilder vertical."""
    cls = classifications.upper()
    if "C-53" in cls: return "pool"
    if "C-39" in cls: return "roofing"
    if "C-10" in cls: return "electrical"
    if "C-36" in cls: return "plumbing"
    if "C-20" in cls: return "hvac"
    if "C-27" in cls: return "landscaping"
    if "B" in cls:    return "remodel"
    return "general"


@router.get("/twilio/token")
async def get_twilio_token(user: dict = Depends(get_current_user)):
    """Generate a Twilio Voice access token for the softphone."""
    from app.core.twilio_config import generate_twilio_token, get_twilio_config
    
    config = get_twilio_config()
    if not config["configured"]:
        return {
            "token": None,
            "configured": False,
            "phone_number": config["phone_number"],
            "message": "Twilio credentials not yet configured in SSM",
        }
    
    identity = user.get("email", "agent")
    token = await generate_twilio_token(identity)
    return {
        "token": token,
        "configured": True,
        "phone_number": config["phone_number"],
        "identity": identity,
    }

@router.get("/twilio/config")
async def get_twilio_status(user: dict = Depends(get_current_user)):
    """Check Twilio configuration status."""
    from app.core.twilio_config import get_twilio_config
    config = get_twilio_config()
    return {
        "configured": config["configured"],
        "phone_number": config["phone_number"],
        "has_twiml_app": bool(config.get("twiml_app_sid")),
    }
