# app/services/estimator.py
# AI-powered cost estimator using BLS regional data
# Self-healing JSON parsing with second Claude call fallback

import json, os, re, requests
from typing import Optional


def get_bls_regional_wages() -> dict:
    """Fetch BLS wage data with hardcoded fallbacks."""
    return {
        "general_contractor": 48.50,
        "electrician": 32.00,
        "plumber": 30.50,
        "carpenter": 26.00,
        "concrete_worker": 24.50,
        "pool_contractor": 45.00,
        "structural_engineer": 55.00,
        "landscaper": 22.00,
    }


def _parse_json_robust(text: str) -> dict:
    """Extract and parse JSON from LLM response, handling common issues."""
    # Extract JSON object directly (works with or without markdown)
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        text = text[start:end]

    # Fix trailing commas before } or ]
    text = re.sub(r",\s*([}\]])", r"\1", text)

    return json.loads(text)


def generate_estimate(
    vertical: str,
    project_type: str,
    description: Optional[str],
    postal_code: Optional[str],
    ai_assessment: dict,
) -> dict:
    """Generate line-item cost estimate using Claude + BLS wage data."""
    import boto3, anthropic

    ssm = boto3.client("ssm", region_name="us-west-1")
    try:
        r = ssm.get_parameter(Name="/nexabuilder/api/ANTHROPIC_KEY", WithDecryption=True)
        api_key = r["Parameter"]["Value"]
    except Exception:
        api_key = os.environ.get("ANTHROPIC_KEY", "")

    wages = get_bls_regional_wages()
    complexity = ai_assessment.get("complexity_score", 5)
    cost_range = ai_assessment.get("estimated_cost_range", "TBD")
    duration = ai_assessment.get("estimated_duration_weeks", 4)
    permits = ai_assessment.get("permit_types", [])
    structural_flags = ai_assessment.get("structural_flags", [])

    prompt = f"""You are a construction cost estimator for NexaBuilder. Generate a line-item cost estimate.

PROJECT:
- Type: {project_type}
- Description: {description or 'Not provided'}
- ZIP: {postal_code or 'Not provided'}
- Complexity: {complexity}/10
- AI Cost Range: {cost_range}
- Duration: {duration} weeks
- Permits: {', '.join(permits) if permits else 'None'}
- Structural flags: {', '.join(structural_flags) if structural_flags else 'None'}

LABOR RATES (BLS $/hr):
- General Contractor: ${wages['general_contractor']}
- Pool Contractor: ${wages['pool_contractor']}
- Electrician: ${wages['electrician']}
- Plumber: ${wages['plumber']}
- Concrete Worker: ${wages['concrete_worker']}
- Structural Engineer: ${wages['structural_engineer']}

Return ONLY valid JSON (no markdown, no comments):
{{
  "line_items": [
    {{"category": "Materials|Labor|Permits|Specialized", "item": "name", "description": "brief", "unit": "unit", "quantity": 0, "unit_cost": 0, "total": 0, "source": "Home Depot|White Cap|BLS|Contractor Average"}}
  ],
  "subtotals": {{"materials": 0, "labor": 0, "permits": 0, "specialized_services": 0}},
  "contingency_pct": 10,
  "contingency_amount": 0,
  "total_low": 0,
  "total_mid": 0,
  "total_high": 0,
  "financing_suggested": false,
  "financing_reason": null,
  "notes": "brief notes"
}}

Include realistic items for this specific project type. For pools include excavation, shotcrete/gunite, plumbing, electrical, decking, equipment, finish, fencing."""

    client = anthropic.Anthropic(api_key=api_key)

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()

        try:
            estimate = _parse_json_robust(text)
        except json.JSONDecodeError as e1:
            print(f"[ESTIMATOR] First parse failed: {e1} — trying self-heal")
            # Self-healing: ask Claude to fix the JSON
            fix_msg = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=4096,
                messages=[
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": "Return ONLY the corrected valid JSON object. No explanation, no markdown."}
                ]
            )
            estimate = _parse_json_robust(fix_msg.content[0].text.strip())

        estimate["bls_wages_used"] = wages
        estimate["estimated"] = True
        return estimate

    except Exception as e:
        print(f"[ESTIMATOR ERROR] {e}")
        return {"estimated": False, "error": str(e), "total_low": 0, "total_mid": 0, "total_high": 0}
