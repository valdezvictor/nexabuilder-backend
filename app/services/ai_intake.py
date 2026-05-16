# app/services/ai_intake.py
# Composite lead scoring engine — 5 factors, 1-10 score
# Factor 1: Project size/complexity  (max 3.5 pts)
# Factor 2: Geography/ZIP            (max 2.5 pts)
# Factor 3: Budget signal            (max 2.0 pts)
# Factor 4: Timeline/readiness       (max 1.0 pt)
# Factor 5: Contact completeness     (max 1.0 pt)
# Total max = 10 pts → normalized to 1-10

import json, os, re
from typing import Optional


# ─── SoCal ZIP prefixes ───────────────────────────────────────────────────────
SOCAL_ZIPS = ("90", "91", "92")
HIGH_INCOME_ZIPS = {
    "90210", "90402", "90272", "92651", "92660", "92662",
    "92625", "92657", "91011", "91108", "91302", "91361",
}

# ─── High-value verticals ─────────────────────────────────────────────────────
HIGH_VALUE_KEYWORDS = [
    "pool", "piscina", "addition", "adu", "new construction",
    "nueva construccion", "remodel", "remodelacion", "outdoor kitchen",
    "cocina exterior", "spa", "roofing", "techo",
]

MEDIUM_VALUE_KEYWORDS = [
    "bathroom", "baño", "kitchen", "cocina", "flooring",
    "electrical", "electrico", "plumbing", "plomeria", "hvac",
    "landscaping", "jardin",
]


def get_anthropic_client():
    import boto3, anthropic
    ssm = boto3.client("ssm", region_name="us-west-1")
    try:
        r = ssm.get_parameter(Name="/nexabuilder/api/ANTHROPIC_KEY", WithDecryption=True)
        key = r["Parameter"]["Value"]
    except Exception:
        key = os.environ.get("ANTHROPIC_KEY", "")
    return anthropic.Anthropic(api_key=key)


# ─── Factor scoring helpers ───────────────────────────────────────────────────

def score_geography(postal_code: Optional[str]) -> float:
    """Factor 2: Geography/ZIP — max 2.5 pts"""
    if not postal_code:
        return 0.5
    zip5 = postal_code.strip()[:5]
    prefix2 = zip5[:2]
    prefix3 = zip5[:3]

    if zip5 in HIGH_INCOME_ZIPS:
        return 2.5  # SoCal high-income ZIP
    if prefix2 in ("90", "91") or prefix3 == "920" or prefix3 == "921" or prefix3 == "922":
        return 2.0  # Core SoCal
    if prefix3 in ("923", "924", "925", "926", "927", "928", "929"):
        return 1.8  # Inland empire / SD adjacent
    if zip5[:2] in ("93", "94", "95", "96"):
        return 1.0  # Other CA
    return 0.3      # Out of state


def score_budget(budget: Optional[str], description: Optional[str],
                 cost_range: Optional[str]) -> float:
    """Factor 3: Budget signal — max 2.0 pts"""
    combined = " ".join(filter(None, [budget, description, cost_range])).lower()

    # Extract any dollar amounts mentioned
    amounts = re.findall(r'\$?([\d,]+)k?', combined)
    max_amount = 0
    for a in amounts:
        try:
            val = float(a.replace(',', ''))
            if 'k' in combined[combined.find(a):combined.find(a)+5]:
                val *= 1000
            max_amount = max(max_amount, val)
        except:
            pass

    if max_amount >= 100000 or any(w in combined for w in ['100k', '150k', '200k', 'six figure']):
        return 2.0
    if max_amount >= 50000 or any(w in combined for w in ['50k', '75k', '80k', '90k']):
        return 1.5
    if max_amount >= 25000 or any(w in combined for w in ['25k', '30k', '40k']):
        return 1.0
    if max_amount >= 10000 or any(w in combined for w in ['10k', '15k', '20k']):
        return 0.5
    if combined and len(combined) > 3:
        return 0.25  # Has description but no clear budget
    return 0.0


def score_timeline(description: Optional[str], project_type: Optional[str]) -> float:
    """Factor 4: Timeline/readiness — max 1.0 pt"""
    combined = " ".join(filter(None, [description, project_type])).lower()
    if any(w in combined for w in [
        'asap', 'immediately', 'now', 'urgent', 'this month', 'este mes',
        'ahora', 'inmediato', 'ready to start', 'listo para empezar',
        'next month', 'próximo mes', 'soon', 'pronto',
    ]):
        return 1.0
    if any(w in combined for w in [
        'this year', 'este año', '2026', 'few months', 'pocos meses',
        'planning', 'planeando', 'considering', 'considering',
    ]):
        return 0.5
    if any(w in combined for w in [
        'just looking', 'solo mirando', 'someday', 'algún día',
        'future', 'futuro', 'not sure', 'no estoy seguro',
    ]):
        return 0.1
    return 0.3  # Neutral/unspecified


def score_contact(first_name: Optional[str], last_name: Optional[str],
                  phone: Optional[str], email: Optional[str],
                  postal_code: Optional[str]) -> float:
    """Factor 5: Contact completeness — max 1.0 pt"""
    score = 0.0
    if first_name and len(first_name.strip()) > 1:
        score += 0.2
    if last_name and len(last_name.strip()) > 1:
        score += 0.1
    if phone and len(re.sub(r'\D', '', phone)) >= 10:
        score += 0.4
    if email and '@' in email and '.' in email:
        score += 0.2
    if postal_code and len(postal_code.strip()) >= 5:
        score += 0.1
    return min(score, 1.0)


def score_project_size(vertical: str, project_type: Optional[str],
                       description: Optional[str],
                       ai_complexity: int) -> float:
    """Factor 1: Project size/complexity — max 3.5 pts"""
    combined = " ".join(filter(None, [
        vertical, project_type, description
    ])).lower()

    # Base from AI complexity score (0-10 → 0-2.0)
    base = (ai_complexity / 10) * 2.0

    # Keyword bonus
    if any(k in combined for k in HIGH_VALUE_KEYWORDS):
        bonus = 1.5
    elif any(k in combined for k in MEDIUM_VALUE_KEYWORDS):
        bonus = 0.8
    elif vertical in ("new_construction", "home_services"):
        bonus = 0.5
    else:
        bonus = 0.2

    return min(base + bonus, 3.5)


def compute_composite_score(
    vertical: str,
    project_type: Optional[str],
    description: Optional[str],
    postal_code: Optional[str],
    budget: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    ai_complexity: int = 5,
    ai_cost_range: Optional[str] = None,
) -> dict:
    """
    Compute 5-factor composite lead score.
    Returns score 1-10 with breakdown.
    """
    f1 = score_project_size(vertical, project_type, description, ai_complexity)
    f2 = score_geography(postal_code)
    f3 = score_budget(budget, description, ai_cost_range)
    f4 = score_timeline(description, project_type)
    f5 = score_contact(first_name, last_name, phone, email, postal_code)

    raw_total = f1 + f2 + f3 + f4 + f5
    max_possible = 3.5 + 2.5 + 2.0 + 1.0 + 1.0  # = 10.0

    # Normalize to 1-10
    composite = max(1, min(10, round((raw_total / max_possible) * 10)))

    # Routing decision
    zip5 = (postal_code or "")[:5]
    is_socal = (postal_code or "")[:2] in ("90", "91", "92")
    if composite >= 7 and is_socal:
        routing = "internal"
    elif composite >= 4:
        # Will be refined by partner_routing.find_matching_partners at intake time
        routing = "partner_or_network"
    else:
        routing = "nurture"

    return {
        "composite_score": composite,
        "routing_recommendation": routing,
        "score_breakdown": {
            "project_size": round(f1, 2),
            "geography": round(f2, 2),
            "budget_signal": round(f3, 2),
            "timeline": round(f4, 2),
            "contact_completeness": round(f5, 2),
            "raw_total": round(raw_total, 2),
            "max_possible": max_possible,
        },
        "is_socal": is_socal,
        "high_income_zip": zip5 in HIGH_INCOME_ZIPS,
    }


# ─── Main AI assessment ───────────────────────────────────────────────────────

def assess_lead(
    vertical: str,
    project_type: Optional[str],
    description: Optional[str],
    postal_code: Optional[str],
    budget: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
) -> dict:
    """
    Full AI assessment + composite lead scoring.
    Returns merged result with both AI analysis and composite score.
    """
    if not description and not project_type:
        result = _default(vertical)
        scoring = compute_composite_score(
            vertical, project_type, description, postal_code,
            budget, first_name, last_name, phone, email,
            ai_complexity=5
        )
        result.update(scoring)
        return result

    prompt = f"""You are an expert home improvement and construction project assessor for NexaBuilder,
serving Southern California homeowners.

Analyze this project and return ONLY valid JSON with this exact structure:
{{
  "complexity_score": <1-10>,
  "complexity_label": "<Simple|Moderate|Complex|Major>",
  "vertical_confirmed": "<home_services|new_construction|financial|legal>",
  "project_category": "<specific category>",
  "estimated_duration_weeks": <integer>,
  "estimated_cost_range": "<e.g. $50,000-$80,000>",
  "permit_required": <true|false>,
  "permit_types": ["<list of permits needed>"],
  "license_types_needed": ["<e.g. C-53, B, C-36>"],
  "financial_alert": <true|false>,
  "financial_alert_reason": "<reason or null>",
  "insurance_alert": <true|false>,
  "insurance_alert_reason": "<reason or null>",
  "structural_flags": ["<any structural concerns>"],
  "ai_notes": "<2-3 sentence summary for the agent including location context>",
  "drone_recommended": <true|false>,
  "drone_reason": "<reason or null>"
}}

PROJECT:
- Vertical: {vertical}
- Type: {project_type or 'Not specified'}
- Description: {description or 'Not provided'}
- ZIP: {postal_code or 'Not provided'}
- Budget: {budget or 'Not specified'}

Scoring guide:
1-3 = Simple (repair, paint, minor fix)
4-6 = Moderate (bathroom, kitchen refresh, flooring)
7-8 = Complex (full remodel, pool, major addition)
9-10 = Major (new construction, pool+spa+outdoor kitchen, multi-trade)

financial_alert = true if estimated cost >$25,000
insurance_alert = true if adds square footage, pool, or significantly changes property value
drone_recommended = true for pools, additions, roofing, new construction
"""

    try:
        client = get_anthropic_client()
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()

        # Parse JSON
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:-1])
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
        text = re.sub(r",\s*([}\]])", r"\1", text)

        result = json.loads(text)
        result["ai_assessed"] = True

        # Compute composite score using AI complexity as input
        scoring = compute_composite_score(
            vertical=vertical,
            project_type=project_type,
            description=description,
            postal_code=postal_code,
            budget=budget,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            email=email,
            ai_complexity=result.get("complexity_score", 5),
            ai_cost_range=result.get("estimated_cost_range"),
        )
        result.update(scoring)

        # Log for monitoring
        print(f"[SCORING] ZIP={postal_code} | AI={result.get('complexity_score')}/10 "
              f"| Composite={scoring['composite_score']}/10 "
              f"| Routing={scoring['routing_recommendation']}")

        return result

    except Exception as e:
        print(f"[AI INTAKE ERROR] {e}")
        result = _default(vertical)
        scoring = compute_composite_score(
            vertical, project_type, description, postal_code,
            budget, first_name, last_name, phone, email
        )
        result.update(scoring)
        return result


def _default(vertical: str) -> dict:
    return {
        "complexity_score": 5,
        "complexity_label": "Moderate",
        "vertical_confirmed": vertical,
        "project_category": "general",
        "estimated_duration_weeks": 4,
        "estimated_cost_range": "TBD",
        "permit_required": False,
        "permit_types": [],
        "license_types_needed": [],
        "financial_alert": False,
        "financial_alert_reason": None,
        "insurance_alert": False,
        "insurance_alert_reason": None,
        "structural_flags": [],
        "ai_notes": "Assessment pending — insufficient project details provided.",
        "drone_recommended": False,
        "drone_reason": None,
        "ai_assessed": False,
    }
