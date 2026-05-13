# app/routers/api/ai_intake_agent.py
# Bilingual A2A AI intake agent (English/Spanish)
# Pre-qualifies leads before routing to licensed reps
# Detects language, frustration level, scores in real time

import os, json
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.core.auth import get_current_user

router = APIRouter(prefix="/api/intake-agent", tags=["AI Intake Agent"])

# ─── Prompts ──────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_EN = """You are Alex, a friendly bilingual intake specialist for NexaBuilder, 
a home improvement and construction services platform serving Southern California.

Your job:
1. Warmly greet the customer and collect project information
2. Ask ONE question at a time, naturally
3. Collect: full name, phone, ZIP code, project type, project description, timeline, budget range
4. Score the lead 1-10 based on: project size, location (SoCal = higher), budget clarity, readiness
5. Detect frustration: repeated short answers, "I don't know", long pauses, negative tone
6. Detect language preference: if they respond in Spanish, switch to Spanish

Scoring guide:
- 8-10: Large project (pool, addition, new construction), SoCal ZIP, clear budget >$25k, ready to start
- 5-7: Medium project, has some details, timeline unclear
- 1-4: Small project, vague details, no budget, not urgent

ALWAYS respond with valid JSON:
{
  "message": "your response to the customer",
  "language": "en" or "es",
  "collected": {
    "name": null or "string",
    "phone": null or "string", 
    "zip": null or "string",
    "project_type": null or "string",
    "description": null or "string",
    "timeline": null or "string",
    "budget": null or "string"
  },
  "lead_score": 0-10,
  "frustration_level": 0-10,
  "ready_for_handoff": true or false,
  "handoff_reason": null or "string",
  "routing": null or "internal" or "call_center_en" or "call_center_es" or "partner_network",
  "intake_complete": false
}

Route as:
- Score >=7 + SoCal ZIP (90xxx/91xxx/92xxx) → "internal"
- Score >=5 + Spanish → "call_center_es"  
- Score >=5 + English → "call_center_en"
- Score <5 → "partner_network"
- Frustration >=7 → immediate handoff to human

When intake_complete=true, include a handoff_summary in English for the rep."""

SYSTEM_PROMPT_ES = """Eres Alejandra, una especialista de atención al cliente de NexaBuilder,
una plataforma de mejoras del hogar y construcción que sirve al Sur de California.

Tu trabajo:
1. Saluda al cliente cálidamente y recopila información del proyecto
2. Haz UNA pregunta a la vez, de forma natural
3. Recopila: nombre completo, teléfono, código postal, tipo de proyecto, descripción, plazo, presupuesto
4. Puntúa el lead del 1-10
5. Detecta frustración: respuestas cortas repetidas, "no sé", tono negativo

SIEMPRE responde con JSON válido con la misma estructura que se te indicó."""


def get_anthropic_client():
    import boto3, anthropic
    ssm = boto3.client("ssm", region_name="us-west-1")
    try:
        r = ssm.get_parameter(Name="/nexabuilder/api/ANTHROPIC_KEY", WithDecryption=True)
        key = r["Parameter"]["Value"]
    except Exception:
        key = os.environ.get("ANTHROPIC_KEY", "")
    return anthropic.Anthropic(api_key=key)


def detect_language(text: str) -> str:
    """Quick heuristic language detection before calling Claude."""
    spanish_indicators = [
        "hola", "buenos", "quiero", "necesito", "tengo", "casa", "trabajo",
        "piscina", "cuánto", "cómo", "qué", "español", "habla", "gracias",
        "por favor", "ayuda", "servicio", "precio", "información"
    ]
    text_lower = text.lower()
    spanish_count = sum(1 for word in spanish_indicators if word in text_lower)
    return "es" if spanish_count >= 2 else "en"


def parse_agent_response(text: str) -> dict:
    """Parse Claude's JSON response with fallback."""
    import re
    # Extract JSON
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        text = text[start:end]
    text = re.sub(r",\s*([}\]])", r"\1", text)
    try:
        return json.loads(text)
    except Exception:
        return {
            "message": text[:500] if text else "I'm having trouble processing that. Could you repeat?",
            "language": "en",
            "collected": {},
            "lead_score": 0,
            "frustration_level": 0,
            "ready_for_handoff": False,
            "handoff_reason": None,
            "routing": None,
            "intake_complete": False,
        }


# ─── Models ───────────────────────────────────────────────────────────────────

class IntakeMessage(BaseModel):
    session_id: str
    message: str
    conversation_history: list = []  # List of {role, content} dicts
    language_hint: Optional[str] = None  # "en" or "es"


class StartIntakeRequest(BaseModel):
    source: str = "web"  # web, phone, sms, whatsapp
    language_hint: Optional[str] = None
    initial_data: Optional[dict] = None  # Pre-filled from form


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/start")
async def start_intake(payload: StartIntakeRequest):
    """
    Start a new intake session.
    Returns first agent message in detected/hinted language.
    """
    lang = payload.language_hint or "en"

    system = SYSTEM_PROMPT_EN if lang == "en" else SYSTEM_PROMPT_ES

    # Build opening message
    if lang == "es":
        opening_prompt = (
            "El cliente acaba de contactar a NexaBuilder. "
            "Salúdalo cálidamente en español y pregunta sobre su proyecto. "
            f"Fuente de contacto: {payload.source}. "
            "Responde con el JSON requerido."
        )
    else:
        opening_prompt = (
            f"A new lead just contacted NexaBuilder via {payload.source}. "
            "Greet them warmly and ask about their project. "
            "Respond with the required JSON."
        )

    # Pre-fill if data available
    if payload.initial_data:
        opening_prompt += f"\nPre-filled data from form: {json.dumps(payload.initial_data)}"
        opening_prompt += "\nAcknowledge what you already know and ask for missing info."

    try:
        client = get_anthropic_client()
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": opening_prompt}]
        )
        response = parse_agent_response(msg.content[0].text)
        response["session_id"] = payload.source + "_" + datetime.utcnow().strftime("%Y%m%d%H%M%S")
        return response
    except Exception as e:
        print(f"[AI INTAKE] Start error: {e}")
        msg = "¡Hola! Soy Alejandra de NexaBuilder. ¿En qué puedo ayudarle hoy?" if lang == "es" \
              else "Hi! I'm Alex from NexaBuilder. What home improvement project can I help you with today?"
        return {
            "session_id": "fallback_" + datetime.utcnow().strftime("%Y%m%d%H%M%S"),
            "message": msg,
            "language": lang,
            "collected": {},
            "lead_score": 0,
            "frustration_level": 0,
            "ready_for_handoff": False,
            "routing": None,
            "intake_complete": False,
        }


@router.post("/message")
async def process_message(payload: IntakeMessage):
    """
    Process a customer message and return agent response.
    Maintains conversation context via conversation_history.
    Auto-detects language switches and frustration.
    """
    # Detect language from customer message
    detected_lang = detect_language(payload.message)
    # Use history language if consistent, otherwise trust detection
    lang = payload.language_hint or detected_lang

    system = SYSTEM_PROMPT_EN if lang == "en" else SYSTEM_PROMPT_ES

    # Build messages array with full history
    messages = list(payload.conversation_history)
    messages.append({"role": "user", "content": payload.message})

    try:
        client = get_anthropic_client()
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=system,
            messages=messages
        )
        response = parse_agent_response(msg.content[0].text)
        response["session_id"] = payload.session_id
        response["detected_language"] = detected_lang

        # Auto-trigger handoff if frustration is high
        if response.get("frustration_level", 0) >= 7:
            response["ready_for_handoff"] = True
            response["handoff_reason"] = "high_frustration"
            response["routing"] = "call_center_es" if lang == "es" else "call_center_en"

        return response

    except Exception as e:
        print(f"[AI INTAKE] Message error: {e}")
        fallback = "Disculpe, hay un problema técnico. Un agente le atenderá en breve." if lang == "es" \
                   else "I'm having a technical issue. A live agent will assist you shortly."
        return {
            "session_id": payload.session_id,
            "message": fallback,
            "language": lang,
            "collected": {},
            "lead_score": 0,
            "frustration_level": 0,
            "ready_for_handoff": True,
            "handoff_reason": "technical_error",
            "routing": "call_center_es" if lang == "es" else "call_center_en",
            "intake_complete": False,
        }


@router.post("/complete")
async def complete_intake(
    session_id: str,
    collected_data: dict,
    conversation_history: list,
    lead_score: int,
    routing: str,
    identity: dict = Depends(get_current_user),
):
    """
    Finalize intake — create lead record + generate rep handoff summary.
    Called by call center system when agent marks intake complete.
    """
    from app.db import get_sessionmaker
    from app.models.lead import Lead
    from sqlalchemy import select

    # Create the lead in DB
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        lead = Lead(
            first_name=collected_data.get("name", "").split()[0] if collected_data.get("name") else None,
            last_name=" ".join(collected_data.get("name", "").split()[1:]) or None,
            phone=collected_data.get("phone"),
            postal_code=collected_data.get("zip"),
            vertical="home_services",
            project_type=collected_data.get("project_type"),
            project_description=collected_data.get("description"),
            source="ai_intake_agent",
            lead_status="review",
        )
        db.add(lead)
        await db.commit()
        await db.refresh(lead)

    # Generate rep handoff summary using AI
    lang = collected_data.get("language", "en")
    try:
        client = get_anthropic_client()
        summary_prompt = (
            f"Generate a concise handoff summary for a licensed sales rep.\n"
            f"Lead data: {json.dumps(collected_data)}\n"
            f"Lead score: {lead_score}/10\n"
            f"Routing: {routing}\n"
            f"Conversation had {len(conversation_history)} exchanges.\n\n"
            f"Include: project summary, key details, suggested opening line "
            f"{'in Spanish' if lang == 'es' else 'in English'}, "
            f"any flags or concerns. Keep it under 150 words."
        )
        summary_msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content": summary_prompt}]
        )
        handoff_summary = summary_msg.content[0].text.strip()
    except Exception:
        handoff_summary = f"Lead {collected_data.get('name', 'Unknown')} - {collected_data.get('project_type', 'Home project')} in {collected_data.get('zip', 'Unknown ZIP')}. Score: {lead_score}/10."

    return {
        "lead_id": lead.id,
        "lead_score": lead_score,
        "routing": routing,
        "handoff_summary": handoff_summary,
        "language": lang,
        "collected": collected_data,
    }
