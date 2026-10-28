"""
app/services/address_service.py
================================
Normalizes property addresses into a deterministic hash for deduplication.

Goal: "123 Main St", "123 Main Street", "123 MAIN ST", "123 main st apt 4"
all produce the same hash so we can enforce one-assessment-per-property.

Normalization rules (in order):
  1. Uppercase everything
  2. Strip unit/apt/suite identifiers (APT, UNIT, STE, #, SUITE, BLDG)
  3. Expand common abbreviations (ST→STREET, AVE→AVENUE, etc.)
  4. Strip punctuation except digits and letters
  5. Collapse whitespace
  6. Concatenate: address_line1 + city + state + postal_code
  7. SHA-256 hash the result

Security note:
  The hash is stored in the DB, not the plaintext address.
  The raw address is stored separately for display/audit only.
  This means even if the DB is compromised, addresses aren't trivially reversible.
"""
import re
import hashlib


# Street type abbreviation → canonical form
STREET_ABBR = {
    r"\bST\b": "STREET",
    r"\bAVE\b": "AVENUE",
    r"\bAVENUE\b": "AVENUE",  # already canonical
    r"\bBLVD\b": "BOULEVARD",
    r"\bDR\b": "DRIVE",
    r"\bRD\b": "ROAD",
    r"\bLN\b": "LANE",
    r"\bCT\b": "COURT",
    r"\bCIR\b": "CIRCLE",
    r"\bPL\b": "PLACE",
    r"\bWAY\b": "WAY",
    r"\bHWY\b": "HIGHWAY",
    r"\bFWY\b": "FREEWAY",
    r"\bPKWY\b": "PARKWAY",
    r"\bTER\b": "TERRACE",
    r"\bTRCE\b": "TRACE",
    r"\bN\b": "NORTH",
    r"\bS\b": "SOUTH",
    r"\bE\b": "EAST",
    r"\bW\b": "WEST",
}

# Unit identifier patterns to strip
UNIT_PATTERNS = [
    r"\bAPT\b[\s#]*[\w-]+",
    r"\bUNIT\b[\s#]*[\w-]+",
    r"\bSTE\b[\s#]*[\w-]+",
    r"\bSUITE\b[\s#]*[\w-]+",
    r"\bBLDG\b[\s#]*[\w-]+",
    r"\bFLOOR\b[\s#]*[\w-]+",
    r"\bFL\b[\s#]*[\w-]+",
    r"#[\s]*[\w-]+",                   # #4B, #204
    r"\bNO\.?\b[\s]*[\w-]+",        # NO. 4, NO 4
]


def normalize_address(
    address_line1: str,
    city: str = "",
    state: str = "",
    postal_code: str = "",
) -> str:
    """
    Returns a normalized address string suitable for consistent hashing.
    Strips unit numbers, expands abbreviations, uppercases.
    """
    # Combine components
    parts = [address_line1, city, state, postal_code]
    combined = " ".join(p.strip() for p in parts if p and p.strip())

    # Uppercase
    s = combined.upper()

    # Strip unit identifiers
    for pattern in UNIT_PATTERNS:
        s = re.sub(pattern, " ", s, flags=re.IGNORECASE)

    # Expand street abbreviations
    for abbr, full in STREET_ABBR.items():
        s = re.sub(abbr, full, s)

    # Remove punctuation except spaces and alphanumerics
    s = re.sub(r"[^A-Z0-9 ]", " ", s)

    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()

    return s


def address_hash(
    address_line1: str,
    city: str = "",
    state: str = "",
    postal_code: str = "",
) -> str:
    """
    Returns a SHA-256 hex digest of the normalized address.
    This is the deduplication key stored in property_assessments.
    """
    normalized = normalize_address(address_line1, city, state, postal_code)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def address_components_from_lead(lead) -> dict:
    """Extract address fields from a Lead model instance."""
    return {
        "address_line1": lead.address_line1 or "",
        "city":          lead.city or "",
        "state":         lead.state or "CA",
        "postal_code":   lead.postal_code or "",
    }


def address_raw(
    address_line1: str,
    city: str = "",
    state: str = "",
    postal_code: str = "",
) -> str:
    """Human-readable combined address string for display/storage."""
    parts = [p.strip() for p in [address_line1, city, state, postal_code] if p and p.strip()]
    return ", ".join(parts)
