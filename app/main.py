import os
import traceback
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, APIRouter
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, generate_latest
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.responses import Response

# ── Routers that actually exist ──────────────────────────────────────────────
from app.api.enrichment import router as enrichment_router
from app.routers.call_center.leads import router as call_center_router
from app.routers.api.trades import router as trades_router
from app.routers.api.zip_lookup import router as zip_router
from app.routers.api.ai import router as ai_router
from app.routers.contractor_leads import router as contractor_leads_router
from app.routers.api.contractors import router as contractors_api_router
from app.routers.contractor_dashboard import router as contractor_dashboard_router
from app.routers.admin_console import router as admin_console_router
from app.routers.admin_ai_insights import router as admin_ai_router
from app.routers.metrics import router as metrics_router
from app.api.routes import admin
from app.routers import auth

# ── DB ───────────────────────────────────────────────────────────────────────
from app.db import test_connection

load_dotenv()

# ── Prometheus ───────────────────────────────────────────────────────────────
REQUEST_COUNT = Counter("http_requests_total", "Total HTTP requests", ["method", "endpoint"])
DB_HEALTH = Gauge("db_health", "Database connection health (1=ok, 0=fail)")

# ── App ──────────────────────────────────────────────────────────────────────
application = FastAPI(title="NexaBuilder API", redirect_slashes=True)

# Static files (only mount if directory exists)
if os.path.isdir("app/static"):
    application.mount("/static", StaticFiles(directory="app/static"), name="static")

# ── Routers ──────────────────────────────────────────────────────────────────
application.include_router(enrichment_router)
application.include_router(call_center_router, prefix="/call-center")
application.include_router(trades_router)
application.include_router(zip_router)
application.include_router(contractor_leads_router)
application.include_router(ai_router)
application.include_router(auth.router)
application.include_router(admin.router)
application.include_router(contractors_api_router)
application.include_router(contractor_dashboard_router)
application.include_router(admin_console_router)
application.include_router(admin_ai_router)
application.include_router(metrics_router)

# ── Prometheus instrumentation ───────────────────────────────────────────────
Instrumentator().instrument(application).expose(application)

# ── Middleware ───────────────────────────────────────────────────────────────
@application.middleware("http")
async def prometheus_middleware(request, call_next):
    response = await call_next(request)
    REQUEST_COUNT.labels(method=request.method, endpoint=request.url.path).inc()
    return response

# ── Health check ─────────────────────────────────────────────────────────────
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

@application.get("/metrics")
async def metrics():
    data = generate_latest(REGISTRY)
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)

@application.get("/")
async def root():
    return {"message": "NexaBuilder API is running"}

@application.get("/debug-path")
async def debug_path():
    import inspect
    import app.main as main_module
    return {
        "main_file": inspect.getfile(main_module),
        "module": main_module.__file__,
    }

# ── FastAPI app alias (some deploy scripts use 'app') ────────────────────────
app = application
