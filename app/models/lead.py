# app/models/lead.py

from sqlalchemy import Column, SmallInteger, Numeric, Integer, String, Float, JSON, DateTime, ForeignKey, func, Boolean, Text
from sqlalchemy.orm import mapped_column, relationship
from app.db import Base


class Lead(Base):
    __tablename__ = "leads"

    id = mapped_column(Integer, primary_key=True)
    contractor_id = mapped_column(ForeignKey("contractors.id"), nullable=True)
    trade_id = mapped_column(ForeignKey("trades.id"), nullable=True)
    zip_id = mapped_column(ForeignKey("zipcodes.id"), nullable=True)

    # Contact / context
    phone = mapped_column(String(50), nullable=True)
    email = mapped_column(String(255), nullable=True)
    first_name = mapped_column(String(100), nullable=True)
    last_name = mapped_column(String(100), nullable=True)
    budget_max = mapped_column(Integer, nullable=True)
    vertical = mapped_column(String(100), nullable=True)

    # Address
    address_line1 = mapped_column(String(255), nullable=True)
    address_line2 = mapped_column(String(255), nullable=True)
    city = mapped_column(String(100), nullable=True)
    state = mapped_column(String(2), nullable=True)
    postal_code = mapped_column(String(10), nullable=True)

    # Geo
    latitude = mapped_column(Float, nullable=True)
    longitude = mapped_column(Float, nullable=True)

    # AI / scoring
    ai_score = mapped_column(Float, nullable=True)
    ai_explanations = mapped_column(JSON, nullable=True)

    # AI intake assessment (Phase 2)
    ai_assessment = mapped_column(JSON, nullable=True)   # Full Claude assessment
    estimate = mapped_column(JSON, nullable=True)         # Line-item cost estimate
    project_type = mapped_column(String(100), nullable=True)
    project_description = mapped_column(String(2000), nullable=True)
    source = mapped_column(String(50), nullable=True)    # web_form, tv_ad, radio_ad, etc

    # ── Attribution / tracking (added 2026-05-29) ─────────────────────────
    site_id          = Column(String(64),  nullable=True)
    source_domain    = Column(String(255), nullable=True)
    referrer_url     = Column(Text,        nullable=True)
    landing_page     = Column(Text,        nullable=True)
    utm_source       = Column(String(128), nullable=True)
    utm_medium       = Column(String(128), nullable=True)
    utm_campaign     = Column(String(255), nullable=True)
    utm_content      = Column(String(255), nullable=True)
    utm_term         = Column(String(255), nullable=True)
    affiliate_id     = Column(String(128), nullable=True)
    sub_id           = Column(String(255), nullable=True)
    click_id         = Column(String(255), nullable=True)
    # ── Consent ────────────────────────────────────────────────────────────
    tcpa_consent     = Column(Boolean,     nullable=True, default=False)
    tcpa_timestamp   = Column(String(50),  nullable=True)
    tcpa_text        = Column(Text,        nullable=True)
    newsletter_optin = Column(Boolean,     nullable=True, default=False)
    language         = Column(String(8),   nullable=True, default='en')
    budget           = Column(String(64),  nullable=True)
    timeline         = Column(String(64),  nullable=True)

    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())

    contractor = relationship("Contractor", back_populates="leads")
    trade = relationship("Trade", back_populates="leads")
    zip = relationship("ZipCode", back_populates="leads")
    routing_tier = Column(Integer, default=1)

    # Lead status progression
    lead_status = Column(String(30), default='submitted')  # submitted|review|matched|site_visit|quote|approved|complete
    assigned_contractor_id = Column(String(100), nullable=True)  # Internal or CSLB license_no
    assigned_at = mapped_column(DateTime(timezone=True), nullable=True)
    status_updated_at = mapped_column(DateTime(timezone=True), nullable=True)
    internal_notes = Column(String(2000), nullable=True)

    # ── Attribution / tracking columns (added 2026-07) ─────────────────────
    nexa_cid         = Column(String(255), nullable=True, index=True)
    session_id       = Column(String(36), nullable=True)   # UUID stored as string
    fbclid           = Column(String(255), nullable=True)
    gclid            = Column(String(255), nullable=True)
    ttclid           = Column(String(255), nullable=True)
    device_type      = Column(String(30),  nullable=True)
    first_touch_at   = Column(DateTime(timezone=True), nullable=True)
    is_attributed    = Column(Boolean, nullable=True, default=False)
    source_domain    = Column(String(255), nullable=True)
    language         = Column(String(10),  nullable=True, default='en')

    # ── Financing / Pre-Qualification (added 2026-07) ─────────────────────
    needs_financing      = Column(Boolean,     nullable=True, default=False)
    financing_amount     = Column(Numeric(12,2), nullable=True)
    financing_type       = Column(String(30),  nullable=True)
    pre_qual_score       = Column(SmallInteger, nullable=True)
    pre_qual_status      = Column(String(20),  nullable=True, default='not_requested')
    ping_tree_eligible   = Column(Boolean,     nullable=True, default=False)
    ping_tree_routed_at  = Column(DateTime(timezone=True), nullable=True)
    annual_income        = Column(Numeric(12,2), nullable=True)
    employment_status    = Column(String(30),  nullable=True)
    years_at_address     = Column(SmallInteger, nullable=True)
    ownership_tenure     = Column(String(20),  nullable=True)
    co_borrower          = Column(Boolean,     nullable=True, default=False)
    ssn_last_four        = Column(String(4),   nullable=True)
    property_address     = Column(Text,        nullable=True)
    property_type        = Column(String(30),  nullable=True)
    property_year_built  = Column(SmallInteger, nullable=True)
    down_payment_available = Column(Numeric(12,2), nullable=True)
    stated_mortgage_balance = Column(Numeric(14,2), nullable=True)
    lender_ref           = Column(String(100), nullable=True)
    project_budget       = Column(Numeric(12,2), nullable=True)
