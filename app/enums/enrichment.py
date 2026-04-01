# app/enums/enrichment.py

from enum import Enum


class EntityType(str, Enum):
    CONTRACTOR = "contractor"
    LICENSE = "license"
    TRADE = "trade"
    ZIPCODE = "zipcode"
    LEAD = "lead"


class JobType(str, Enum):
    GEOCODE_ADDRESS = "geocode_address"
    VERIFY_LICENSE = "verify_license"
    NORMALIZE_TRADE = "normalize_trade"
    DISCOVER_WEBSITE = "discover_website"
    DISCOVER_EMAIL = "discover_email"
    SCORE_CONTRACTOR = "score_contractor"
    SCORE_LEAD = "score_lead"
