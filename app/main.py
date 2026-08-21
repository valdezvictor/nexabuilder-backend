import os
import traceback
from dotenv import load_dotenv
from fastapi import FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, generate_latest
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.responses import Response
from fastapi.middleware.cors import CORSMiddleware

# ── Only import routers that actually exist on EC2 ───────────────────────────
from app.routers.api.enrichment import router as enrichment_router
from app.routers.api.partner_routing import router as partner_routing_router
from app.routers.api.ping_post import router as ping_post_router
from app.routers.api.ai_intake_agent import router as ai_intake_router
from app.routers.call_center.leads import router as call_center_router
from app.routers.api.trades import router as trades_router
from app.routers.api.zip_lookup import router as zip_router
from app.routers.api.ai import router as ai_router
from app.routers.api.contractors import router as contractors_api_router
from app.routers.api.revenue import router as revenue_router
from app.routers.api.keyword_research import router as keyword_router
from app.routers.api.meta_generator import router as meta_router
from app.routers.api.content_tools import router as content_tools_router
from app.routers.api.seo_pipeline import router as seo_router
from app.routers.api.media_upload import router as media_router
from app.routers.api.leads import router as leads_router
from app.routers.api.routing import router as routing_router
from app.routers.metrics import router as metrics_router
from app.routers import auth
from app.routers.api.magic_link import router as magic_link_router
from app.routers.api.estimate import router as estimate_router
from app.routers.api.service_providers import router as service_provider_router, job_router as service_job_router
from app.routers.api.documents import router as documents_router
from app.routers.api.contractor_matching import router as contractor_matching_router
from app.routers.api.contractor_match import router as contractor_match_router
from app.routers.api.lead_intake import router as lead_intake_router
from app.routers.api.blog import router as blog_router
from app.routers.api.magic_link import router as magic_link_router
from app.routers.api.estimate import router as estimate_router
from app.routers.api.service_providers import router as service_provider_router, job_router as service_job_router
from app.routers.api.documents import router as documents_router
from app.routers.api.contractor_match import router as contractor_match_router
from app.routers.admin_metrics import router as admin_metrics_router
from app.routers.admin_metrics import dashboard_router
from app.routers.api.content import router as content_router, cms_admin_router
from app.routers.api.verify import router as verify_router
from app.db import test_connection

load_dotenv()

# ── Prometheus ───────────────────────────────────────────────────────────────
REQUEST_COUNT = Counter("http_requests_total", "Total HTTP requests", ["method", "endpoint"])
DB_HEALTH = Gauge("db_health", "Database connection health (1=ok, 0=fail)")

# ── App ──────────────────────────────────────────────────────────────────────
application = FastAPI(title="NexaBuilder API", redirect_slashes=False)

# CORS — allow all NexaBuilder tenant origins
application.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://valdezvictor.com",
        "https://nexabuilder.com",
        "https://www.nexabuilder.com",
        "https://unapiscina.com",
        "https://www.unapiscina.com",
        "https://admin.nexabuilder.com",
        "https://contractor.nexabuilder.com",
        "https://call.nexabuilder.com",
        "https://partners.nexabuilder.com",
        "https://member.nexabuilder.com",
        "https://service.nexabuilder.com",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5500",
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
application.include_router(magic_link_router, prefix="/api")
application.include_router(estimate_router)
application.include_router(service_provider_router)
application.include_router(service_job_router)
application.include_router(documents_router)
application.include_router(contractor_matching_router)
application.include_router(contractor_match_router)

# Contractor portal (contractor.nexabuilder.com)
from app.routers.api.contractor_portal import router as contractor_portal_router
application.include_router(contractor_portal_router)

from app.routers.api.contractor_outreach import router as outreach_router
application.include_router(outreach_router)

# Leads router (member portal timeline, my-projects, events)
from app.routers.api.leads import router as leads_api_router
application.include_router(leads_api_router, prefix="/api")
application.include_router(lead_intake_router)
application.include_router(enrichment_router)
application.include_router(partner_routing_router)
application.include_router(ping_post_router)
application.include_router(ai_intake_router)
application.include_router(call_center_router, prefix="/call-center")
application.include_router(trades_router)
application.include_router(zip_router)
application.include_router(ai_router)
application.include_router(leads_router)
application.include_router(routing_router)
application.include_router(metrics_router)
application.include_router(admin_metrics_router, prefix="/api")
application.include_router(contractors_api_router)
application.include_router(revenue_router)
application.include_router(keyword_router)
application.include_router(meta_router)
application.include_router(content_tools_router)
application.include_router(seo_router)
application.include_router(media_router)
application.include_router(dashboard_router)
from app.routers.api.call_center_tools import router as call_tools_router
from app.routers.api.user_management import router as user_mgmt_router
from app.routers.api.twilio_voice import router as twilio_voice_router
from app.routers.api.chat import router as chat_router
application.include_router(call_tools_router, prefix="/api")
application.include_router(user_mgmt_router, prefix="/api")
application.include_router(twilio_voice_router, prefix="/api")
application.include_router(chat_router, prefix="/api")
application.include_router(cms_admin_router)
application.include_router(content_router)
application.include_router(verify_router)
application.include_router(blog_router)

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

from fastapi import Request as _Request

@application.get("/api/debug-headers")
async def debug_headers(request: _Request):
    return dict(request.headers)

from app.routers.api.social_router import router as social_router, tracking_middleware
application.include_router(social_router)
application.middleware(chr(34)+'http'+chr(34))(tracking_middleware)

from app.routers.api.gsc_router import router as gsc_router
application.include_router(gsc_router)

from app.routers.api.bing_router import router as bing_router
application.include_router(bing_router)

from app.routers.api.seo_content_router import router as seo_content_router
from app.routers.api.guides_router import router as guides_router
application.include_router(seo_content_router)
application.include_router(guides_router, prefix="/api/seo-content", tags=["guides"])

from app.routers.api.materials_router import router as materials_router
application.include_router(materials_router)
from app.routers.api.materials_bulk import router as materials_bulk_router
application.include_router(materials_bulk_router)

from app.attribution_middleware import AttributionMiddleware
application.add_middleware(AttributionMiddleware)

from app.routers.api.attribution_router import router as attribution_router
application.include_router(attribution_router)

from app.routers.api.call_tracking_router import router as call_tracking_router
application.include_router(call_tracking_router)

from app.routers.api.keyword_rank_router import router as rank_router
application.include_router(rank_router)

from app.routers.api.instagram_router import router as instagram_router
application.include_router(instagram_router)

from app.routers.api.financing_router import router as financing_router
application.include_router(financing_router)
from app.routers.api.crm_router import router as crm_router
from app.routers.api.contact import _contact_router
application.include_router(crm_router)
app.include_router(_contact_router)
