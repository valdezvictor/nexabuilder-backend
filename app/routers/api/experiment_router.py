import os
"""
experiment_router.py — CRO A/B experiment tracking
Records impressions and conversions per variant.
"""
from fastapi import APIRouter, Header
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import text as sqlt
import logging

log = logging.getLogger("cro")
router = APIRouter(prefix="/api/experiments", tags=["experiments"])

def _db():
    import os
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        os.getenv("DATABASE_URL","").replace("postgresql+asyncpg","postgresql+psycopg2"),
        pool_pre_ping=True)
    return sessionmaker(bind=engine)()

ADM_KEY = os.getenv("CRM_ADMIN_KEY","")
def _adm(k): 
    if k != ADM_KEY: 
        from fastapi import HTTPException
        raise HTTPException(403,"Forbidden")


class ImpressionPayload(BaseModel):
    experiment_id: str
    variant:       str
    nexa_cid:      Optional[str] = None
    page_path:     Optional[str] = None
    vertical:      Optional[str] = None
    utm_source:    Optional[str] = None


class ConversionPayload(BaseModel):
    experiment_id:    str
    variant:          str
    nexa_cid:         Optional[str] = None
    conversion_value: Optional[float] = None
    vertical:         Optional[str] = None


@router.post("/impression", include_in_schema=False)
def record_impression(payload: ImpressionPayload):
    """Called when a user sees a variant — no auth, public."""
    db = _db()
    try:
        db.execute(sqlt("""
            INSERT INTO ab_experiment_results
              (experiment_id, variant, nexa_cid, page_path,
               event_type, converted, vertical, utm_source)
            VALUES (:eid, :var, :cid, :path,
                    'impression', false, :vert, :src)
        """), {
            "eid":  payload.experiment_id[:80],
            "var":  payload.variant[:10],
            "cid":  payload.nexa_cid,
            "path": payload.page_path,
            "vert": payload.vertical,
            "src":  payload.utm_source,
        })
        db.commit()
        return {"ok": True}
    except Exception as e:
        log.warning(f"Impression record failed: {e}")
        return {"ok": False}
    finally:
        db.close()


@router.post("/conversion", include_in_schema=False)
def record_conversion(payload: ConversionPayload):
    """Mark variant as converted — called on lead submit."""
    db = _db()
    try:
        # Update the most recent impression row for this user+experiment
        db.execute(sqlt("""
            UPDATE ab_experiment_results
            SET converted=true, conversion_value=:val,
                event_type='conversion'
            WHERE id = (
                SELECT id FROM ab_experiment_results
                WHERE experiment_id=:eid
                  AND nexa_cid=:cid
                  AND converted=false
                ORDER BY created_at DESC
                LIMIT 1
            )
        """), {
            "eid": payload.experiment_id,
            "cid": payload.nexa_cid,
            "val": payload.conversion_value,
        })
        db.commit()
        return {"ok": True}
    except Exception as e:
        log.warning(f"Conversion record failed: {e}")
        return {"ok": False}
    finally:
        db.close()


@router.get("/results/{experiment_id}")
def get_results(experiment_id: str, x_admin_key: str = Header(...)):
    """Get A/B results for an experiment — admin only."""
    _adm(x_admin_key)
    db = _db()
    try:
        rows = db.execute(sqlt("""
            SELECT
                variant,
                COUNT(*) FILTER (WHERE event_type='impression') as impressions,
                COUNT(*) FILTER (WHERE converted=true)          as conversions,
                ROUND(
                    COUNT(*) FILTER (WHERE converted=true)::numeric /
                    NULLIF(COUNT(*) FILTER (WHERE event_type='impression'),0) * 100
                , 2) as conversion_rate,
                AVG(conversion_value) FILTER (WHERE converted=true) as avg_value
            FROM ab_experiment_results
            WHERE experiment_id = :eid
            GROUP BY variant
            ORDER BY variant
        """), {"eid": experiment_id}).fetchall()

        results = [dict(r._mapping) for r in rows]

        # Calculate p-value if we have both variants
        if len(results) >= 2:
            try:
                import math
                a = next((r for r in results if r["variant"]=="A"), None)
                b = next((r for r in results if r["variant"]=="B"), None)
                if a and b and a["impressions"] and b["impressions"]:
                    na, ca = int(a["impressions"]), int(a["conversions"] or 0)
                    nb, cb = int(b["impressions"]), int(b["conversions"] or 0)
                    pa, pb = ca/na, cb/nb
                    p_pool = (ca+cb)/(na+nb)
                    if p_pool > 0 and p_pool < 1:
                        se = math.sqrt(p_pool*(1-p_pool)*(1/na+1/nb))
                        z  = abs(pb-pa)/se if se > 0 else 0
                        # Approximate two-tailed p-value
                        p_val = 2*(1 - 0.5*(1+math.erf(z/math.sqrt(2))))
                        for r in results:
                            r["p_value"] = round(p_val, 4)
                            r["significant"] = p_val < 0.05
                            r["winner"] = (pb > pa and r["variant"]=="B" and p_val < 0.05) or                                           (pa > pb and r["variant"]=="A" and p_val < 0.05)
            except Exception: pass

        return {"experiment_id": experiment_id, "variants": results}
    finally:
        db.close()
