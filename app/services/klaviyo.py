import os, json, logging
import urllib.request, urllib.error
from typing import Optional

logger = logging.getLogger(__name__)

def _get_key():
    key = os.environ.get("KLAVIYO_PRIVATE_KEY")
    if key: return key
    try:
        import boto3
        ssm = boto3.client("ssm", region_name="us-west-1")
        r = ssm.get_parameter(Name="/nexabuilder/klaviyo/PRIVATE_API_KEY", WithDecryption=True)
        return r["Parameter"]["Value"]
    except Exception as e:
        logger.warning(f"[Klaviyo] No API key: {e}")
        return None

LIST_ROUTING = {
    # unapiscina.com
    ("unapiscina.com","es"):            "QZ8Vhy",
    ("unapiscina.com","en"):            "SMyNN3",
    # renovationremodel.com
    ("renovationremodel.com","en"):     "VwGWv2",
    ("renovationremodel.com","es"):     "VwGWv2",
    # iquotesai.com verticals
    ("iquotesai.com/construction","en"): "WG7MnS",
    ("iquotesai.com/insurance","en"):    "W56JLf",
    ("iquotesai.com/loans","en"):        "Vs5G9C",
    ("iquotesai.com/solar","en"):        "VYtuqR",
    ("iquotesai.com/education","en"):    "WwRgG6",
    # iquotesai site_id based routing
    ("iquotesai-construction","en"):     "WG7MnS",
    ("iquotesai-insurance","en"):        "W56JLf",
    ("iquotesai-loans","en"):            "Vs5G9C",
    ("iquotesai-solar","en"):            "VYtuqR",
    ("iquotesai-education","en"):        "WwRgG6",
}
LIST_ALL             = "TqzRqE"
LIST_LEADS_OPEN      = "WnqzNZ"
LIST_LEADS_CONVERTED = "TWXDFT"

def _kv(method, path, body=None, key=None):
    if not key: key = _get_key()
    if not key: return None, 0
    url = f"https://a.klaviyo.com/api/{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Klaviyo-API-Key {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "revision": "2024-10-15",
    })
    try:
        r = urllib.request.urlopen(req, timeout=10)
        raw = r.read()
        return (json.loads(raw) if raw else {}), r.status
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:200]}, e.code
    except Exception as exc:
        return {"error": str(exc)}, 0

def _upsert_profile(email, attrs, key):
    data, status = _kv("POST", "profiles/", {"data": {"type": "profile", "attributes": {
        "email": email,
        **{k: v for k, v in attrs.items() if k in ("first_name","last_name","phone_number")},
        "properties": {k: v for k, v in attrs.items() if k not in ("first_name","last_name","phone_number","email")},
    }}}, key)
    if status in (200, 201): return data["data"]["id"]
    if status == 409:
        try:
            dup = data["errors"][0]["meta"]["duplicate_profile_id"]
            _kv("PATCH", f"profiles/{dup}/", {"data": {"type": "profile", "id": dup,
                "attributes": {"properties": {k: v for k, v in attrs.items() if k not in ("first_name","last_name","phone_number","email")}}}}, key)
            return dup
        except: pass
    return None

def _add_to_list(list_id, profile_id, key):
    _, status = _kv("POST", f"lists/{list_id}/relationships/profiles/",
        {"data": [{"type": "profile", "id": profile_id}]}, key)
    return status in (200, 204)

def klaviyo_sync_lead(lead):
    """Sync lead to Klaviyo after intake. Non-blocking."""
    if not lead.email: return False
    key = _get_key()
    if not key: return False
    try:
        attrs = {
            "first_name":  lead.first_name or "",
            "last_name":   lead.last_name or "",
            "phone_number": lead.phone or "",
            "source_domain": getattr(lead, "source_domain", None) or lead.source or "nexabuilder.com",
            "site_id":     getattr(lead, "site_id", None) or "nexabuilder",
            "language":    getattr(lead, "language", None) or "en",
            "vertical":    lead.vertical or "",
            "utm_source":  getattr(lead, "utm_source", None) or "",
            "utm_medium":  getattr(lead, "utm_medium", None) or "",
            "utm_campaign": getattr(lead, "utm_campaign", None) or "",
            "affiliate_id": getattr(lead, "affiliate_id", None) or "",
            "lead_status": "submitted",
            "nexabuilder_lead_id": str(lead.id),
        }
        pid = _upsert_profile(lead.email, attrs, key)
        if not pid: return False
        _add_to_list(LIST_LEADS_OPEN, pid, key)
        _kv("POST", "events/", {"data": {"type": "event", "attributes": {
            "metric": {"data": {"type": "metric", "attributes": {"name": "Lead Submitted"}}},
            "profile": {"data": {"type": "profile", "attributes": {"email": lead.email}}},
            "properties": {"lead_id": lead.id, "vertical": lead.vertical or "",
                "source": attrs["source_domain"], "financing": bool(getattr(lead, "needs_financing", False))},
        }}}, key)
        if getattr(lead, "newsletter_optin", False):
            src = attrs["source_domain"]
            lang = attrs["language"]
            specific = LIST_ROUTING.get((src, lang))
            _add_to_list(LIST_ALL, pid, key)
            if specific: _add_to_list(specific, pid, key)
        logger.info(f"[Klaviyo] Lead synced: {lead.email} lead_id={lead.id}")
        return True
    except Exception as exc:
        logger.exception(f"[Klaviyo] sync_lead error: {exc}")
        return False

def klaviyo_add_subscriber(email, first_name="", last_name="", phone="",
    source_domain="nexabuilder.com", site_id="nexabuilder", language="en",
    vertical="", utm_source="", utm_campaign="", affiliate_id="", lead_id=None):
    """Standalone newsletter opt-in."""
    if not email or "@" not in email: return False
    key = _get_key()
    if not key: return False
    try:
        attrs = {"first_name": first_name, "last_name": last_name, "phone_number": phone,
            "source_domain": source_domain, "site_id": site_id, "language": language,
            "vertical": vertical, "utm_source": utm_source, "utm_campaign": utm_campaign,
            "affiliate_id": affiliate_id, "lead_status": "subscriber",
            "nexabuilder_lead_id": str(lead_id) if lead_id else ""}
        pid = _upsert_profile(email, attrs, key)
        if not pid: return False
        _add_to_list(LIST_ALL, pid, key)
        specific = LIST_ROUTING.get((source_domain, language))
        if specific: _add_to_list(specific, pid, key)
        return True
    except Exception as exc:
        logger.exception(f"[Klaviyo] add_subscriber error: {exc}")
        return False
