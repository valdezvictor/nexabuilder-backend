"""
app/routers/api/twilio_voice.py
================================
Twilio Voice integration:
  POST /api/twilio/voice      — TwiML webhook (Twilio calls this)
  GET  /api/twilio/token      — Access token for browser softphone
  POST /api/twilio/voice/status — Call status callbacks
"""
from fastapi import APIRouter, Request, Response, Depends
from fastapi.responses import PlainTextResponse
from app.core.auth import get_current_user

router = APIRouter()

# ── TwiML webhook — Twilio calls this when a call comes in ───────────────────
@router.post("/twilio/voice")
async def twilio_voice_webhook(request: Request):
    """
    Called by Twilio when:
    1. An agent initiates an outbound call from the softphone
    2. An inbound call comes in to the Twilio number
    
    Returns TwiML XML telling Twilio how to handle the call.
    """
    form = await request.form()
    
    # Who is calling / being called
    to       = form.get("To", "")
    from_    = form.get("From", "")
    caller   = form.get("Caller", "")
    direction = form.get("Direction", "inbound")
    
    from app.core.twilio_config import get_twilio_config
    config = get_twilio_config()
    twilio_number = config.get("phone_number", "+15625125744")
    
    # Outbound call — agent is calling a number
    if to and not to.startswith("client:") and to != twilio_number:
        # Agent is calling an external number (homeowner, contractor)
        # Normalize the number — remove any non-digit chars except leading +
        import re as _re
        clean_to = '+' + _re.sub(r'\D', '', to) if not to.startswith('+') else to
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial callerId="{twilio_number}" timeout="30" record="record-from-ringing"
          action="https://api.nexabuilder.com/api/twilio/voice/status" method="POST">
        <Number statusCallbackEvent="initiated ringing answered completed"
                statusCallback="https://api.nexabuilder.com/api/twilio/voice/status"
                statusCallbackMethod="POST">{clean_to}</Number>
    </Dial>
</Response>"""
    
    # Inbound call to our Twilio number — route to available agent
    elif to == twilio_number or direction == "inbound":
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">
        Thank you for calling NexaBuilder, your home improvement specialists. Please hold for just a moment.
    </Say>
    <Dial timeout="25" record="record-from-ringing" callerId="{twilio_number}"
          action="{twilio_number}" method="POST">
        <Client statusCallbackEvent="initiated ringing answered completed"
                statusCallback="https://api.nexabuilder.com/api/twilio/voice/status">agent</Client>
        <Client statusCallbackEvent="initiated ringing answered completed"
                statusCallback="https://api.nexabuilder.com/api/twilio/voice/status">agent2</Client>
    </Dial>
    <Say voice="Polly.Joanna">
        We apologize, all specialists are currently assisting other homeowners.
        Please visit nexabuilder.com to submit your project online and receive a free quote, 
        or call us back and we will connect you right away.
    </Say>
</Response>"""
    
    # Client-to-client (agent browser call)
    else:
        client_name = to.replace("client:", "") if to.startswith("client:") else "agent"
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial timeout="20">
        <Client>{client_name}</Client>
    </Dial>
</Response>"""
    
    return Response(content=twiml, media_type="application/xml")


# ── Voice access token — issued to browser softphone ─────────────────────────
@router.get("/twilio/token")
async def get_voice_token(user: dict = Depends(get_current_user)):
    """Generate Twilio Voice access token for browser softphone."""
    from app.core.twilio_config import generate_voice_token
    identity = (user.get("email") or "agent").split("@")[0]
    result = generate_voice_token(identity)
    return result


@router.get("/twilio/token/check")
async def check_twilio_status():
    """Public endpoint — check Twilio config without auth (for softphone init)."""
    from app.core.twilio_config import get_twilio_config
    config = get_twilio_config()
    return {
        "configured":   config["configured"],
        "phone_number": config["phone_number"],
        "has_app_sid":  bool(config.get("twiml_app_sid")),
    }


# DEAD endpoint — replaced by above
@router.post("/twilio/voice/status")
async def call_status(request: Request):
    """
    Called by Twilio when call status changes.
    Use this to log call records, update lead timelines, etc.
    """
    form = await request.form()
    
    call_sid    = form.get("CallSid", "")
    status      = form.get("CallStatus", "")
    duration    = form.get("CallDuration", "0")
    to          = form.get("To", "")
    from_       = form.get("From", "")
    direction   = form.get("Direction", "")
    recording   = form.get("RecordingUrl", "")
    
    # Log to console for now — wire to DB in next iteration
    print(f"[Twilio] Call {call_sid}: {from_} → {to} | {status} | {duration}s | {direction}")
    if recording:
        print(f"[Twilio] Recording: {recording}")
    
    # TODO: Update lead timeline when we have the lead_id
    # await update_lead_timeline(lead_id, "call_completed", {...})
    
    return Response(content="", media_type="text/plain")
