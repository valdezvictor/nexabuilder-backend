"""
app/routers/api/chat.py
========================
Live chat backend with layered bot/abuse protection:

  1. Rate limiting     — IP-based, per session, per window
  2. Session tokens    — HMAC-signed, required for /message
  3. Topic guard       — keyword classifier, off-topic blocked
  4. Input sanitizer   — strips HTML, prompt injection patterns
  5. Spam detector     — repeated chars, gibberish, profanity list
  6. Honeypot          — bot_field must be empty
  7. Confidence score  — low-quality sessions flagged / auto-closed
  8. Session expiry    — 30 min inactivity kills session
"""

import hashlib, hmac, time, re, html, os
from datetime import datetime, timedelta
from collections import defaultdict
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
CHAT_SECRET        = os.getenv("CHAT_WIDGET_SECRET", "nb-chat-secret-change-in-prod")
MAX_MSG_LENGTH     = 500          # chars per message
MAX_MSGS_PER_SESSION = 30         # before session ends
RATE_LIMIT_WINDOW  = 60           # seconds
RATE_LIMIT_MAX_IP  = 10           # requests per window per IP
RATE_LIMIT_MAX_SESSION = 5        # messages per window per session
SESSION_TTL        = 1800         # 30 min inactivity
MIN_TOPIC_SCORE    = 1            # topic matches needed to continue

# ─────────────────────────────────────────────────────────────────────────────
# In-memory stores (Redis in production)
# ─────────────────────────────────────────────────────────────────────────────
sessions: dict     = {}
ip_rate:  dict     = defaultdict(list)   # ip → [timestamp, ...]
sess_rate: dict    = defaultdict(list)   # session_id → [timestamp, ...]
blocked_ips: set   = set()

# ─────────────────────────────────────────────────────────────────────────────
# Topic relevance — home improvement & NexaBuilder topics
# ─────────────────────────────────────────────────────────────────────────────
TOPIC_KEYWORDS = {
    # Verticals
    "pool", "pools", "swimming", "spa", "jacuzzi", "hot tub",
    "roof", "roofing", "shingle", "tile roof", "flat roof",
    "electrical", "electric", "wiring", "panel", "circuit", "ev charger",
    "plumbing", "pipe", "leak", "drain", "water heater", "sewer",
    "hvac", "ac", "air conditioning", "heating", "furnace", "ductwork",
    "remodel", "remodeling", "kitchen", "bathroom", "addition",
    "landscaping", "landscape", "lawn", "irrigation", "garden",
    "solar", "panels", "solar panels", "battery", "energy",
    "painting", "paint", "exterior", "interior",
    "flooring", "floor", "hardwood", "tile", "carpet",
    "concrete", "driveway", "patio", "stamped", "deck",
    "fencing", "fence", "gate",
    "windows", "window", "door", "sliding",
    "insulation", "drywall", "framing",
    # Service terms
    "contractor", "contractors", "license", "cslb", "quote", "estimate",
    "cost", "price", "how much", "project", "work", "repair", "install",
    "replace", "fix", "build", "construction", "home improvement",
    "permit", "inspection", "bid", "proposal",
    # NexaBuilder specific
    "nexabuilder", "nexa", "financing", "loan", "finance", "credit",
    "insurance", "contractor match", "rating", "review",
    # Location
    "california", "socal", "southern california", "orange county",
    "los angeles", "san diego", "riverside", "anaheim", "irvine",
    "backyard", "property", "house", "home", "condo", "apartment",
    # Greetings & contact
    "hello", "hi", "hey", "help", "question", "agent", "speak",
    "call", "phone", "contact", "info", "information",
}

OFF_TOPIC_PATTERNS = [
    r"\b(sex|porn|xxx|nude|naked|escort|drug|weed|cannabis|bitcoin|crypto|forex|invest|stock|hack|phish|scam|mlm|pyramid)\b",
    r"\b(password|login|account|credit card|ssn|social security|bank account)\b",
    r"(how to|tutorial|explain|write|code|program|script|api|sql|python|javascript)",
    r"\b(politic|president|election|democrat|republican|biden|trump|congress)\b",
    r"\b(celebrity|actor|movie|film|music|song|lyrics|sports|game|gaming)\b",
]

SPAM_PATTERNS = [
    r"(.)\1{6,}",                    # aaaaaaa repeated char
    r"\b(\w+)(\s+\1){3,}\b",         # word word word word
    r"[A-Z]{10,}",                     # ALLCAPS SHOUTING
    r"[!?]{4,}",                       # !!!!????
    r"http[s]?://(?!nexabuilder\.com)", # external URLs
    r"\$\d{3,}",                        # money spam ($999 deal)
]

PROFANITY = {
    "fuck","shit","ass","bitch","cunt","dick","pussy","bastard",
    "asshole","motherfucker","nigger","faggot","retard",
}

PROMPT_INJECTION_PATTERNS = [
    r"ignore (previous|all|the) (instructions?|prompts?|rules?|context)",
    r"you are now",
    r"pretend (you are|to be|you\'re)",
    r"act as (a|an|the|if)",
    r"jailbreak",
    r"DAN mode",
    r"system prompt",
    r"<(script|iframe|img|svg|object|embed)",
    r"(DROP|SELECT|INSERT|UPDATE|DELETE|UNION).*TABLE",
]

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def get_client_ip(request: Request) -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else request.client.host or "unknown"

def rate_check_ip(ip: str) -> bool:
    """True if OK, False if rate limited."""
    if ip in blocked_ips:
        return False
    now = time.time()
    ip_rate[ip] = [t for t in ip_rate[ip] if now - t < RATE_LIMIT_WINDOW]
    if len(ip_rate[ip]) >= RATE_LIMIT_MAX_IP:
        return False
    ip_rate[ip].append(now)
    return True

def rate_check_session(session_id: str) -> bool:
    """True if OK, False if rate limited."""
    now = time.time()
    sess_rate[session_id] = [t for t in sess_rate[session_id] if now - t < RATE_LIMIT_WINDOW]
    if len(sess_rate[session_id]) >= RATE_LIMIT_MAX_SESSION:
        return False
    sess_rate[session_id].append(now)
    return True

def generate_session_token(session_id: str) -> str:
    """HMAC-signed token for session validation."""
    msg = f"{session_id}:{int(time.time() // 3600)}"  # changes every hour
    return hmac.new(CHAT_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:16]

def validate_session_token(session_id: str, token: str) -> bool:
    """Validate token — allow current and previous hour window."""
    for offset in [0, 1]:
        msg = f"{session_id}:{int(time.time() // 3600) - offset}"
        expected = hmac.new(CHAT_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:16]
        if hmac.compare_digest(expected, token):
            return True
    return False

def sanitize_input(text: str) -> str:
    """Strip HTML, normalize whitespace, limit length."""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)           # strip HTML tags
    text = re.sub(r"\s+", " ", text).strip()       # normalize whitespace
    return text[:MAX_MSG_LENGTH]

def check_prompt_injection(text: str) -> bool:
    """Returns True if injection detected."""
    lower = text.lower()
    for pattern in PROMPT_INJECTION_PATTERNS:
        if re.search(pattern, lower, re.IGNORECASE):
            return True
    return False

def check_spam(text: str) -> bool:
    """Returns True if spam detected."""
    for pattern in SPAM_PATTERNS:
        if re.search(pattern, text):
            return True
    return False

def check_profanity(text: str) -> bool:
    """Returns True if profanity detected."""
    words = set(re.findall(r"\b\w+\b", text.lower()))
    return bool(words & PROFANITY)

def score_topic_relevance(text: str) -> int:
    """Returns count of topic keyword matches."""
    lower = text.lower()
    return sum(1 for kw in TOPIC_KEYWORDS if kw in lower)

def check_off_topic(text: str) -> bool:
    """Returns True if clearly off-topic."""
    lower = text.lower()
    for pattern in OFF_TOPIC_PATTERNS:
        if re.search(pattern, lower, re.IGNORECASE):
            return True
    return False

def session_is_expired(session: dict) -> bool:
    """Check if session has been inactive too long."""
    last_active = session.get("last_active", 0)
    return (time.time() - last_active) > SESSION_TTL

def compute_trust_score(session: dict) -> int:
    """
    0–100. Higher = more trustworthy session.
    Used to decide if session gets agent escalation.
    """
    score = 50  # base
    score += min(session.get("topic_matches", 0) * 5, 25)   # good topic signals
    score -= session.get("off_topic_count", 0) * 15          # off-topic penalty
    score -= session.get("spam_count", 0) * 20               # spam penalty
    score -= session.get("injection_count", 0) * 30          # injection penalty
    score += 10 if session.get("contact") else 0             # has contact info
    score += 5  if session.get("visitor_name") else 0        # has name
    return max(0, min(100, score))

# ─────────────────────────────────────────────────────────────────────────────
# AI Response engine
# ─────────────────────────────────────────────────────────────────────────────
AI_RESPONSES = {
    "pool":       "A pool installation in Southern California typically ranges from $65,000 to $120,000 depending on size and features. I can match you with licensed C-53 pool contractors in your area. Would you like a free quote?",
    "roof":       "Roofing costs vary by material and home size. A standard shingle re-roof runs $12,000–$22,000. Want me to connect you with a licensed C-39 roofing contractor?",
    "remodel":    "Kitchen remodels range from $30,000 to $150,000+. Bathroom remodels are $15,000–$60,000. I can match you with a licensed general contractor — want a free estimate?",
    "electrical": "Electrical work varies widely — panel upgrades, EV chargers, rewiring. Want a free quote from a licensed C-10 electrical contractor?",
    "plumbing":   "Plumbing costs depend on the scope. Would you like a free estimate from a licensed C-36 plumber in your area?",
    "hvac":       "HVAC installation or replacement typically runs $5,000–$15,000. Want a free quote from a licensed C-20 HVAC contractor?",
    "solar":      "Solar installation for a typical home runs $15,000–$35,000 before incentives. Federal tax credits can offset 30%. Want a free estimate?",
    "landscaping":"Landscaping projects range widely based on scope. Want to connect with a licensed C-27 landscaping contractor?",
    "financing":  "We work with lending partners who can finance your project. Many homeowners qualify for low monthly payments. Would you like to see your options?",
    "quote":      "I can get you a free, no-obligation quote in about 2 minutes. What type of project are you working on?",
    "hello":      "Hi! I\'m the NexaBuilder assistant. I help homeowners get free quotes from licensed, CSLB-verified contractors in Southern California. What project can I help you with?",
    "agent":      "Let me connect you with one of our specialists right now. Can I get your name and best contact number?",
    "default":    "I can help you get a free quote from licensed contractors in your area. What type of home improvement project are you working on?",
}

def get_ai_response(message: str, session: dict) -> tuple[str, bool]:
    """Returns (response, should_collect_lead)."""
    lower = message.lower()
    if any(w in lower for w in ["agent","human","person","talk to","speak to","call me","phone"]):
        return AI_RESPONSES["agent"], False
    for kw, resp in AI_RESPONSES.items():
        if kw != "default" and kw != "hello" and kw != "quote" and kw != "agent":
            if kw in lower:
                return resp, True
    if any(w in lower for w in ["quote","estimate","cost","price","how much","contractor"]):
        return AI_RESPONSES["quote"], True
    if any(w in lower for w in ["hi","hello","hey","good morning","good afternoon","good evening"]):
        return AI_RESPONSES["hello"], False
    if any(w in lower for w in ["financing","finance","loan","payment","monthly","afford"]):
        return AI_RESPONSES["financing"], True
    return AI_RESPONSES["default"], False

# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────
class ChatSessionInit(BaseModel):
    session_id: str
    visitor_name: str
    contact: str
    source: Optional[str] = None
    page_title: Optional[str] = None
    bot_field: Optional[str] = ""   # honeypot — must be empty
    widget_token: Optional[str] = None  # optional pre-auth token

class ChatMessageIn(BaseModel):
    session_id: str
    message: str
    visitor_name: Optional[str] = None
    contact: Optional[str] = None
    page_url: Optional[str] = None
    session_token: str              # required — issued at session creation
    bot_field: Optional[str] = ""  # honeypot — must be empty

# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/chat/sessions")
async def create_session(body: ChatSessionInit, request: Request):
    """
    Initialize a new chat session.
    Returns a session_token required for all subsequent /message calls.
    """
    ip = get_client_ip(request)

    # ── Honeypot check ──
    if body.bot_field:
        # Bots fill hidden fields — silently accept but mark as bot
        return {"status": "created", "session_id": body.session_id,
                "session_token": "bot-detected", "bot": True}

    # ── IP rate limit ──
    if not rate_check_ip(ip):
        raise HTTPException(429, "Too many requests. Please wait a moment.")

    # ── Basic input validation ──
    name    = sanitize_input(body.visitor_name or "")
    contact = sanitize_input(body.contact or "")

    if len(name) < 2:
        raise HTTPException(400, "Please enter your name.")
    if len(contact) < 7:
        raise HTTPException(400, "Please enter a valid phone or email.")
    if check_spam(name) or check_spam(contact):
        raise HTTPException(400, "Invalid input.")
    if check_profanity(name):
        raise HTTPException(400, "Please use appropriate language.")

    # ── Create session ──
    token = generate_session_token(body.session_id)
    sessions[body.session_id] = {
        "id":             body.session_id,
        "ip":             ip,
        "visitor_name":   name,
        "contact":        contact,
        "source":         body.source or "",
        "messages":       [],
        "created_at":     datetime.utcnow().isoformat(),
        "last_active":    time.time(),
        "off_topic_count":0,
        "spam_count":     0,
        "injection_count":0,
        "topic_matches":  0,
        "msg_count":      0,
        "is_bot":         False,
        "is_blocked":     False,
        "trust_score":    50,
        "is_agent_handling": False,
    }

    return {
        "status":        "created",
        "session_id":    body.session_id,
        "session_token": token,
    }


@router.post("/chat/message")
async def handle_message(body: ChatMessageIn, request: Request):
    """
    Handle an incoming chat message with full bot/abuse protection.
    """
    ip = get_client_ip(request)

    # ── Honeypot check ──
    if body.bot_field:
        return {"response": "Thank you for your message!", "is_agent": False}

    # ── IP rate limit ──
    if not rate_check_ip(ip):
        raise HTTPException(429, "Too many messages. Please slow down.")

    # ── Session token validation ──
    if body.session_token == "bot-detected":
        return {"response": "Thanks! We\'ll be in touch.", "is_agent": False}

    if not validate_session_token(body.session_id, body.session_token):
        raise HTTPException(403, "Invalid session. Please refresh and try again.")

    # ── Session lookup / expiry ──
    session = sessions.get(body.session_id)
    if not session:
        raise HTTPException(404, "Session not found. Please refresh.")
    if session.get("is_blocked"):
        raise HTTPException(403, "This chat session has been suspended.")
    if session_is_expired(session):
        del sessions[body.session_id]
        raise HTTPException(410, "Session expired. Please start a new chat.")
    if session.get("msg_count", 0) >= MAX_MSGS_PER_SESSION:
        raise HTTPException(429, "Session message limit reached.")

    # ── Per-session rate limit ──
    if not rate_check_session(body.session_id):
        return {"response": "Please slow down — I\'m still processing your last message.", "is_agent": False}

    # ── Sanitize input ──
    raw_msg = body.message or ""
    message = sanitize_input(raw_msg)

    if len(message.strip()) < 2:
        return {"response": "I didn\'t catch that. What project can I help you with?", "is_agent": False}

    # ── Prompt injection detection ──
    if check_prompt_injection(message):
        session["injection_count"] = session.get("injection_count", 0) + 1
        if session["injection_count"] >= 2:
            session["is_blocked"] = True
            return {"response": "This session has been terminated.", "is_agent": False,
                    "blocked": True}
        return {
            "response": "I can only help with home improvement questions. What project are you working on?",
            "is_agent": False, "flagged": True
        }

    # ── Spam detection ──
    if check_spam(message):
        session["spam_count"] = session.get("spam_count", 0) + 1
        if session["spam_count"] >= 3:
            session["is_blocked"] = True
            return {"response": "This session has been terminated due to spam.", "is_agent": False}
        return {"response": "I didn\'t understand that. What home improvement project can I help with?", "is_agent": False}

    # ── Profanity filter ──
    if check_profanity(message):
        session["spam_count"] = session.get("spam_count", 0) + 1
        return {
            "response": "Please keep our conversation professional. I\'m here to help with home improvement questions.",
            "is_agent": False, "flagged": True
        }

    # ── Off-topic detection ──
    is_off_topic = check_off_topic(message)
    topic_score  = score_topic_relevance(message)

    if is_off_topic:
        session["off_topic_count"] = session.get("off_topic_count", 0) + 1
        if session["off_topic_count"] >= 3:
            session["is_blocked"] = True
            return {
                "response": "This chat is for home improvement inquiries only. Session ended.",
                "is_agent": False, "blocked": True
            }
        return {
            "response": "I specialize in home improvement projects — pools, roofing, remodeling, and more. What project can I help you with?",
            "is_agent": False, "flagged": True
        }

    # ── Topic score — no keywords at all after 3 messages ──
    session["topic_matches"]  = session.get("topic_matches", 0) + topic_score
    session["msg_count"]      = session.get("msg_count", 0) + 1
    session["last_active"]    = time.time()

    if session["msg_count"] > 3 and session["topic_matches"] < MIN_TOPIC_SCORE:
        session["off_topic_count"] = session.get("off_topic_count", 0) + 1
        return {
            "response": "I\'m here to help with home improvement questions. Are you looking for a contractor quote for a specific project?",
            "is_agent": False
        }

    # ── Update trust score ──
    session["trust_score"] = compute_trust_score(session)

    # ── Log message ──
    session["messages"].append({
        "role":  "visitor",
        "text":  message,
        "time":  datetime.utcnow().isoformat(),
        "score": topic_score,
    })

    # ── Generate response ──
    if session.get("is_agent_handling"):
        return {"response": None, "is_agent": True, "should_collect_lead": False}

    response, should_collect = get_ai_response(message, session)

    session["messages"].append({
        "role": "ai",
        "text": response,
        "time": datetime.utcnow().isoformat(),
    })

    # High trust score → offer agent escalation
    offer_agent = (
        session["trust_score"] >= 60 and
        should_collect and
        session["msg_count"] >= 2
    )

    return {
        "response":          response,
        "is_agent":          False,
        "should_collect_lead": should_collect,
        "offer_agent":       offer_agent,
        "trust_score":       session["trust_score"],
        "session_id":        body.session_id,
    }


@router.get("/chat/sessions")
async def list_active_sessions(request: Request):
    """Active sessions for call center agents. Auth required in production."""
    now = time.time()
    active = []
    for s in list(sessions.values()):
        if session_is_expired(s) or s.get("is_blocked") or s.get("is_bot"):
            continue
        active.append({
            "id":            s["id"],
            "visitor_name":  s.get("visitor_name"),
            "contact":       s.get("contact"),
            "source":        s.get("source"),
            "message_count": len(s.get("messages", [])),
            "last_message":  s["messages"][-1]["text"] if s.get("messages") else "",
            "trust_score":   s.get("trust_score", 50),
            "created_at":    s.get("created_at"),
            "is_agent_handling": s.get("is_agent_handling", False),
        })

    # Sort by most recent activity
    active.sort(key=lambda x: x.get("created_at",""), reverse=True)
    return {"sessions": active, "count": len(active)}
