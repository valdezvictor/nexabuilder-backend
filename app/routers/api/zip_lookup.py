from fastapi import APIRouter, HTTPException
import pgeocode

router = APIRouter(prefix="/api", tags=["ZIP Lookup"])

nomi = pgeocode.Nominatim("us")

@router.get("/zip/{zipcode}")
def lookup_zip(zipcode: str):
    result = nomi.query_postal_code(zipcode)

    if result is None or result.place_name is None:
        raise HTTPException(status_code=404, detail="ZIP code not found")

    return {
        "city": result.place_name,
        "state": result.state_code,
        "county": result.county_name,
        "lat": result.latitude,
        "lng": result.longitude,
    }
