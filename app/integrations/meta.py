"""Meta (Facebook) Conversions API.

Preparation only: the event is sent when - and only when - both ``META_PIXEL_ID``
and ``META_ACCESS_TOKEN`` are configured. Every failure is reported as data, never
as an exception, and the access token never appears in logs or error strings.

Privacy: raw client IPs are never stored by this service (only a salted hash), so
``client_ip_address`` is deliberately omitted from ``user_data``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx

from app.config import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models import Member, MemberAttribution

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.facebook.com"
EVENT_NAME = "CompleteRegistration"
DISABLED_REASON = "META_PIXEL_ID/META_ACCESS_TOKEN not configured"
MAX_ERROR_LENGTH = 500

_NON_DIGITS = re.compile(r"\D")
_CUSTOM_DATA_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")


def is_enabled() -> bool:
    """True only when both the pixel id and the access token are configured."""
    return get_settings().meta_enabled


# --------------------------------------------------------------------------- helpers
def _sha256_hex(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def _hash_phone(phone: str | None) -> str | None:
    """SHA256 of the digits only (Meta expects a country-code number, no separators)."""
    if not phone:
        return None
    digits = _NON_DIGITS.sub("", str(phone))
    if not digits:
        return None
    return hashlib.sha256(digits.encode("utf-8")).hexdigest()


def _sanitize(message: str, secret: str) -> str:
    """Never let the access token escape through a log line or an error string."""
    text = str(message)
    if secret and secret in text:
        text = text.replace(secret, "***")
    return text[:MAX_ERROR_LENGTH]


def _response_text(response: Any) -> str:
    try:
        return str(response.text or "").strip()[:MAX_ERROR_LENGTH]
    except Exception:  # pragma: no cover - defensive
        return ""


def _event_id(member: Any) -> str:
    """Stable id so Meta can de-duplicate retries of the same conversion."""
    value = getattr(member, "id", None)
    return str(value) if value else str(uuid.uuid4())


def _custom_data(attribution: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for field in _CUSTOM_DATA_FIELDS:
        value = getattr(attribution, field, None)
        if value:
            data[field] = value
    return data


def build_event(
    member: Member, attribution: MemberAttribution | None, event_source_url: str | None = None
) -> dict[str, Any]:
    """Build a single Conversions API event (hashed, no raw PII, no IP)."""
    user_data: dict[str, Any] = {}
    email_hash = _sha256_hex(getattr(member, "email", None))
    if email_hash:
        user_data["em"] = [email_hash]
    phone_hash = _hash_phone(getattr(member, "phone", None))
    if phone_hash:
        user_data["ph"] = [phone_hash]
    for field in ("fbp", "fbc", "user_agent"):
        value = getattr(attribution, field, None)
        if value:
            key = "client_user_agent" if field == "user_agent" else field
            user_data[key] = value

    event: dict[str, Any] = {
        "event_name": EVENT_NAME,
        "event_time": int(time.time()),
        "action_source": "website",
        "event_id": _event_id(member),
        "user_data": user_data,
    }
    if event_source_url:
        event["event_source_url"] = event_source_url
    custom_data = _custom_data(attribution)
    if custom_data:
        event["custom_data"] = custom_data
    return event


# --------------------------------------------------------------------------- public API
def send_complete_registration(
    member: Member, attribution: MemberAttribution | None, event_source_url: str | None = None
) -> dict:
    """Send a ``CompleteRegistration`` event. Returns a status dict, never raises."""
    try:
        settings = get_settings()
        if not settings.meta_enabled:
            return {"status": "disabled", "reason": DISABLED_REASON}

        payload: dict[str, Any] = {"data": [build_event(member, attribution, event_source_url)]}
        if settings.meta_test_event_code:
            payload["test_event_code"] = settings.meta_test_event_code

        url = f"{GRAPH_BASE_URL}/{settings.meta_api_version}/{settings.meta_pixel_id}/events"
        params = {"access_token": settings.meta_access_token}
        logger.info("Sending Meta %s event for member %s", EVENT_NAME, _event_id(member))
        with httpx.Client(timeout=settings.meta_timeout_seconds) as client:
            response = client.post(url, params=params, json=payload)
    except Exception as exc:
        error = _sanitize(f"{exc.__class__.__name__}: {exc}", _configured_token())
        logger.warning("Meta Conversions API request failed: %s", error)
        return {"status": "failed", "http_status": None, "error": error}

    http_status = getattr(response, "status_code", None)
    if http_status is not None and 200 <= http_status < 300:
        logger.info("Meta %s event accepted (http_status=%s)", EVENT_NAME, http_status)
        return {"status": "sent", "http_status": http_status, "error": None}

    detail = _sanitize(_response_text(response), _configured_token())
    logger.warning("Meta Conversions API rejected the event (http_status=%s): %s", http_status, detail)
    return {
        "status": "failed",
        "http_status": http_status,
        "error": detail or f"HTTP {http_status}",
    }


def _configured_token() -> str:
    try:
        return get_settings().meta_access_token
    except Exception:  # pragma: no cover - defensive
        return ""
