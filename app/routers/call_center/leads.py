from fastapi import APIRouter, Form, Depends, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.orm import Session
from app.db import get_db

import re
import time
from app.views.call_center_leads import templates
from app.services.ai_lead_scoring import predict_lead_quality

from datetime import datetime, time, date, timedelta


router = APIRouter()

# ---------- Create lead ----------
@router.post("/leads/create")
async def create_lead(
    request: Request,
    db: Session = Depends(get_db),

    # Personal Info
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(None),
    phone: str = Form(None),

    # Address
    address_line1: str = Form(None),
    address_line2: str = Form(None),
    city: str = Form(...),
    state: str = Form(...),
    postal_code: str = Form(None),

    # Project
    vertical: str = Form(...),
    project_type: str = Form(...),
    description: str = Form(None),

    # Budget
    budget_min: int = Form(...),
    budget_max: int = Form(...),
    funding_option: str = Form("undecided"),
    needs_financing: str = Form(None),

    # Metadata
    source: str = Form(None),
):

    # Normalize vertical
    normalized_vertical = normalize_vertical(db, vertical)

    # Create lead
    new_lead = Lead(
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        address_line1=address_line1,
        address_line2=address_line2,
        city=city,
        state=state,
        postal_code=postal_code,
        vertical=normalized_vertical,
        project_type=project_type,
        description=description,
        budget_min=budget_min,
        budget_max=budget_max,
        funding_option=funding_option,
        source=source,
    )

    # Set needs_financing field
    if needs_financing == "yes":
        new_lead.needs_financing = True
    elif needs_financing == "no":
        new_lead.needs_financing = False
    else:
        new_lead.needs_financing = None

    # Email fallback
    if not new_lead.email:
        new_lead.email = f"noemail+{int(time.time())}@nexabuilder.com"

    db.add(new_lead)
    db.commit()
    db.refresh(new_lead)

    # ---------- RUN ROUTING ENGINE ----------
    route_lead(new_lead, db)

    return RedirectResponse(
        f"/call-center/leads/{new_lead.id}",
        status_code=303
    )

# ------------ API Route Lead ----------
@router.post("/api/route-lead/{lead_id}")
async def api_route_lead(lead_id: int, db: Session = Depends(get_db)):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    # Call your routing engine
    result = route_lead(db, lead)

    db.commit()

    return {
        "tier": lead.routing_tier,
        "contractors": result.contractors if hasattr(result, "contractors") else []
    }

# --------- Lead Contacted ---------
@router.post("/api/mark-contacted/{lead_id}")
async def mark_contacted(lead_id: int, db: Session = Depends(get_db)):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    lead.contacted = True  # ⭐ You already have this field in your model
    db.commit()

    return {"status": "success", "lead_id": lead_id}

# ---------- Lead creation form ----------
@router.get("/leads/create", response_class=HTMLResponse)
async def create_lead_form(request: Request):
    return templates.TemplateResponse(
        "call_center/leads/create.html",
        {"request": request}
    )

# --------- Lead details --------------
@router.get("/leads/{lead_id}", response_class=HTMLResponse)
async def lead_details(request: Request, lead_id: int, db: Session = Depends(get_db)):

    # Load lead
    lead = db.query(Lead).get(lead_id)
    if not lead:
        return HTMLResponse("Lead not found", status_code=404)

    # Load routing logs for this lead
    routing_logs = (
        db.query(RoutingDecisionLog)
        .filter(RoutingDecisionLog.lead_id == lead_id)
        .order_by(RoutingDecisionLog.created_at.desc())
        .all()
    )

    for log in routing_logs:
        if not log.contractor_snapshot:
            log.contractgor_snapshot =[]

    # Load estimate (draft/final)
    estimate = (
        db.query(Estimate)
        .filter(Estimate.lead_id == lead_id)
        .first()
    )

    # Generate call script for this lead
    call_script = get_call_script(lead.vertical, lead)

    # ------------------------------------------------------------
    # ⭐ Contractor Matching + Scoring + Distance
    # ------------------------------------------------------------

    contractors = match_contractors(lead, db)
    scored = score_contractors(lead, contractors)

    for item in scored:
        c = item["contractor"]
        item["distance"] = miles_between(lead.postal_code, c.postal_code)

    # ------------------------------------------------------------
    # ⭐ Use official routing engine scoring
    # ------------------------------------------------------------
    score, breakdown = score_lead(lead)
    tier = determine_tier(score)

    scoring_breakdown = {
        "base": breakdown["base_score"],
        "vertical": breakdown["vertical_score"],
        "funding": breakdown["funding_bonus"],
        "ai": 0,
    }

    # ------------------------------------------------------------
    # Render template with full context
    # ------------------------------------------------------------

    (
        log,
        explanation_list,
        scored_contractors,
        alerts,
        requires_financing_escalation,
        summary,
    ) = route_lead(lead, db)

    ai_summary = build_ai_routing_summary(
        lead=lead,
        summary=summary,
        alerts=alerts,
        scored_contractors=scored_contractors,
    )

    return templates.TemplateResponse(
        "call_center/agent/index.html",
        {
            "routing_alerts": alerts,
            "request": request,
            "lead": lead,
            "routing_logs": routing_logs,
            "call_script": call_script,
            "estimate": estimate,
            "lead_score": score,
            "lead_tier": tier,
            "scoring_breakdown": scoring_breakdown,
            "requires_financing_escalation": requires_financing_escalation,

            # ⭐ Now these variables exist
            "matched_contractors": scored_contractors,
            "explanation_lines": explanation_list,
            "summary": summary,
            "ai_summary": ai_summary,
        },
    )


# ---------- ZIP distance helper ----------
def zip_distance(zip1: str, zip2: str) -> int:
    try:
        return abs(int(zip1) - int(zip2))
    except:
        return 999

# ---------- ZIP to lat/lon helper (using a simple lookup) ----------
def zip_to_latlon(zip_code: str) -> tuple[float, float]:
    """
    Returns (lat, lon) for a ZIP code.
    Assumes you have a ZIP table or external API.
    """

    row = db.query(ZipCode).filter(ZipCode.zip == zip_code).first()
    if not row:
        raise ValueError("ZIP not found")

    return (row.latitude, row.longitude)

# ---------- Scoring ----------
def calculate_score(data: dict, ai_score: int | None = None) -> int:
    score = 0  # <-- FIXED (was base_score)

    vertical = data.get("vertical")
    budget_max = data.get("budget_max") or 0

    AI_WEIGHT = 0.30  # 30% influence

    # If AI score is provided, blend it with the rules-based score
    if ai_score is not None:
        blended = int(score * (1 - AI_WEIGHT) + ai_score * AI_WEIGHT)
        return blended

    # -------------------------
    # Vertical-specific scoring
    # -------------------------
    if vertical == "C-39":  # Roofing
        roof_age = data.get("roof_age")
        if roof_age and int(roof_age) > 15:
            score += 10
        if budget_max >= 15000:
            score += 5

    if vertical == "C-20":  # HVAC
        system_age = data.get("system_age")
        if system_age and int(system_age) > 10:
            score += 8

    if vertical == "C-10":  # Solar
        bill = data.get("electric_bill")
        if bill and int(bill) >= 150:
            score += 10

    # -------------------------
    # Phone scoring
    # -------------------------
    phone = data.get("phone", "")
    digits = "".join(filter(str.isdigit, phone))

    if not digits:
        score -= 10
    elif len(digits) != 10:
        score -= 10
    else:
        area = digits[:3]
        prefix = digits[3:6]

        if area[0] in ["0", "1"] or prefix[0] in ["0", "1"]:
            score -= 10
        else:
            score += 5

        voip_prefixes = {"742", "744", "747", "948"}
        if prefix in voip_prefixes:
            score -= 5

        high_trust_areas = {"714", "949", "650", "408", "512"}
        if area in high_trust_areas:
            score += 3

    # -------------------------
    # Email scoring
    # -------------------------
    email = data.get("email", "")
    email_regex = r"^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$"

    if not email:
        score -= 10
    else:
        if re.match(email_regex, email):
            score += 5
        else:
            score -= 10

        domain = email.split("@")[-1].lower()
        suspicious_domains = {"mailinator.com", "tempmail.com", "10minutemail.com"}
        if domain in suspicious_domains:
            score -= 5

        high_trust_domains = {"gmail.com", "yahoo.com", "outlook.com", "icloud.com"}
        if domain in high_trust_domains:
            score += 3

    # -------------------------
    # Postal code scoring
    # -------------------------
    postal_code = data.get("postal_code")
    if postal_code:
        score += 5
    else:
        score -= 10

    # -------------------------
    # zip scoring
    # -------------------------
    zip_score = score_zip_match(lead_zip, contractor)
    total_score += zip_score
    if zip_score == 10:
        explanations.append("Strong ZIP match (contractor prefers this ZIP).")
    elif zip_score == 5:
        explanations.append(f"Within {contractor.zip_radius_override} mile radius.")


    # -------------------------
    # Funding option scoring
    # -------------------------
    funding_option = data.get("funding_option")
    if funding_option == "financing":
        score += 10

    return score  # <-- FIXED (was base_score)

# ---------- Build AI features ----------
def build_ai_features(data: dict) -> dict:
    phone = data.get("phone", "")
    email = data.get("email", "")
    budget_max = data.get("budget_max") or 0
    vertical = data.get("vertical")

    digits = "".join(filter(str.isdigit, phone))
    has_valid_phone = len(digits) == 10

    has_valid_email = False
    if email:
        import re
        email_regex = r"^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$"
        has_valid_email = bool(re.match(email_regex, email))

    high_budget = budget_max >= 15000

    return {
        "vertical": vertical,
        "has_valid_phone": has_valid_phone,
        "has_valid_email": has_valid_email,
        "high_budget": high_budget,
    }

# ---------- ZIP preference scoring ----------

# ---------- Vertical preference scoring ----------

# ---------- Quality classification ----------
@router.post("/leads/{lead_id}/accept")
async def contractor_accept_lead(
    request: Request,
    lead_id: int,
    contractor_id: int = Form(...),
    db: Session = Depends(get_db)
):
    contractor = db.query(Contractor).get(contractor_id)
    if not contractor:
        raise HTTPException(status_code=404, detail="Contractor not found")

    # Update contractor performance
    contractor.accepted_leads += 1
    contractor.last_accepted_at = datetime.utcnow()
    contractor.acceptance_rate = contractor.accepted_leads / max(
        1, contractor.accepted_leads + contractor.declined_leads
    )

    # Update capacity
    contractor.leads_today += 1
    contractor.leads_this_week += 1

    db.commit()

    return {"status": "accepted"}

# ---------- Contractor reliability scoring ----------

# ---------- Alerts ----------
def generate_alerts(data: dict):
    alerts = []

    phone = data.get("phone", "")
    digits = "".join(filter(str.isdigit, phone))

    if not digits:
        alerts.append("Missing phone")
    elif len(digits) != 10:
        alerts.append("Invalid phone number")
    else:
        area = digits[:3]
        prefix = digits[3:6]

        if area[0] in ["0", "1"]:
            alerts.append("Invalid area code")

        if prefix[0] in ["0", "1"]:
            alerts.append("Invalid prefix")

        voip_prefixes = {"742", "744", "747", "948"}
        if prefix in voip_prefixes:
            alerts.append("Possible VOIP number")

    # Email alerts
    email = data.get("email", "")
    email_regex = r"^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$"

    if not email:
        alerts.append("Missing email")
    else:
        if not re.match(email_regex, email):
            alerts.append("Invalid email format")

        domain = email.split("@")[-1].lower()
        suspicious_domains = {"mailinator.com", "tempmail.com", "10minutemail.com"}
        if domain in suspicious_domains:
            alerts.append("Suspicious email domain")

    return alerts

# ---------- Explanations ----------
def generate_explanations(data: dict, score: int) -> list[str]:
    explanations = []
    vertical = data.get("vertical")
    budget_max = data.get("budget_max")
    phone = data.get("phone")
    email = data.get("email")
    postal_code = data.get("postal_code")

    # Vertical insights
    if vertical == "C-39":
        explanations.append("Roofing lead with indicators of replacement-level need.")
    if vertical == "C-10":
        explanations.append("Solar lead with strong financial indicators (high bill).")
    if vertical == "C-20":
        explanations.append("HVAC lead with aging system likely requiring replacement.")

    # Budget
    if budget_max and budget_max >= 15000:
        explanations.append("Budget suggests a high-value project opportunity.")

    # Contact quality
    if phone:
        explanations.append("Phone number appears structurally valid.")
    else:
        explanations.append("Missing or invalid phone number reduces contactability.")

    if email:
        explanations.append("Email format appears valid.")
    else:
        explanations.append("Missing or invalid email reduces follow-up reliability.")

    # Geography
    if postal_code:
        explanations.append(f"Lead located in ZIP {postal_code}, enabling local contractor matching.")
    else:
        explanations.append("Missing ZIP code reduces geographic match accuracy.")

    # Contractor availability
    if matched_contractors == 0:
        explanations.append("No available contractors currently accepting leads for this vertical.")
    else:
        explanations.append(f"{matched_contractors} contractors currently available for this vertical.")

    # Sample contractor insights
    if contractor_samples:
        for c in contractor_samples:
            reliability = contractor_reliability_score(c)
            if reliability >= 0.8:
                explanations.append(f"{c.name} has a strong acceptance history.")
            elif reliability >= 0.5:
                explanations.append(f"{c.name} has a moderate acceptance history.")
            else:
                explanations.append(f"{c.name} has a low acceptance history, reducing routing confidence.")

        # Capacity insights
        for c in contractor_samples:
            if not contractor_has_capacity(c):
                explanations.append(f"{c.name} is currently at capacity for new leads.")
            else:
                explanations.append(f"{c.name} has remaining capacity for new leads.")

    # ZIP preference insights
    if contractor_samples:
        for c in contractor_samples:
            pref = contractor_zip_preference_score(c, postal_code)
            if pref == 1.0:
                explanations.append(f"{c.name} strongly prefers ZIP {postal_code}.")
            elif pref == 0.5:
                explanations.append(f"{c.name} prefers nearby ZIPs around {postal_code}.")
            else:
                explanations.append(f"{c.name} does not have a ZIP preference match.")

    # Vertical preference insights
    if contractor_samples:
        for c in contractor_samples:
            pref = contractor_vertical_preference_score(c, vertical)
            if pref == 1.0:
                explanations.append(f"{c.name} strongly prefers this project type ({vertical}).")
            else:
                explanations.append(f"{c.name} does not have a strong preference for this project type.")

    # Score summary
    if score >= 90:
        explanations.append("Overall this is a premium-quality lead.")
    elif score >= 70:
        explanations.append("Overall this is a high-quality lead with strong routing confidence.")
    elif score >= 50:
        explanations.append("Lead shows mixed indicators; moderate routing confidence.")
    else:
        explanations.append("Low-quality lead due to weak contact info or low project signals.")

    # At the end, optionally:
    # (We don't call AI here to avoid double work; we just reference the label in the template.)
    return explanations

# ---------- Quality classification ----------
def classify_quality(score: int) -> str:
    if score >= 90:
        return "Premium"
    if score >= 70:
        return "High"
    if score >= 50:
        return "Medium"
    return "Low"
    if matched_contractors == 0:
        return "Low"
    if all(contractor_reliability_score(c) < 0.5 for c in contractor_samples):
        return "Low"
    if any(contractor_reliability_score(c) < 0.3 for c in contractor_samples):
        return "Medium"

# ---------- Contractor matching ----------
def find_matching_contractors(db: Session, vertical: str | None, postal_code: str | None):
    q = db.query(Contractor)

    # Must be active
    q = q.filter(Contractor.is_active == True)

    # Must have valid license
    q = q.filter(Contractor.license_status == "active")

    # Match by trade
    if vertical:
        q = q.join(Contractor.trades).filter_by(code=vertical)

    contractors = q.all()

    # ---------- Availability filtering (PLACE IT HERE) ----------
    contractors = [
        c for c in contractors
        if contractor_is_available(c, vertical)
    ]

    # ---------- Capacity filtering (PLACE IT AFTER AVAILABILITY) ----------
    contractors = [
        c for c in contractors
        if contractor_has_capacity(c)
    ]

    # ---------- Radius filtering ----------
    if postal_code:
        contractors = [
            c for c in contractors
            if c.postal_code and zip_distance(c.postal_code, postal_code) <= 50
        ]

    # Optional: exclude contractors who explicitly do NOT want this ZIP
    contractors = [
        c for c in contractors
        if contractor_zip_preference_score(c, postal_code) >= 0.0
    ]

    # Optional: exclude contractors who explicitly do NOT want this vertical
    contractors = [
        c for c in contractors
        if contractor_vertical_preference_score(c, vertical) >= 0.0
    ]
    contractors = sorted(contractors, key=lambda c: contractor_rank(c, vertical, postal_code), reverse=True)
    return contractors

# ---------- Scoring and ranking ----------
def score_zip_match(lead_zip: str, contractor) -> int:
    """
    Returns ZIP-based score contribution:
    +10 for exact ZIP match
    +5 for within radius override
    +0 otherwise
    """

    if not lead_zip:
        return 0

    # 1. Exact ZIP match
    if contractor.preferred_zips and lead_zip in contractor.preferred_zips:
        return 10

    # 2. Radius override
    if contractor.zip_radius_override:
        try:
            lead_coords = zip_to_latlon(lead_zip)
            contractor_coords = zip_to_latlon(contractor.postal_code)

            distance_miles = geodesic(lead_coords, contractor_coords).miles

            if distance_miles <= contractor.zip_radius_override:
                return 5

        except Exception:
            # ZIP lookup failure → no ZIP score
            return 0

    # 3. No match
    return 0

# ---------- Ranking ----------
def contractor_rank(c, vertical: str | None = None, postal_code: str | None = None):
    availability_score = 1 if contractor_is_available(c, vertical) else 0
    capacity_score = 1 if contractor_has_capacity(c) else 0
    tier_score = {"A": 4, "B": 3, "C": 2, "D": 1}.get(getattr(c, "tier", None), 0)
    reliability = contractor_reliability_score(c)
    zip_pref = contractor_zip_preference_score(c, postal_code)
    vertical_pref = contractor_vertical_preference_score(c, vertical)
    distance = zip_distance(getattr(c, "postal_code", None), postal_code) if postal_code else 999
    acceptance = getattr(c, "acceptance_rate", 0) or 0

    return (
        availability_score,  # highest priority
        capacity_score,      # next priority
        zip_pref,            # geographic fit
        vertical_pref,       # vertical fit
        tier_score,          # tier priority
        reliability,         # reliability score
        acceptance,          # acceptance rate as tiebreaker
        -distance            # closer contractors rank higher
    )


# ---------- Estimate matches for preview ----------
def estimate_matches(data: dict, db: Session):
    vertical = data.get("vertical")
    postal_code = data.get("postal_code")

    contractors = find_matching_contractors(db, vertical=vertical, postal_code=postal_code)
    return len(contractors), contractors[:3]

# ---------- Contractor availability check ----------

# ---------- Contractor capacity check ----------

# ---------- Capacity reset ----------

# ---------- Routing preview ----------
@router.post("/routing/preview", response_class=HTMLResponse)
async def routing_preview(
    request: Request,
    db: Session = Depends(get_db)
):
    form = await request.form()
    data = dict(form)

    for field in ["budget_min", "budget_max", "lead_score", "roof_age", "system_age", "electric_bill"]:
        if field in data and data[field]:
            try:
                data[field] = int(data[field])
            except ValueError:
                pass

    preview = run_routing_engine_preview(data, db)

    return templates.TemplateResponse(
        "call_center/leads/_routing_preview.html",
        {
            "request": request,
            "preview": preview
        }
    )

# ---------- Routing engine preview ----------
def determine_tier(score: int) -> str:
    if score >= 90:
        return "A"
    if score >= 70:
        return "B"
    if score >= 50:
        return "C"
    return "D"

# ---------- Main routing preview function ----------
def run_routing_engine_preview(data: dict, db: Session):
    score = calculate_score(data)
    tier = determine_tier(score)
    alerts = generate_alerts(data)
    matched_contractors, contractor_samples = estimate_matches(data, db)
    explanations = generate_explanations(data, score)
    quality = classify_quality(score)
    # ---- AI lead quality prediction ----
    ai_features = build_ai_features(data)
    ai_result = predict_lead_quality(ai_features)

    score = calculate_score(data, ai_score=ai_result["ai_score"])

    return {
        "score": score,
        "tier": tier,
        "alerts": alerts,
        "matched_contractors": matched_contractors,
        "contractor_samples": contractor_samples,
        "quality": quality,
        "explanations": explanations,
        "ai_score": ai_result["ai_score"],
        "ai_confidence": ai_result["ai_confidence"],
        "ai_label": ai_result["ai_label"],
    }

# ---------- Lead index ----------
@router.get("/leads/", response_class=HTMLResponse)
async def lead_index(request: Request, db: Session = Depends(get_db)):
    leads = (
        db.query(Lead)
        .order_by(Lead.created_at.desc())
        .all()
    )

    return templates.TemplateResponse(
        "call_center/leads/index.html",
        {
            "request": request,
            "leads": leads
        }
    )

# --------- Server Side DataTables ----------
@router.get("/api/leads")
def datatables_leads(request: Request, db: Session = Depends(get_db)):
    draw = int(request.query_params.get("draw", 1))
    start = int(request.query_params.get("start", 0))
    length = int(request.query_params.get("length", 25))

    total = db.query(Lead).count()

    leads = (
        db.query(Lead)
        .order_by(Lead.created_at.desc())
        .offset(start)
        .limit(length)
        .all()
    )

    data = []
    for lead in leads:
        actions_html = f"""
        <div class='desktop-actions d-none d-md-flex gap-2'>
            <button class="btn btn-sm btn-success call-btn" data-phone="{lead.phone}">
                <i class="ti ti-phone"></i>
            </button>
            <button class="btn btn-sm btn-info text-btn" data-phone="{lead.phone}">
               <i class="ti ti-message"></i>
            </button>
            <button class="btn btn-sm btn-warning route-btn" data-lead-id="{lead.id}">
                <i class="ti ti-repeat"></i>
            </button>
            <button class="btn btn-sm btn-secondary contacted-btn" data-lead-id="{lead.id}">
                <i class="ti ti-check"></i>
            </button>
            <a href="/call-center/leads/{lead.id}" class="btn btn-sm btn-primary">
                <i class="ti ti-arrow-right"></i>
            </a>
        </div>

        <div class='quick-actions d-flex d-md-none gap-2'>
            <button class="btn btn-sm btn-success call-btn" data-phone="{lead.phone}">
                <i class="ti ti-phone"></i>
            </button>
            <button class="btn btn-sm btn-info text-btn" data-phone="{lead.phone}">
                <i class="ti ti-message"></i>
            </button>
            <button class="btn btn-sm btn-warning route-btn" data-lead-id="{lead.id}">
                <i class="ti ti-repeat"></i>
            </button>
            <button class="btn btn-sm btn-secondary contacted-btn" data-lead-id="{lead.id}">
                <i class="ti ti-check"></i>
            </button>
            <a href="/call-center/leads/{lead.id}" class="btn btn-sm btn-primary">
                <i class="ti ti-arrow-right"></i>
            </a>
        </div>
        """

        hot_ribbon = ""
        if lead.routing_tier == "HOT":
            hot_ribbon = "<div class='hot-ribbon'>HOT</div>"

        data.append([
            f"<div class='lead-id-cell'>{hot_ribbon}{lead.id}</div>",
            lead.created_at.strftime("%Y-%m-%d %H:%M"),
            f"{lead.first_name} {lead.last_name}",
            lead.vertical,
            lead.postal_code,
            lead.lead_score or "-",
            lead.routing_tier or "-",
            lead.status,
            actions_html
        ])

    return {
        "draw": draw,
        "recordsTotal": total,
        "recordsFiltered": total,
        "data": data,
    }

@router.get("/api/leads/latest-id")
def latest_lead_id(db: Session = Depends(get_db)):
    latest = db.query(Lead).order_by(Lead.id.desc()).first()
    return {"latest_id": latest.id if latest else 0}

@router.get("/api/settings/new-lead-banner")
def get_new_lead_banner_setting(db: Session = Depends(get_db)):
    enabled = get_setting(db, "enable_new_lead_banner")
    return {"enabled": enabled}

#---------- Opt-in history ----------
@router.get("/leads/{lead_id}/optin-history")
def get_optin_history(lead_id: int, db: Session = Depends(get_db)):
    logs = (
        db.query(RoutingDecisionLog)
        .filter(RoutingDecisionLog.lead_id == lead_id)
        .order_by(RoutingDecisionLog.created_at.desc())
        .all()
    )

    return [
        {
            "id": log.id,
            "created_at": log.created_at.isoformat() + "Z",
            "included": log.included,
            "exclusion_reasons": log.exclusion_reasons,
            "ai_score": log.ai_score,
            "ai_label": log.ai_label,
            "ai_confidence": log.ai_confidence,
            "rules_score": log.rules_score,
            "routing_score": log.routing_score,
            "contractor_id": log.contractor_id,
            "contractor_name": log.contractor_name,
            "message": log.message,
        }
        for log in logs
    ]

#---------- Routing explanation endpoint ----------
@router.get("/leads/{lead_id}/routing-explanation")
def routing_explanation(lead_id: int, db: Session = Depends(get_db)):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Lead not found")

    (
        log,
        explanation_list,
        scored_contractors,
        alerts,
        requires_financing_escalation,
        summary,
    ) = route_lead(lead, db)

    ai_summary = build_ai_routing_summary(
        lead=lead,
        summary=summary,
        alerts=alerts,
        scored_contractors=scored_contractors,
    )

    return {
        "explanations": explanation_list,
        "alerts": alerts,
        "summary": summary,
        "ai_summary": ai_summary,
        "contractors": scored_contractors,
    }

#---------- Contractor matches endpoint ----------
@router.get("/leads/{lead_id}/contractor-matches")
def contractor_matches(lead_id: int, db: Session = Depends(get_db)):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Lead not found")

    contractors = match_contractors(lead, db)
    scored = score_contractors(lead, contractors)

    return [
        {
            "contractor_id": item["contractor"].id,
            "name": item["contractor"].business_name,
            "score": item["score"],
            "distance": item.get("distance"),
        }
        for item in scored
    ]

#---------- Contractors list endpoint (for debugging) ----------
@router.get("/contractors")
def cc_list_contractors(db: Session = Depends(get_db)):
    return db.query(Contractor).all()

#---------- Contractor details endpoint (for debugging) ----------
@router.get("/contractors/{contractor_id}")
def cc_get_contractor(contractor_id: int, db: Session = Depends(get_db)):
    contractor = db.query(Contractor).filter(Contractor.id == contractor_id).first()
    if not contractor:
        raise HTTPException(404, "Contractor not found")
    return contractor
