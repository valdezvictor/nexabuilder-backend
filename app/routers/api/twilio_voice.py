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
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial callerId="{twilio_number}" timeout="30" record="record-from-ringing">
        <Number>{to}</Number>
    </Dial>
</Response>"""
    
    # Inbound call to our Twilio number — route to available agent
    elif to == twilio_number or direction == "inbound":
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna">
        Thank you for calling NexaBuilder. Please hold while we connect you with a home improvement specialist.
    </Say>
    <Dial timeout="20" record="record-from-ringing">
        <Client>agent</Client>
        <Client>agent2</Client>
    </Dial>
    <Say voice="Polly.Joanna">
        All agents are currently busy. Please call back or visit nexabuilder.com to submit a free project quote.
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
    """
    Generates a Twilio Voice access token for the browser softphone.
    The token allows the browser to make and receive calls via WebRTC.
    
    Requires: Twilio account SID, API Key SID, API Key Secret, TwiML App SID
    Note: For production, use API Keys (not auth token directly).
    """
    from app.core.twilio_config import get_twilio_config
    config = get_twilio_config()
    
    if not config["configured"]:
        return {
            "token":        None,
            "configured":   False,
            "phone_number": config["phone_number"],
            "message":      "Add Twilio credentials to SSM to enable live calling",
        }
    
    identity = (user.get("email") or "agent").split("@")[0]
    
    try:
        from twilio.jwt.access_token import AccessToken
        from twilio.jwt.access_token.grants import VoiceGrant
        
        # AccessToken(account_sid, api_key_sid, api_key_secret, ...)
        # Using auth_token as api_key_secret is OK for testing
        # For production: create API Keys in Twilio console
        token = AccessToken(
            config["account_sid"],
            config["account_sid"],    # api_key_sid — use account SID for now
            config["auth_token"],     # api_key_secret
            identity=identity,
            ttl=3600
        )
        
        grant = VoiceGrant(
            outgoing_application_sid=config["twiml_app_sid"],
            incoming_allow=True,
        )
        token.add_grant(grant)
        jwt = token.to_jwt()
        
        return {
            "token":        jwt if isinstance(jwt, str) else jwt.decode("utf-8"),
            "configured":   True,
            "phone_number": config["phone_number"],
            "identity":     identity,
            "twiml_app":    config["twiml_app_sid"],
        }
        
    except Exception as e:
        return {
            "token":      None,
            "configured": False,
            "error":      str(e),
            "message":    "Token generation failed — check Twilio credentials in SSM",
        }


# ── Call status webhook — Twilio sends call events here ──────────────────────
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
