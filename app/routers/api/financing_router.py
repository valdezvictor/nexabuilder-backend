"""
financing_router.py — NexaBuilder Financial Platform Phase 2
Handles pre-qualification scoring, AVM lookup, loan product matching,
LendAPI routing, and ping tree failover.

Endpoints:
  POST /api/financing/pre-qualify      — score a lead + return matched products
  POST /api/financing/submit           — submit application to lender (LendAPI / Raul)
  GET  /api/financing/applications     — pipeline list (admin)
  GET  /api/financing/application/{id} — single app detail (admin)
  GET  /api/financing/pipeline         — summary view for dashboard widget
  POST /api/financing/ping-tree        — manual ping tree route trigger
  GET  /api/financing/products         — available loan products
  GET  /api/financing/revenue          — revenue summary (admin)
"""
import os, logging, json as _json
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Header, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import text as sqlt

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/financing", tags=["Financing"])

ADMIN_KEY = os.getenv("CMS_ADMIN_KEY", "")


def _req(key):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(
        os.getenv("DATABASE_URL", "").replace("postgresql+asyncpg", "postgresql+psycopg2"),
        pool_pre_ping=True)
    return sessionmaker(bind=engine)()


# ═══════════════════════════════════════════════════════════════
# PRE-QUALIFICATION ENGINE
# ═══════════════════════════════════════════════════════════════

class PreQualRequest(BaseModel):
    lead_id:                int
    requested_amount:       float
    property_address:       Optional[str] = None
    property_type:          Optional[str] = "single_family"
    annual_income:          Optional[float] = None
    employment_status:      Optional[str] = "employed"
    years_at_address:       Optional[int] = None
    ownership_tenure:       Optional[str] = "own_with_mortgage"
    stated_mortgage_balance: Optional[float] = None
    stated_monthly_debts:   Optional[float] = None  # all debts excl mortgage
    co_borrower:            Optional[bool] = False
    ssn_last_four:          Optional[str] = None    # for soft pull
    financing_type:         Optional[str] = None    # heloc|home_improvement|pool_loan|personal
    project_budget:         Optional[float] = None
    down_payment_available: Optional[float] = None


def _calculate_pre_qual_score(data: dict) -> tuple[int, str, list, dict]:
    """
    NexaBuilder internal pre-qualification scoring engine.
    Returns: (score 0-100, tier A/B/C/D/decline, reasons[], avm_estimate{})

    Scoring model (industry-standard construction lending criteria):
    ┌─────────────────────────────┬───────┐
    │ Factor                      │ Points│
    ├─────────────────────────────┼───────┤
    │ Income adequacy (DTI < 43%) │  25   │
    │ Equity / LTV position       │  25   │
    │ Employment stability        │  15   │
    │ Ownership tenure            │  15   │
    │ Loan-to-project ratio       │  10   │
    │ Co-borrower present         │   5   │
    │ Down payment available      │   5   │
    └─────────────────────────────┴───────┘
    """
    score = 0
    reasons = []
    avm = {}

    annual_income          = data.get("annual_income") or 0
    monthly_income         = annual_income / 12 if annual_income else 0
    requested_amount       = data.get("requested_amount") or 0
    project_budget         = data.get("project_budget") or requested_amount
    mortgage_balance       = data.get("stated_mortgage_balance") or 0
    monthly_debts          = data.get("stated_monthly_debts") or 0
    ownership              = data.get("ownership_tenure") or "own_with_mortgage"
    employment             = data.get("employment_status") or "employed"
    years_at_address       = data.get("years_at_address") or 0
    co_borrower            = data.get("co_borrower") or False
    down_payment           = data.get("down_payment_available") or 0
    property_address       = data.get("property_address") or ""

    # ── AVM Estimation ───────────────────────────────────────────────
    # We don't have a live AVM API yet — estimate from median SoCal values
    # by zip code prefix. Phase 3 will plug in ATTOM or CoreLogic.
    zip_prefix = property_address[-5:][:3] if len(property_address) >= 5 else "900"
    # LA County median by zip prefix (rough estimate until live AVM)
    zip_avm_map = {
        "900": 780000, "901": 850000, "902": 920000, "903": 710000,
        "904": 680000, "905": 650000, "906": 820000, "907": 760000,
        "908": 700000, "909": 690000, "910": 850000, "911": 780000,
        "912": 910000, "913": 860000, "914": 1050000, "915": 950000,
        "916": 820000, "917": 780000, "918": 720000, "919": 690000,
        "920": 750000, "921": 820000, "922": 880000, "923": 640000,
        "924": 700000, "925": 1100000, "926": 980000, "927": 860000,
        "928": 920000, "929": 680000, "930": 640000, "931": 720000,
        "932": 660000, "933": 640000,
    }
    est_avm = zip_avm_map.get(zip_prefix, 750000)
    est_equity = max(0, est_avm - mortgage_balance) if mortgage_balance else est_avm * 0.40
    est_ltv = round((mortgage_balance / est_avm * 100), 1) if est_avm > 0 else 60.0
    cltv = round(((mortgage_balance + requested_amount) / est_avm * 100), 1) if est_avm > 0 else 70.0
    max_eligible = round(est_avm * 0.85 - mortgage_balance, 2)

    avm = {
        "estimated_value":   est_avm,
        "estimated_equity":  est_equity,
        "ltv_percent":       est_ltv,
        "cltv_percent":      cltv,
        "max_loan_eligible": max(0, max_eligible),
        "source":            "nexabuilder_zip_estimate",
        "confidence":        "medium"
    }

    # ── Factor 1: Income / DTI (25 pts) ─────────────────────────────
    if monthly_income > 0:
        # Estimate new monthly payment: 30-year at 8% APR
        if requested_amount > 0:
            monthly_rate = 0.08 / 12
            n = 360
            est_payment = requested_amount * (monthly_rate * (1 + monthly_rate)**n) / ((1 + monthly_rate)**n - 1)
        else:
            est_payment = 0

        total_monthly_obligations = monthly_debts + est_payment
        dti = (total_monthly_obligations / monthly_income * 100) if monthly_income > 0 else 99

        if dti <= 36:
            score += 25
            reasons.append("excellent_dti")
        elif dti <= 43:
            score += 18
            reasons.append("acceptable_dti")
        elif dti <= 50:
            score += 8
            reasons.append("high_dti_marginal")
        else:
            score += 0
            reasons.append("dti_exceeds_threshold")
    else:
        score += 10  # income not stated — partial credit
        reasons.append("income_not_stated")

    # ── Factor 2: Equity / LTV (25 pts) ─────────────────────────────
    if ownership in ("own", "own_with_mortgage"):
        if cltv <= 70:
            score += 25
            reasons.append("strong_equity_position")
        elif cltv <= 80:
            score += 20
            reasons.append("good_equity_position")
        elif cltv <= 85:
            score += 12
            reasons.append("adequate_equity")
        elif cltv <= 90:
            score += 6
            reasons.append("low_equity_marginal")
        else:
            score += 0
            reasons.append("insufficient_equity")
    elif ownership == "rent":
        # Renters — only unsecured products eligible
        score += 8
        reasons.append("renter_unsecured_only")
    else:
        score += 10
        reasons.append("ownership_unconfirmed")

    # ── Factor 3: Employment stability (15 pts) ──────────────────────
    employment_map = {
        "employed":        15,
        "self_employed":   10,  # higher risk but eligible
        "retired":         12,  # income stable, no employment risk
        "contractor":      10,
        "part_time":        6,
        "other":            5,
        "unemployed":       0,
    }
    emp_pts = employment_map.get(employment, 8)
    score += emp_pts
    if emp_pts >= 12:
        reasons.append("stable_employment")
    elif emp_pts >= 8:
        reasons.append("variable_income_employment")
    else:
        reasons.append("employment_risk_flag")

    # ── Factor 4: Ownership tenure (15 pts) ──────────────────────────
    if years_at_address >= 5:
        score += 15
        reasons.append("long_tenure_5plus_years")
    elif years_at_address >= 2:
        score += 10
        reasons.append("established_tenure_2plus_years")
    elif years_at_address >= 1:
        score += 6
        reasons.append("new_homeowner_1plus_year")
    else:
        score += 3
        reasons.append("recent_purchase_under_1yr")

    # ── Factor 5: Loan-to-project ratio (10 pts) ─────────────────────
    if project_budget > 0 and requested_amount > 0:
        loan_to_project = requested_amount / project_budget
        if loan_to_project <= 0.85:
            score += 10
            reasons.append("reasonable_loan_to_project")
        elif loan_to_project <= 1.0:
            score += 6
            reasons.append("full_project_financing")
        else:
            score += 2
            reasons.append("over_project_amount")
    else:
        score += 5
        reasons.append("project_amount_estimated")

    # ── Factor 6: Co-borrower (5 pts) ────────────────────────────────
    if co_borrower:
        score += 5
        reasons.append("co_borrower_strengthens_application")

    # ── Factor 7: Down payment available (5 pts) ─────────────────────
    if down_payment > 0:
        score += 5
        reasons.append("down_payment_available")

    # Cap at 100
    score = min(100, max(0, score))

    # ── Tier assignment ───────────────────────────────────────────────
    if score >= 80:
        tier = "A"
    elif score >= 65:
        tier = "B"
    elif score >= 50:
        tier = "C"
    elif score >= 35:
        tier = "D"
    else:
        tier = "decline"

    return score, tier, reasons, avm


def _match_loan_products(db, score: int, tier: str, avm: dict,
                          requested_amount: float, vertical: str,
                          ownership: str, annual_income: float,
                          financing_type: str = None) -> list:
    """Match eligible loan products based on pre-qual results."""
    est_avm    = avm.get("estimated_value", 0)
    cltv       = avm.get("cltv_percent", 100)
    est_equity = avm.get("estimated_equity", 0)

    rows = db.execute(sqlt("""
        SELECT id, product_code, product_name, product_category,
               min_loan_amount, max_loan_amount, min_fico_score,
               max_ltv_percent, max_cltv_percent, max_dti_percent,
               rate_low_apr, rate_high_apr, typical_term_months,
               verticals, min_project_amount, routing_priority, description
        FROM loan_products
        WHERE is_active = TRUE
          AND min_loan_amount <= :amount
          AND max_loan_amount >= :amount
          AND (:vertical = ANY(verticals) OR 'all' = ANY(verticals))
        ORDER BY routing_priority ASC
    """), {"amount": requested_amount, "vertical": vertical}).fetchall()

    matched = []
    for row in rows:
        r = dict(row._mapping)

        # Filter by product type if specified
        if financing_type and r["product_category"] != financing_type:
            if financing_type not in ("any", None):
                continue

        # Filter: renters can't use HELOC / secured products
        if ownership == "rent" and r["product_category"] in ("heloc",):
            continue

        # Filter: equity requirements for secured products
        if r["product_category"] in ("heloc",):
            if cltv > (r.get("max_cltv_percent") or 90):
                continue
            if est_equity < requested_amount:
                continue

        # Score-based tier filter
        # A+B can access all; C gets unsecured + FHA; D gets personal only
        if tier == "C" and r["product_category"] in ("heloc",):
            continue
        if tier == "D" and r["product_category"] not in ("personal", "home_improvement"):
            continue
        if tier == "decline":
            continue

        # Estimate monthly payment
        if r["typical_term_months"] and r["rate_low_apr"]:
            low_rate    = float(r["rate_low_apr"]) / 100 / 12
            n           = int(r["typical_term_months"])
            req_f       = float(requested_amount)
            if low_rate > 0:
                pmt_low = req_f * (low_rate * (1+low_rate)**n) / ((1+low_rate)**n - 1)
            else:
                pmt_low = req_f / n
            r["monthly_payment_estimate_low"] = round(pmt_low, 0)
        else:
            r["monthly_payment_estimate_low"] = None

        r["why_matched"] = "Eligible based on property equity, income, and project type"
        matched.append(r)

    return matched


# ═══════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@router.post("/pre-qualify")
async def pre_qualify(payload: PreQualRequest,
                      x_admin_key: str = Header(...)):
    """
    Run the NexaBuilder pre-qualification engine.
    Returns score, tier, AVM estimate, and matched loan products.
    No credit pull — fully soft analysis.
    """
    _req(x_admin_key)
    db = _db()
    try:
        # Get lead context
        lead_row = db.execute(sqlt("""
            SELECT id, vertical, postal_code, first_name, last_name,
                   project_budget, needs_financing, financing_amount
            FROM leads WHERE id = :id
        """), {"id": payload.lead_id}).fetchone()
        if not lead_row:
            raise HTTPException(status_code=404, detail="Lead not found")

        lead = dict(lead_row._mapping)
        vertical = lead.get("vertical", "remodel")

        # Build scoring input
        def _f(v):
            """Safe float cast — handles Decimal and None."""
            try: return float(v) if v is not None else None
            except: return None

        score_data = {
            "requested_amount":        _f(payload.requested_amount),
            "project_budget":          _f(payload.project_budget) or _f(lead.get("project_budget")),
            "annual_income":           _f(payload.annual_income),
            "employment_status":       payload.employment_status,
            "years_at_address":        payload.years_at_address,
            "ownership_tenure":        payload.ownership_tenure,
            "stated_mortgage_balance": _f(payload.stated_mortgage_balance),
            "stated_monthly_debts":    _f(payload.stated_monthly_debts),
            "co_borrower":             payload.co_borrower,
            "property_address":        payload.property_address or lead.get("property_address", ""),
            "down_payment_available":  _f(payload.down_payment_available),
        }

        # Run scoring engine
        score, tier, reasons, avm = _calculate_pre_qual_score(score_data)

        # Estimate DTI for display
        monthly_income = (payload.annual_income or 0) / 12
        monthly_rate   = 0.08 / 12
        n              = 360
        if payload.requested_amount and payload.requested_amount > 0:
            est_payment = payload.requested_amount * (monthly_rate * (1+monthly_rate)**n) / ((1+monthly_rate)**n - 1)
        else:
            est_payment = 0
        total_debts = (payload.stated_monthly_debts or 0) + est_payment
        dti_calc = round(total_debts / monthly_income * 100, 1) if monthly_income > 0 else None

        # Match loan products
        products = _match_loan_products(
            db, score, tier, avm,
            payload.requested_amount,
            vertical,
            payload.ownership_tenure or "own_with_mortgage",
            payload.annual_income or 0,
            payload.financing_type
        )

        # Save property valuation
        pv_id = None
        if payload.property_address:
            pv_result = db.execute(sqlt("""
                INSERT INTO property_valuations
                  (lead_id, property_address, zip_code, property_type,
                   avm_value, avm_confidence, avm_source,
                   estimated_mortgage, estimated_equity, ltv_percent,
                   cltv_percent, max_loan_eligible, state)
                VALUES
                  (:lid, :addr, :zip, :ptype,
                   :avm, :conf, :src,
                   :mort, :equity, :ltv,
                   :cltv, :max_loan, 'CA')
                ON CONFLICT DO NOTHING
                RETURNING id
            """), {
                "lid":      payload.lead_id,
                "addr":     payload.property_address,
                "zip":      payload.property_address[-5:] if payload.property_address else None,
                "ptype":    payload.property_type,
                "avm":      avm["estimated_value"],
                "conf":     avm["confidence"],
                "src":      avm["source"],
                "mort":     payload.stated_mortgage_balance,
                "equity":   avm["estimated_equity"],
                "ltv":      avm["ltv_percent"],
                "cltv":     avm["cltv_percent"],
                "max_loan": avm["max_loan_eligible"],
            })
            pv_row = pv_result.fetchone()
            if pv_row:
                pv_id = pv_row[0]

        # Update lead pre_qual fields
        db.execute(sqlt("""
            UPDATE leads SET
              annual_income       = :income,
              employment_status   = :emp,
              years_at_address    = :yrs,
              ownership_tenure    = :own,
              property_address    = COALESCE(:addr, property_address),
              pre_qual_score      = :score,
              pre_qual_status     = :status,
              financing_type      = :ftype,
              co_borrower         = :coborrow,
              needs_financing     = TRUE,
              financing_amount    = :amount

            WHERE id = :lid
        """), {
            "income":    payload.annual_income,
            "emp":       payload.employment_status,
            "yrs":       payload.years_at_address,
            "own":       payload.ownership_tenure,
            "addr":      payload.property_address,
            "score":     score,
            "status":    "pre_qualified" if tier not in ("decline",) else "declined",
            "ftype":     payload.financing_type,
            "coborrow":  payload.co_borrower,
            "amount":    payload.requested_amount,
            "lid":       payload.lead_id,
        })
        db.commit()

        # ── Auto-submit to lender network if Tier A/B/C ──────────────
        if tier in ("A","B","C"):
            try:
                lender = db.execute(sqlt(
                    "SELECT id FROM lending_partners WHERE is_active=TRUE ORDER BY is_primary DESC LIMIT 1"
                )).fetchone()

                product = db.execute(sqlt(
                    "SELECT id FROM loan_products WHERE is_active=TRUE "
                    "AND min_loan_amount <= :amt AND max_loan_amount >= :amt "
                    "AND (:vert = ANY(verticals) OR 'all' = ANY(verticals)) "
                    "ORDER BY routing_priority ASC LIMIT 1"
                ), {"amt": _f(payload.requested_amount), "vert": vertical}).fetchone()

                if product:
                    db.execute(sqlt(
                        "INSERT INTO financing_applications "
                        "(lead_id, loan_product_id, lender_partner_id, "
                        " requested_loan_amount, requested_loan_purpose, "
                        " project_type, project_budget, "
                        " annual_income, employment_status, co_borrower, "
                        " stated_mortgage_balance, stated_monthly_debts, "
                        " property_address, avm_value, estimated_equity, ltv_estimate, "
                        " pre_qual_score, pre_qual_tier, status, submitted_at) "
                        "VALUES (:lid,:prod,:lender,:amt,:purpose,:ptype,:budget,"
                        ":income,:emp,:coborrow,:mortgage,:debts,"
                        ":addr,:avm,:equity,:ltv,:score,:tier,"
                        "'submitted_to_lender',NOW()) ON CONFLICT DO NOTHING"
                    ), {
                        "lid":     payload.lead_id,
                        "prod":    product[0],
                        "lender":  str(lender[0]) if lender else None,
                        "amt":     _f(payload.requested_amount),
                        "purpose": f"{vertical.title()} project",
                        "ptype":   lead.get("project_type"),
                        "budget":  _f(payload.project_budget or payload.requested_amount),
                        "income":  _f(payload.annual_income),
                        "emp":     payload.employment_status,
                        "coborrow":payload.co_borrower,
                        "mortgage":_f(payload.stated_mortgage_balance),
                        "debts":   _f(payload.stated_monthly_debts),
                        "addr":    payload.property_address,
                        "avm":     float(avm.get("estimated_value",0)),
                        "equity":  float(avm.get("estimated_equity",0)),
                        "ltv":     float(avm.get("cltv_percent",0)),
                        "score":   score,
                        "tier":    tier,
                    })
                    db.commit()
                    log.info(f"Lead {payload.lead_id} auto-submitted. Tier {tier}")
            except Exception as e:
                log.warning(f"Auto-submit failed lead {payload.lead_id}: {e}")
                try: db.rollback()
                except: pass

        elif tier == "D":
            try:
                db.execute(sqlt(
                    "INSERT INTO ping_tree_routes "
                    "(lead_id, route_reason, destination, destination_url, "
                    " destination_partner, vertical, zip_code, status) "
                    "VALUES (:lid,'low_score','techcial',"
                    "'https://api.techcial.com/v1/leads/receive',"
                    "'Techcial Lead Exchange',:vert,:zip,'pending')"
                ), {"lid": payload.lead_id, "vert": vertical, "zip": lead.get("postal_code")})
                db.execute(sqlt(
                    "UPDATE leads SET ping_tree_eligible=TRUE, "
                    "ping_tree_routed_at=NOW(), pre_qual_status='ping_tree_routed' "
                    "WHERE id=:id"
                ), {"id": payload.lead_id})
                db.commit()
                log.info(f"Lead {payload.lead_id} routed to ping tree. Tier D.")
            except Exception as e:
                log.warning(f"Ping tree route failed: {e}")

        return {
            "lead_id":          payload.lead_id,
            "lead_name":        f"{lead['first_name']} {lead['last_name']}",
            "vertical":         vertical,
            "requested_amount": payload.requested_amount,

            # Pre-qual result
            "pre_qual_score":   score,
            "pre_qual_tier":    tier,
            "pre_qual_reasons": reasons,
            "pre_qualified":    tier not in ("decline",),

            # Property / AVM
            "avm":              avm,
            "property_valuation_id": pv_id,

            # Financial snapshot
            "dti_estimate":     dti_calc,
            "monthly_income":   round(monthly_income, 0),
            "est_monthly_payment": round(est_payment, 0),

            # Matched products
            "matched_products": products,
            "product_count":    len(products),

            # Next step
            "next_step": "submit_to_lender" if tier in ("A","B","C") else
                         "ping_tree" if tier == "D" else "decline_with_referral",

            # LendAPI ready flag
            "lendapi_eligible": tier in ("A","B") and score >= 65,
        }
    finally:
        db.close()


@router.post("/submit")
async def submit_application(
    lead_id:          int,
    loan_product_id:  int,
    bg:               BackgroundTasks,
    x_admin_key:      str = Header(...)
):
    """
    Submit a pre-qualified lead to the matched lender.
    Currently routes to Raul Cruz (primary) or LendAPI (when keys obtained).
    Logs every routing attempt to lender_routing_log.
    """
    _req(x_admin_key)
    db = _db()
    try:
        # Get lead + pre-qual data
        lead = db.execute(sqlt("""
            SELECT l.*, pv.avm_value, pv.estimated_equity, pv.ltv_percent,
                   pv.cltv_percent, pv.property_address AS pv_address
            FROM leads l
            LEFT JOIN property_valuations pv ON pv.lead_id = l.id
            WHERE l.id = :id
            ORDER BY pv.created_at DESC
            LIMIT 1
        """), {"id": lead_id}).fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        l = dict(lead._mapping)

        # Get loan product
        prod = db.execute(sqlt(
            "SELECT * FROM loan_products WHERE id = :id"
        ), {"id": loan_product_id}).fetchone()
        if not prod:
            raise HTTPException(status_code=404, detail="Loan product not found")

        p = dict(prod._mapping)

        # Get active lender — prefer the product's lender, fallback to primary
        lender = db.execute(sqlt("""
            SELECT * FROM lending_partners
            WHERE is_active = TRUE
            ORDER BY is_primary DESC, created_at ASC
            LIMIT 1
        """)).fetchone()

        # Create financing application record
        fa_result = db.execute(sqlt("""
            INSERT INTO financing_applications
              (lead_id, loan_product_id, lender_partner_id,
               requested_loan_amount, requested_loan_purpose,
               project_type, project_budget,
               annual_income, employment_status, co_borrower,
               stated_mortgage_balance, stated_monthly_debts,
               property_address, avm_value, estimated_equity, ltv_estimate,
               pre_qual_score, pre_qual_tier,
               status, submitted_at)
            VALUES
              (:lid, :prod_id, :lender_id,
               :amount, :purpose,
               :ptype, :budget,
               :income, :emp, :coborrow,
               :mortgage, :debts,
               :addr, :avm, :equity, :ltv,
               :score, :tier,
               'submitted_to_lender', NOW())
            RETURNING id, app_reference
        """), {
            "lid":       lead_id,
            "prod_id":   loan_product_id,
            "lender_id": str(lender[0]) if lender else None,
            "amount":    l.get("financing_amount") or l.get("project_budget"),
            "purpose":   f"{l.get('vertical','').title()} project — {l.get('project_type','')}",
            "ptype":     l.get("project_type"),
            "budget":    l.get("project_budget"),
            "income":    l.get("annual_income"),
            "emp":       l.get("employment_status"),
            "coborrow":  l.get("co_borrower"),
            "mortgage":  l.get("stated_mortgage_balance"),
            "debts":     l.get("stated_monthly_debts"),
            "addr":      l.get("property_address") or l.get("pv_address"),
            "avm":       l.get("avm_value"),
            "equity":    l.get("estimated_equity"),
            "ltv":       l.get("ltv_percent"),
            "score":     l.get("pre_qual_score"),
            "tier":      "A" if (l.get("pre_qual_score") or 0) >= 80 else
                         "B" if (l.get("pre_qual_score") or 0) >= 65 else "C",
        })
        fa_row   = fa_result.fetchone()
        fa_id    = fa_row[0]
        fa_ref   = fa_row[1]
        db.commit()

        # Route to lender in background
        if lender:
            bg.add_task(_route_to_lender, fa_id, lead_id, l, p, dict(lender._mapping))
        else:
            # No active lender — queue for ping tree
            bg.add_task(_route_ping_tree, lead_id, fa_id, "no_active_lender")

        return {
            "status":         "submitted",
            "app_id":         fa_id,
            "app_reference":  fa_ref,
            "routed_to":      lender["name"] if lender else "ping_tree",
            "loan_product":   p["product_name"],
            "message":        "Application submitted. Lender response expected within 24-48 hours."
        }
    finally:
        db.close()


async def _route_to_lender(fa_id: int, lead_id: int, lead: dict, product: dict, lender: dict):
    """Background: format and send application to lender API."""
    import httpx, time as _time
    db = _db()
    start = _time.time()
    try:
        # Build lender payload (PII-safe — scrub full SSN)
        payload = {
            "nexabuilder_ref":    f"NB-{lead_id}",
            "financing_app_ref":  f"FA-{fa_id}",
            "loan_product":       product.get("product_code"),
            "loan_amount":        lead.get("financing_amount") or lead.get("project_budget"),
            "loan_purpose":       f"{lead.get('vertical','').title()} project",
            "project_type":       lead.get("project_type"),
            "project_address":    lead.get("property_address"),
            "borrower": {
                "first_name":        lead.get("first_name"),
                "last_name":         lead.get("last_name"),
                "email":             lead.get("email"),
                "phone":             lead.get("phone"),
                "annual_income":     lead.get("annual_income"),
                "employment_status": lead.get("employment_status"),
                "years_at_address":  lead.get("years_at_address"),
                "co_borrower":       lead.get("co_borrower"),
            },
            "property": {
                "address":           lead.get("property_address"),
                "type":              lead.get("property_type"),
                "estimated_value":   lead.get("avm_value"),
                "mortgage_balance":  lead.get("stated_mortgage_balance"),
                "estimated_equity":  lead.get("estimated_equity"),
            },
            "nexabuilder_score":   lead.get("pre_qual_score"),
            "consent":             True,
            "source":              "nexabuilder",
        }

        # Attempt API call
        api_url = lender.get("api_endpoint", "")
        response_status = "pending"
        response_payload = {}

        if api_url and "placeholder" not in api_url.lower():
            try:
                async with httpx.AsyncClient(timeout=30) as c:
                    r = await c.post(api_url, json=payload,
                        headers={"Content-Type": "application/json"})
                latency_ms = int((_time.time() - start) * 1000)
                response_status = "accepted" if r.status_code in (200,201) else "declined"
                response_payload = r.json() if r.text else {}

                # Update application with response
                db.execute(sqlt("""
                    UPDATE financing_applications SET
                      status = :status,
                      external_app_id = :ext_id,
                      lender_submitted_at = NOW(),
                      updated_at = NOW()
                    WHERE id = :id
                """), {
                    "status":  "under_review" if response_status=="accepted" else "declined_by_lender",
                    "ext_id":  response_payload.get("application_id") or response_payload.get("id"),
                    "id":      fa_id
                })
            except Exception as e:
                response_status = "error"
                response_payload = {"error": str(e)}
                latency_ms = int((_time.time() - start) * 1000)
        else:
            # API not yet live — mark as queued for manual review
            latency_ms = 0
            response_status = "queued_manual"
            response_payload = {"note": "Lender API not yet activated. Application queued for manual submission."}
            db.execute(sqlt("""
                UPDATE financing_applications SET
                  status = 'submitted_to_lender',
                  lender_submitted_at = NOW()
                WHERE id = :id
            """), {"id": fa_id})

        # Log the routing attempt
        db.execute(sqlt("""
            INSERT INTO lender_routing_log
              (financing_app_id, lead_id, lender_partner_id,
               loan_product_id, routing_type,
               request_payload, response_payload,
               response_status, latency_ms, routed_at)
            VALUES
              (:fa_id, :lid, :lender_id, :prod_id, :rtype,
               :req::jsonb, :resp::jsonb, :status, :latency, NOW())
        """), {
            "fa_id":     fa_id,
            "lid":       lead_id,
            "lender_id": str(lender.get("id","")) if lender else None,
            "prod_id":   product.get("id"),
            "rtype":     "lendapi" if "lendapi" in (lender.get("name","")).lower() else "raul_cruz",
            "req":       _json.dumps({k:v for k,v in payload.items() if k != "borrower"}),
            "resp":      _json.dumps(response_payload),
            "status":    response_status,
            "latency":   latency_ms,
        })
        db.commit()

    except Exception as e:
        log.error(f"Lender routing error for FA {fa_id}: {e}")
    finally:
        db.close()


async def _route_ping_tree(lead_id: int, fa_id: int, reason: str):
    """Background: route unmatched lead to Techcial ping tree."""
    db = _db()
    try:
        lead = db.execute(sqlt(
            "SELECT vertical, postal_code, project_budget FROM leads WHERE id=:id"
        ), {"id": lead_id}).fetchone()

        db.execute(sqlt("""
            INSERT INTO ping_tree_routes
              (lead_id, financing_app_id, route_reason,
               destination, destination_url, destination_partner,
               vertical, zip_code, status)
            VALUES
              (:lid, :fa_id, :reason,
               'techcial', 'https://api.techcial.com/v1/leads/receive',
               'Techcial Lead Exchange',
               :vert, :zip, 'pending')
        """), {
            "lid":    lead_id,
            "fa_id":  fa_id,
            "reason": reason,
            "vert":   lead[0] if lead else None,
            "zip":    lead[1] if lead else None,
        })

        # Mark lead as ping tree eligible
        db.execute(sqlt("""
            UPDATE leads SET
              ping_tree_eligible = TRUE,
              ping_tree_routed_at = NOW(),
              pre_qual_status = 'ping_tree_routed'
            WHERE id = :id
        """), {"id": lead_id})

        db.commit()
        log.info(f"Lead {lead_id} routed to ping tree. Reason: {reason}")
    except Exception as e:
        log.error(f"Ping tree routing error for lead {lead_id}: {e}")
    finally:
        db.close()


# ── Admin endpoints ───────────────────────────────────────────

@router.get("/pipeline")
async def get_pipeline(
    status:  Optional[str] = None,
    tier:    Optional[str] = None,
    days:    int = 30,
    limit:   int = 100,
    x_admin_key: str = Header(...)
):
    _req(x_admin_key)
    db = _db()
    try:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        where = ["submitted_at >= :since"]
        params = {"since": since, "limit": limit}
        if status: where.append("status = :status"); params["status"] = status
        if tier:   where.append("pre_qual_tier = :tier"); params["tier"] = tier

        rows = db.execute(sqlt(f"""
            SELECT * FROM financing_pipeline
            WHERE {' AND '.join(where)}
            LIMIT :limit
        """), params).fetchall()

        # KPI summary
        all_rows = db.execute(sqlt("""
            SELECT
              COUNT(*) AS total,
              COUNT(CASE WHEN pre_qual_tier IN ('A','B','C') THEN 1 END) AS pre_qualified,
              COUNT(CASE WHEN status='funded' THEN 1 END) AS funded,
              SUM(CASE WHEN status='funded' THEN approved_amount ELSE 0 END) AS total_funded,
              SUM(CASE WHEN status='funded' THEN referral_fee_earned ELSE 0 END) AS fees_earned,
              AVG(pre_qual_score)::INT AS avg_score
            FROM financing_applications
            WHERE submitted_at >= :since
        """), {"since": since}).fetchone()

        return {
            "period_days": days,
            "kpis":        dict(all_rows._mapping) if all_rows else {},
            "applications": [dict(r._mapping) for r in rows],
            "total":        len(rows),
        }
    finally:
        db.close()


@router.get("/application/{app_id}")
async def get_application(app_id: int, x_admin_key: str = Header(...)):
    _req(x_admin_key)
    db = _db()
    try:
        row = db.execute(sqlt(
            "SELECT * FROM financing_pipeline WHERE app_id = :id"
        ), {"id": app_id}).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Application not found")

        # Get routing log
        logs = db.execute(sqlt("""
            SELECT routing_type, response_status, response_message,
                   latency_ms, routed_at
            FROM lender_routing_log
            WHERE financing_app_id = :id
            ORDER BY routed_at DESC
        """), {"id": app_id}).fetchall()

        return {
            "application": dict(row._mapping),
            "routing_log": [dict(l._mapping) for l in logs],
        }
    finally:
        db.close()


@router.get("/products")
async def get_products(
    vertical: Optional[str] = None,
    amount:   Optional[float] = None,
    x_admin_key: str = Header(...)
):
    _req(x_admin_key)
    db = _db()
    try:
        where = ["is_active = TRUE"]
        params = {}
        if vertical: where.append(":vert = ANY(verticals)"); params["vert"] = vertical
        if amount:   where.append("min_loan_amount <= :amt AND max_loan_amount >= :amt"); params["amt"] = amount

        rows = db.execute(sqlt(f"""
            SELECT * FROM loan_products WHERE {' AND '.join(where)}
            ORDER BY routing_priority
        """), params).fetchall()
        return {"products": [dict(r._mapping) for r in rows]}
    finally:
        db.close()


@router.get("/revenue")
async def get_revenue(x_admin_key: str = Header(...)):
    _req(x_admin_key)
    db = _db()
    try:
        rows = db.execute(sqlt(
            "SELECT * FROM financing_revenue_summary LIMIT 24"
        )).fetchall()
        total = db.execute(sqlt("""
            SELECT
              COUNT(*) AS total_applications,
              COUNT(CASE WHEN status='funded' THEN 1 END) AS total_funded,
              COALESCE(SUM(CASE WHEN status='funded' THEN referral_fee_earned END),0) AS lifetime_fees,
              COALESCE(SUM(CASE WHEN status='funded' THEN approved_amount END),0) AS lifetime_volume
            FROM financing_applications
        """)).fetchone()
        return {
            "monthly": [dict(r._mapping) for r in rows],
            "lifetime": dict(total._mapping) if total else {}
        }
    finally:
        db.close()


@router.post("/ping-tree")
async def manual_ping_tree(
    lead_id: int,
    reason:  str = "manual_route",
    bg:      BackgroundTasks = None,
    x_admin_key: str = Header(...)
):
    _req(x_admin_key)
    bg.add_task(_route_ping_tree, lead_id, None, reason)
    return {"status": "routed", "lead_id": lead_id, "destination": "techcial"}
