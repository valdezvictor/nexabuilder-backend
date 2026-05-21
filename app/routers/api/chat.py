from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

router = APIRouter()

class ChatSession(BaseModel):
    session_id: str
    visitor_name: str
    contact: str
    source: Optional[str] = None
    page_title: Optional[str] = None

class ChatMessage(BaseModel):
    session_id: str
    message: str
    visitor_name: Optional[str] = None
    contact: Optional[str] = None
    page_url: Optional[str] = None

# In-memory session store (replace with DB/Redis for production)
sessions: dict = {}

# Keywords that trigger lead collection
LEAD_TRIGGERS = [
    "quote", "price", "cost", "estimate", "install", "repair",
    "pool", "roof", "remodel", "electrical", "plumbing", "hvac",
    "want", "need", "looking for", "interested"
]

AI_RESPONSES = {
    "pool":       "A pool installation in Southern California typically ranges from $65,000 to $120,000 depending on size and features. I can match you with licensed C-53 pool contractors in your area. Would you like a free quote?",
    "roof":       "Roofing costs vary by material and size. A standard shingle re-roof runs $12,000-$22,000. Want me to connect you with a licensed C-39 roofing contractor?",
    "remodel":    "Kitchen remodels range from $30,000 to $150,000+. Bathroom remodels are $15,000-$60,000. I can get you matched with a licensed general contractor — want a free estimate?",
    "electrical": "Electrical work varies widely — panel upgrades, EV chargers, rewiring. Want me to get you a free quote from a licensed C-10 electrical contractor?",
    "plumbing":   "Plumbing costs depend on the scope. Would you like a free estimate from a licensed C-36 plumbing contractor in your area?",
    "hvac":       "HVAC and AC installation or replacement typically runs $5,000-$15,000. Want a free quote from a licensed C-20 HVAC contractor?",
    "financing":  "We work with lending partners who can finance your project. Many homeowners qualify for low monthly payments with no impact to credit for the initial check. Want to see your options?",
    "quote":      "I can get you a free, no-obligation quote from licensed contractors in your area. It takes about 2 minutes. What type of project do you need help with?",
    "hello":      "Hi! I\'m the NexaBuilder AI assistant. I can help you get free quotes from licensed contractors, answer questions about home improvement projects, or connect you with a live agent. What can I help you with today?",
    "agent":      "Of course! Let me connect you with one of our specialists. They\'ll have your project details ready. What\'s the best way to reach you?",
    "default":    "That\'s a great question! I want to make sure I get you the right help. Are you looking for a contractor quote, have a question about your project, or would you like to speak with a live agent?"
}

def get_ai_response(message: str, session: dict) -> tuple[str, bool]:
    """Returns (response_text, should_collect_lead)"""
    msg_lower = message.lower()
    
    # Check for agent request
    if any(word in msg_lower for word in ["agent", "human", "person", "talk to someone", "call me"]):
        return ("I\'m connecting you with an available agent right now. They\'ll be with you in just a moment. In the meantime, can I confirm the best number or email to reach you?", False)
    
    # Match vertical keywords
    for keyword, response in AI_RESPONSES.items():
        if keyword in msg_lower and keyword not in ["default", "hello", "agent", "quote"]:
            should_collect = any(t in msg_lower for t in LEAD_TRIGGERS)
            return (response, should_collect)
    
    # Lead intent detection
    if any(t in msg_lower for t in LEAD_TRIGGERS):
        return (AI_RESPONSES["quote"], True)
    
    # Greeting
    if any(w in msg_lower for w in ["hi", "hello", "hey", "good morning", "good afternoon"]):
        return (AI_RESPONSES["hello"], False)
    
    return (AI_RESPONSES["default"], False)


@router.post("/chat/sessions")
async def create_session(session: ChatSession):
    """Initialize a new chat session from the widget."""
    sessions[session.session_id] = {
        "id": session.session_id,
        "visitor_name": session.visitor_name,
        "contact": session.contact,
        "source": session.source,
        "messages": [],
        "created_at": datetime.utcnow().isoformat(),
        "is_agent_handling": False,
    }
    # TODO: notify call center agents via WebSocket
    return {"status": "created", "session_id": session.session_id}


@router.post("/chat/message")
async def handle_message(msg: ChatMessage):
    """Handle incoming chat message — AI responds or routes to agent."""
    session = sessions.get(msg.session_id, {
        "visitor_name": msg.visitor_name,
        "contact": msg.contact,
        "messages": [],
        "is_agent_handling": False,
    })
    
    # Log message
    session["messages"].append({
        "role": "visitor",
        "text": msg.message,
        "time": datetime.utcnow().isoformat(),
    })
    
    # If agent is handling, just log and notify (WebSocket in production)
    if session.get("is_agent_handling"):
        return {"response": None, "is_agent": True, "should_collect_lead": False}
    
    # AI response
    response, should_collect = get_ai_response(msg.message, session)
    
    session["messages"].append({
        "role": "ai",
        "text": response,
        "time": datetime.utcnow().isoformat(),
    })
    
    sessions[msg.session_id] = session
    
    return {
        "response": response,
        "is_agent": False,
        "should_collect_lead": should_collect,
        "session_id": msg.session_id,
    }


@router.get("/chat/sessions")
async def list_active_sessions():
    """Return active chat sessions for call center agents."""
    return {
        "sessions": [
            {
                "id": s["id"],
                "visitor_name": s.get("visitor_name"),
                "contact": s.get("contact"),
                "message_count": len(s.get("messages", [])),
                "last_message": s["messages"][-1]["text"] if s.get("messages") else "",
                "created_at": s.get("created_at"),
            }
            for s in sessions.values()
        ]
    }
