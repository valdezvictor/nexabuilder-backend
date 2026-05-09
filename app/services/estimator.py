# app/services/estimator.py
# AI-powered cost estimator using BLS regional data + web search pricing
# Runs after AI intake assessment to generate line-item cost breakdown

import json, os, requests
from typing import Optional


def get_bls_regional_wages(area_code: str = "0000000") -> dict:
    """
    Fetch BLS Occupational Employment and Wage Statistics for a region.
    area_code: MSA code (default 0000000 = national)
    """
    import boto3
    ssm = boto3.client("ssm", region_name="us-west-1")
    try:
        r = ssm.get_parameter(Name="/nexabuilder/api/BLS_API_KEY", WithDecryption=True)
        api_key = r["Parameter"]["Value"]
    except Exception:
        api_key = os.environ.get("BLS_API_KEY", "")

    # BLS series IDs for construction trades (national averages as fallback)
    series = {
        "general_contractor":  "OEWS000000000000011193100",  # Construction managers
        "electrician":         "OEWS000000000000047210100",  # Electricians
        "plumber":             "OEWS000000000000047220100",  # Plumbers
        "carpenter":           "OEWS000000000000047210100",  # Carpenters
        "concrete_worker":     "OEWS000000000000047211100",  # Cement masons
    }

    try:
        response = requests.post(
            "https://api.bls.gov/publicAPI/v2/timeseries/data/",
            json={
                "seriesid": list(series.values())[:3],  # API limit per call
                "registrationkey": api_key,
                "catalog": False,
            },
            timeout=10
        )
        data = response.json()
        if data.get("status") == "REQUEST_SUCCEEDED":
            wages = {}
            for i, (role, _) in enumerate(list(series.items())[:3]):
                try:
                    latest = data["Results"]["series"][i]["data"][0]
                    wages[role] = float(latest["value"])
                except (IndexError, KeyError, ValueError):
                    wages[role] = _default_wage(role)
            return wages
    except Exception as e:
        print(f"[BLS] API error: {e}")

    return {k: _default_wage(k) for k in series.keys()}


def _default_wage(role: str) -> float:
    """National average hourly wages by trade (2024 BLS data)."""
    defaults = {
        "general_contractor": 48.50,
        "electrician": 32.00,
        "plumber": 30.50,
        "carpenter": 26.00,
        "concrete_worker": 24.50,
        "pool_contractor": 45.00,
        "structural_engineer": 55.00,
    }
    return defaults.get(role, 30.00)


def generate_estimate(
    vertical: str,
    project_type: str,
    description: Optional[str],
    postal_code: Optional[str],
    ai_assessment: dict,
) -> dict:
    """
    Generate line-item cost estimate using Claude with BLS wage data.
    """
    import boto3, anthropic

    ssm = boto3.client("ssm", region_name="us-west-1")
    try:
        r = ssm.get_parameter(Name="/nexabuilder/api/ANTHROPIC_KEY", WithDecryption=True)
        api_key = r["Parameter"]["Value"]
    except Exception:
        api_key = os.environ.get("ANTHROPIC_KEY", "")

    # Get regional wages
    wages = get_bls_regional_wages()

    complexity = ai_assessment.get("complexity_score", 5)
    cost_range = ai_assessment.get("estimated_cost_range", "TBD")
    duration = ai_assessment.get("estimated_duration_weeks", 4)
    permits = ai_assessment.get("permit_types", [])

    prompt = f"""You are a construction cost estimator for NexaBuilder.

Generate a detailed line-item cost estimate for this project.

PROJECT:
- Type: {project_type}
- Description: {description or 'Not provided'}
- ZIP Code: {postal_code or 'Not provided'}
- AI Complexity Score: {complexity}/10
- AI Cost Range: {cost_range}
- Duration: {duration} weeks
- Permits Required: {', '.join(permits) if permits else 'None'}

REGIONAL LABOR RATES (BLS data, $/hour):
- General Contractor: ${wages.get('general_contractor', 48.50):.2f}
- Electrician: ${wages.get('electrician', 32.00):.2f}
- Plumber: ${wages.get('plumber', 30.50):.2f}
- Concrete Worker: ${wages.get('concrete_worker', 24.50):.2f}
- Carpenter: ${wages.get('carpenter', 26.00):.2f}

Return ONLY valid JSON with this structure:
{{
  "line_items": [
    {{
      "category": "<Materials|Labor|Permits|Specialized Services>",
      "item": "<item name>",
      "description": "<brief description>",
      "unit": "<each|sq ft|linear ft|cubic yard|hours|lump sum>",
      "quantity": <number>,
      "unit_cost": <number>,
      "total": <number>,
      "source": "<Home Depot|Lowes|White Cap|BLS|Contractor Average|Permit Office>"
    }}
  ],
  "subtotals": {{
    "materials": <total materials cost>,
    "labor": <total labor cost>,
    "permits": <total permit cost>,
    "specialized_services": <total specialized services>
  }},
  "contingency_pct": <5-15 based on complexity>,
  "contingency_amount": <contingency amount>,
  "total_low": <low estimate>,
  "total_mid": <mid estimate>,
  "total_high": <high estimate>,
  "financing_suggested": <true|false>,
  "financing_reason": "<reason if suggested>",
  "notes": "<any important estimating notes>"
}}

Include realistic line items for:
1. Major materials (concrete, lumber, equipment, fixtures)
2. Labor by trade
3. Permit fees (typical for the area)
4. Specialized services (shotcrete, excavation, engineering if needed)
5. Equipment rental if applicable

Use current market pricing. For pools include: excavation, shotcrete/gunite, plumbing, electrical, decking, equipment, finish.
"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
        import re
        text = re.sub(r',\s*([}\]])', r'\1', text)
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
        import re as _re
        text = _re.sub(r",\s*([}\]])", r"", text)
        estimate = json.loads(text)
        estimate["bls_wages_used"] = wages
        estimate["estimated"] = True
        return estimate
    except Exception as e:
        print(f"[ESTIMATOR ERROR] {e}")
        return {
            "estimated": False,
            "error": str(e),
            "total_low": 0,
            "total_mid": 0,
            "total_high": 0,
        }
