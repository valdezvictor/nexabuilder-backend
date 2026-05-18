from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum


class ContentTypeEnum(str, Enum):
    text      = "text"
    image_url = "image_url"
    json      = "json"
    html      = "html"


# ── Public response (GET /api/content/{page_slug}/{block_key}) ───────────────
class ContentBlockPublic(BaseModel):
    page_slug:    str
    block_key:    str
    tenant_id:    str
    content_type: ContentTypeEnum
    value:        Optional[str]
    alt_text:     Optional[str]

    class Config:
        from_attributes = True


# ── Admin upsert (PUT /api/content/{page_slug}/{block_key}) ──────────────────
class ContentBlockUpsert(BaseModel):
    content_type: ContentTypeEnum = ContentTypeEnum.text
    value:        Optional[str]   = None
    alt_text:     Optional[str]   = None
    is_published: bool            = False
    tenant_id:    str             = "nexabuilder"


# ── Admin full response ───────────────────────────────────────────────────────
class ContentBlockAdmin(BaseModel):
    id:           int
    page_slug:    str
    block_key:    str
    tenant_id:    str
    content_type: ContentTypeEnum
    value:        Optional[str]
    alt_text:     Optional[str]
    is_published: bool
    version:      int
    updated_at:   Optional[datetime]
    updated_by:   Optional[str]

    class Config:
        from_attributes = True


# ── Bulk response for page listing ───────────────────────────────────────────
class ContentBlockList(BaseModel):
    page_slug: str
    tenant_id: str
    blocks:    list[ContentBlockAdmin]
    total:     int
