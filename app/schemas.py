"""Consistent API envelope + request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- envelope
def ok(data: Any = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"success": True, "data": data, "error": None, "meta": meta or {}}


def fail(code: str, message: str, details: Any = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "success": False,
        "data": None,
        "error": {"code": code, "message": message, "details": details},
        "meta": meta or {},
    }


class APIError(Exception):
    """Raised anywhere in the API layer to produce a consistent JSON error body."""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


# --------------------------------------------------------------------------- requests
class RegisterRequest(BaseModel):
    """JSON body of POST /api/v1/members/register."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    full_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    phone: str | None = Field(default=None, max_length=64)
    company: str | None = Field(default=None, max_length=200)
    consent_marketing: bool = False

    utm_source: str | None = Field(default=None, max_length=255)
    utm_medium: str | None = Field(default=None, max_length=255)
    utm_campaign: str | None = Field(default=None, max_length=255)
    utm_content: str | None = Field(default=None, max_length=255)
    utm_term: str | None = Field(default=None, max_length=255)
    landing_url: str | None = Field(default=None, max_length=2048)
    referrer: str | None = Field(default=None, max_length=2048)
    fbp: str | None = Field(default=None, max_length=255)
    fbc: str | None = Field(default=None, max_length=255)
    fbclid: str | None = Field(default=None, max_length=255)

    source: str | None = Field(default=None, max_length=50)


class ResendVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pass


# --------------------------------------------------------------------------- responses
class AttributionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    utm_term: str | None = None
    landing_url: str | None = None
    referrer: str | None = None
    fbp: str | None = None
    fbc: str | None = None
    ip_hash: str | None = None
    created_at: datetime | None = None


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    email: str
    phone: str | None = None
    company: str | None = None
    status: str
    consent_marketing: bool
    email_verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MemberDetailOut(MemberOut):
    attribution: AttributionOut | None = None


class RegisterResultOut(BaseModel):
    member: MemberOut
    verification_sent: bool
    duplicate: bool
    message: str
