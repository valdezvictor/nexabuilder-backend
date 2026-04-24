import os
import traceback

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, generate_latest
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.responses import Response

from app.api.enrichment import router as enrichment_router
from app.routers.auth import router as auth_router
from app.routers.call_center.leads import router as call_center_router
from app.routers.api.routing import router as routing_router
from app.routers.api.leads import router as leads_router
from app.routers.api.trades import router as trades_router
from app.routers.api.zip_lookup import router as zip_router
from app.routers.api.ai import router as ai_router
from app.routers.api.contractors import router as contractors_api_router
from app.routers.metrics import router as metrics_router

from app.db import test_connection
from app.services.ai_lead_scoring import predict_lead_quality

load_dotenv()

# Prometheus
REQUEST_COUNT = Counter("http_requests_total", "Total HTTP requests", ["method", "endpoint"])
DB_HEALTH = Gauge("db_health", "Database connection health (1=ok, 0=fail)")

application = FastAPI(title="Contractor Scraper API", redirect_slashes=True, root_path="/api")

templates = Jinja2Templates(directory="templates")
application.mount("/static", StaticFiles(directory="app/static"), name="static")

# CORS
application.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "https://nexabuilder.com",
        "https://admin.nexabuilder.com",
        "https://contractor.nexabuilder.com",
        "https://call.nexabuilder.com",
        "https://partners.nexabuilder.com",
        "https://member.nexabuilder.com",
        "https://www.nexabuilder.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
application.include_router(enrichment_router)
application.include_router(call_center_router, prefix="/call-center")
application.include_router(metrics_router)
application.include_router(auth_router)
application.include_router(trades_router)
application.include_router(zip_router)
application.include_router(ai_router)
application.include_router(contractors_api_router)
application.include_router(routing_router)
application.include_router(leads_router)

# Prometheus
Instrumentator().instrument(application).expose(application)

# DB Health
async def check_db_health():
    try:
        result = await test_connection()
        return result == 1
    except Exception:
        return False

@application.get("/health")
async def health():
    ok = await check_db_health()
    DB_HEALTH.set(1 if ok else 0)
    return {"status": "ok" if ok else "db connection failed"}

@application.middleware("http")
async def prometheus_middleware(request, call_next):
    response = await call_next(request)
    REQUEST_COUNT.labels(method=request.method, endpoint=request.url.path).inc()
    return response

@application.get("/metrics")
async def metrics():
    data = generate_latest(REGISTRY)
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)

@application.get("/")
async def root():
    return {"message": "NexaBuilder API is running"}
