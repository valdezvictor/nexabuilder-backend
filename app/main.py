import os
import traceback
from dotenv import load_dotenv
from fastapi import FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, generate_latest
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.responses import Response
from fastapi.middleware.cors import CORSMiddleware

# ── Only import routers that actually exist on EC2 ───────────────────────────
from app.api.enrichment import router as enrichment_router
from app.routers.call_center.leads import router as call_center_router
from app.routers.api.trades import router as trades_router
from app.routers.api.zip_lookup import router as zip_router
from app.routers.api.ai import router as ai_router
from app.routers.api.contractors import router as contractors_api_router
from app.routers.api.leads import router as leads_router
from app.routers.api.routing import router as routing_router
from app.routers.metrics import router as metrics_router
from app.routers import auth
from app.routers.admin_metrics import router as admin_metrics_router
from app.routers.admin_metrics import dashboard_router
from app.db import test_connection

load_dotenv()

# ── Prometheus ───────────────────────────────────────────────────────────────
REQUEST_COUNT = Counter("http_requests_total", "Total HTTP requests", ["method", "endpoint"])
DB_HEALTH = Gauge("db_health", "Database connection health (1=ok, 0=fail)")

# ── App ──────────────────────────────────────────────────────────────────────
application = FastAPI(title="NexaBuilder API", redirect_slashes=True)

# CORS — allow all NexaBuilder tenant origins
application.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://admin.nexabuilder.com",
        "https://contractor.nexabuilder.com",
        "https://call.nexabuilder.com",
        "https://partners.nexabuilder.com",
        "https://member.nexabuilder.com",
        "http://localhost:3000",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if os.path.isdir("app/static"):
    from fastapi.staticfiles import StaticFiles
    application.mount("/static", StaticFiles(directory="app/static"), name="static")

# ── Routers ──────────────────────────────────────────────────────────────────
application.include_router(auth.router, prefix="/api")
application.include_router(enrichment_router)
application.include_router(call_center_router, prefix="/call-center")
application.include_router(trades_router)
application.include_router(zip_router)
application.include_router(ai_router)
application.include_router(contractors_api_router)
application.include_router(leads_router)
application.include_router(routing_router)
application.include_router(metrics_router)
application.include_router(admin_metrics_router)
application.include_router(dashboard_router)

# ── Prometheus instrumentation ───────────────────────────────────────────────
Instrumentator().instrument(application).expose(application)

# ── Middleware ───────────────────────────────────────────────────────────────
@application.middleware("http")
async def prometheus_middleware(request, call_next):
    response = await call_next(request)
    REQUEST_COUNT.labels(method=request.method, endpoint=request.url.path).inc()
    return response

# ── Health ───────────────────────────────────────────────────────────────────
async def check_db_health():
    try:
        result = await test_connection()
        return result == 1
    except Exception as e:
        print("HEALTH CHECK ERROR:", e)
        traceback.print_exc()
        return False

@application.get("/health")
async def health():
    ok = await check_db_health()
    DB_HEALTH.set(1 if ok else 0)
    return {"status": "ok" if ok else "db connection failed"}

@application.get("/db-test")
async def db_test():
    result = await test_connection()
    return {"db": result}

@application.get("/metrics-data")
async def metrics_data():
    data = generate_latest(REGISTRY)
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)

@application.get("/")
async def root():
    return {"message": "NexaBuilder API is running"}

app = application
