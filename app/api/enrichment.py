from fastapi import APIRouter


router = APIRouter()


@router.post("/enrichment/run")
def run_enrichment(limit: int = 100):
    summary = enrich_contractors(limit=limit)
    return summary


@router.post("/enrichment/{contractor_id}/run")
def run_single_enrichment(contractor_id: int):
    summary = enrich_contractors(limit=1)  # later: implement single-target enrichment
    return summary
