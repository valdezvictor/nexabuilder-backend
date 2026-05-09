# app/services/ai_intake.py
import json, os
from typing import Optional


def get_anthropic_client():
    import boto3, anthropic
    ssm = boto3.client("ssm", region_name="us-west-1")
    try:
        r = ssm.get_parameter(Name="/nexabuilder/api/ANTHROPIC_KEY", WithDecryption=True)
        key = r["Parameter"]["Value"]
    except Exception:
        key = os.environ.get("ANTHROPIC_KEY", "")
    return anthropic.Anthropic(api_key=key)


def assess_lead(vertical: str, project_type: Optional[str], description: Optional[str], postal_code: Optional[str]) -> dict:
    if not description and not project_type:
        return _default(vertical)

    prompt = f"""You are an expert home improvement and construction project assessor for NexaBuilder.

Analyze this project and return ONLY valid JSON with this exact structure:
{{
  "complexity_score": <1-10>,
  "complexity_label": "<Simple|Moderate|Complex|Major>",
  "vertical_confirmed": "<home_services|new_construction|financial|legal>",
  "project_category": "<specific category>",
  "estimated_duration_weeks": <integer>,
  "estimated_cost_range": "<e.g. $5,000-$15,000>",
  "permit_required": <true|false>,
  "permit_types": [],
  "license_types_needed": [],
  "financial_alert": <true|false>,
  "financial_alert_reason": "<reason or null>",
  "insurance_alert": <true|false>,
  "insurance_alert_reason": "<reason or null>",
  "structural_flags": [],
  "ai_notes": "<1-2 sentence summary for agent>",
  "drone_recommended": <true|false>,
  "drone_reason": "<reason or null>"
}}

PROJECT:
- Vertical: {vertical}
- Type: {project_type or 'Not specified'}
- Description: {description or 'Not provided'}
- ZIP: {postal_code or 'Not provided'}

Scoring: 1-3=Simple, 4-6=Moderate, 7-8=Complex, 9-10=Major
financial_alert=true if >$25k or structural financing needed
insurance_alert=true if adds sqft, pool, or changes property value significantly
drone_recommended=true for pools, additions, new construction
"""

    try:
        import anthropic
        client = get_anthropic_client()
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:-1])
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
        import re as _re
        text = _re.sub(r",\s*([}\]])", r"", text)
        result = json.loads(text)
        result["ai_assessed"] = True
        return result
    except Exception as e:
        print(f"[AI INTAKE ERROR] {e}")
        return _default(vertical)


def _default(vertical: str) -> dict:
    return {
        "complexity_score": 5, "complexity_label": "Moderate",
        "vertical_confirmed": vertical, "project_category": "general",
        "estimated_duration_weeks": 2, "estimated_cost_range": "TBD",
        "permit_required": False, "permit_types": [],
        "license_types_needed": [], "financial_alert": False,
        "financial_alert_reason": None, "insurance_alert": False,
        "insurance_alert_reason": None, "structural_flags": [],
        "ai_notes": "Assessment pending - insufficient details.",
        "drone_recommended": False, "drone_reason": None, "ai_assessed": False,
    }
