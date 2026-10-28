"""
app/schemas/blog_article.py
============================
Pydantic schemas for blog article API endpoints.
"""
from pydantic import BaseModel, field_validator, model_validator
from typing import Optional, List
from datetime import datetime
from enum import Enum
import re


class ArticleStatusEnum(str, Enum):
    draft     = "draft"
    review    = "review"
    scheduled = "scheduled"
    published = "published"
    archived  = "archived"


# ── Helpers ───────────────────────────────────────────────────────────────────
def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")

def _word_count(html: str) -> int:
    text = _strip_html(html)
    return len(text.split())

def _reading_time(wc: int) -> int:
    return max(1, round(wc / 200))


# ── Request schemas ───────────────────────────────────────────────────────────
class ArticleCreate(BaseModel):
    """Used by POST /api/blog/ to create a new article."""
    site_id:               str
    slug:                  str
    language:              str          = "es"
    hreflang_pair_slug:    Optional[str] = None

    # SEO Core — all required at creation
    h1:                    str
    seo_title:             str
    meta_description:      str
    canonical_url:         Optional[str] = None
    primary_keyword:       Optional[str] = None

    # Content
    deck:                  Optional[str] = None
    body_html:             Optional[str] = None
    toc_auto:              bool          = True

    # Media
    featured_image_url:    Optional[str] = None
    featured_image_alt:    Optional[str] = None
    featured_image_caption: Optional[str] = None
    og_image_url:          Optional[str] = None

    # Taxonomy
    category:              Optional[str] = None
    tags:                  Optional[str] = None
    related_article_ids:   Optional[str] = None

    # Authorship
    author_name:           str           = "Equipo Una Piscina"
    author_bio:            Optional[str] = None
    author_avatar:         Optional[str] = None

    # Publishing
    status:                ArticleStatusEnum = ArticleStatusEnum.draft
    scheduled_at:          Optional[datetime] = None

    # Schema / SEO
    schema_json:           Optional[str] = None
    schema_types:          Optional[str] = None

    # Local SEO
    geo_region:            Optional[str] = None
    geo_cities:            Optional[str] = None

    # AEO / GEO
    aeo_target_questions:  Optional[str] = None

    # OG overrides
    og_title:              Optional[str] = None
    og_description:        Optional[str] = None

    @field_validator("seo_title")
    @classmethod
    def seo_title_length(cls, v):
        if len(v) > 120:
            raise ValueError("seo_title must be 120 chars or fewer")
        return v

    @field_validator("meta_description")
    @classmethod
    def meta_desc_length(cls, v):
        if len(v) > 320:
            raise ValueError("meta_description must be 320 chars or fewer")
        return v

    @field_validator("slug")
    @classmethod
    def slug_format(cls, v):
        if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", v):
            raise ValueError("slug must be lowercase letters, numbers, and hyphens only")
        return v


class ArticleUpdate(BaseModel):
    """Used by PUT /api/blog/{id} — all fields optional for partial updates."""
    h1:                    Optional[str] = None
    seo_title:             Optional[str] = None
    meta_description:      Optional[str] = None
    canonical_url:         Optional[str] = None
    primary_keyword:       Optional[str] = None
    deck:                  Optional[str] = None
    body_html:             Optional[str] = None
    toc_auto:              Optional[bool] = None
    featured_image_url:    Optional[str] = None
    featured_image_alt:    Optional[str] = None
    featured_image_caption: Optional[str] = None
    og_image_url:          Optional[str] = None
    category:              Optional[str] = None
    tags:                  Optional[str] = None
    related_article_ids:   Optional[str] = None
    author_name:           Optional[str] = None
    author_bio:            Optional[str] = None
    author_avatar:         Optional[str] = None
    status:                Optional[ArticleStatusEnum] = None
    scheduled_at:          Optional[datetime] = None
    schema_json:           Optional[str] = None
    schema_types:          Optional[str] = None
    geo_region:            Optional[str] = None
    geo_cities:            Optional[str] = None
    aeo_target_questions:  Optional[str] = None
    og_title:              Optional[str] = None
    og_description:        Optional[str] = None
    hreflang_pair_slug:    Optional[str] = None
    updated_by:            Optional[str] = None


# ── Response schemas ──────────────────────────────────────────────────────────
class ArticlePublic(BaseModel):
    """Public response — what the frontend receives for rendering."""
    id:                    int
    site_id:               str
    slug:                  str
    language:              str
    hreflang_pair_slug:    Optional[str]
    h1:                    str
    seo_title:             str
    meta_description:      str
    canonical_url:         Optional[str]
    primary_keyword:       Optional[str]
    deck:                  Optional[str]
    body_html:             Optional[str]
    toc_auto:              bool
    featured_image_url:    Optional[str]
    featured_image_alt:    Optional[str]
    featured_image_caption: Optional[str]
    og_image_url:          Optional[str]
    category:              Optional[str]
    tags:                  Optional[str]
    related_article_ids:   Optional[str]
    author_name:           str
    author_bio:            Optional[str]
    author_avatar:         Optional[str]
    schema_json:           Optional[str]
    schema_types:          Optional[str]
    geo_region:            Optional[str]
    geo_cities:            Optional[str]
    aeo_target_questions:  Optional[str]
    og_title:              Optional[str]
    og_description:        Optional[str]
    reading_time_minutes:  Optional[int]
    word_count:            Optional[int]
    published_at:          Optional[datetime]
    modified_at:           Optional[datetime]

    model_config = {"from_attributes": True}


class ArticleAdmin(ArticlePublic):
    """Admin response — includes status, scheduling, and audit fields."""
    status:          ArticleStatusEnum
    scheduled_at:    Optional[datetime]
    created_at:      Optional[datetime]
    created_by:      Optional[str]
    updated_by:      Optional[str]

    model_config = {"from_attributes": True}


class ArticleListItem(BaseModel):
    """Compact item for list views — omits body_html."""
    id:                   int
    site_id:              str
    slug:                 str
    language:             str
    h1:                   str
    seo_title:            str
    category:             Optional[str]
    status:               ArticleStatusEnum
    published_at:         Optional[datetime]
    modified_at:          Optional[datetime]
    reading_time_minutes: Optional[int]
    word_count:           Optional[int]
    featured_image_url:   Optional[str]

    model_config = {"from_attributes": True}


class ArticleList(BaseModel):
    """Paginated list response."""
    articles: List[ArticleListItem]
    total:    int
    page:     int
    per_page: int
    pages:    int
