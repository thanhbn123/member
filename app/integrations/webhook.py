"""Verified-member webhook delivery.

The payload is signed with ``app.security.sign_payload`` over the *exact* bytes
that are sent, so receivers can verify both integrity and freshness::

    X-Member-Signature: sha256=<hmac(secret, "<timestamp>." + body)>

Delivery retries ``WEBHOOK_MAX_ATTEMPTS`` times with exponential backoff. Failures
are reported as data; this module never raises.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from enum import Enum
from typing import TYPE_CHECKING, Any

import httpx

from app.config import get_settings
from app.security import iso_timestamp, sign_payload

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models import Member

logger = logging.getLogger(__name__)

EVENT_NAME = "member.verified"
MAX_ERROR_LENGTH = 500
_ATTRIBUTION_FIELDS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "landing_url",
    "referrer",
    "fbp",
    "fbc",
)


def is_enabled() -> bool:
    """True only when both the webhook URL and the signing secret are configured."""
    return get_settings().webhook_enabled


# --------------------------------------------------------------------------- helpers
def _sleep(seconds: float) -> None:
    """Indirection so tests can replace the wait; a failed wait never aborts delivery."""
    if seconds > 0:
        try:
            time.sleep(seconds)
        except Exception:  # pragma: no cover - interrupted waits must not raise
            logger.debug("Sleep between webhook attempts was interrupted", exc_info=True)


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _sanitize(message: Any, secret: str) -> str:
    text = str(message)
    if secret and secret in text:
        text = text.replace(secret, "***")
    return text[:MAX_ERROR_LENGTH]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    return str(value)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return iso_timestamp(value)
    except Exception:  # pragma: no cover - defensive
        return str(value)


def _safe_attribution(member: Any) -> dict[str, Any]:
    """Attribution as a plain dict; a detached/unloaded relationship yields ``{}``.

    ``ip_hash`` is intentionally never published: it is a salted hash of the
    visitor IP and the receiver has no use for it.
    """
    try:
        attribution = getattr(member, "attribution", None)
    except Exception:
        return {}
    if attribution is None:
        return {}
    data: dict[str, Any] = {}
    for field in _ATTRIBUTION_FIELDS:
        value = getattr(attribution, field, None)
        if value:
            data[field] = value
    return data


def build_payload(member: Member, occurred_at: str | None = None) -> dict[str, Any]:
    """Payload for ``member.verified`` (no secrets, no raw IP)."""
    return {
        "event": EVENT_NAME,
        "occurred_at": occurred_at or iso_timestamp(),
        "member": {
            "id": str(getattr(member, "id", "") or ""),
            "full_name": getattr(member, "full_name", None),
            "email": getattr(member, "email", None),
            "phone": getattr(member, "phone", None),
            "company": getattr(member, "company", None),
            "status": _text(getattr(member, "status", None)),
            "consent_marketing": bool(getattr(member, "consent_marketing", False)),
            "email_verified_at": _iso(getattr(member, "email_verified_at", None)),
            "created_at": _iso(getattr(member, "created_at", None)),
        },
        "attribution": _safe_attribution(member),
    }


def _headers(secret: str, timestamp: str, body: bytes, delivery_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Member-Event": EVENT_NAME,
        "X-Member-Timestamp": timestamp,
        "X-Member-Signature": sign_payload(secret, timestamp, body),
        "X-Member-Delivery": delivery_id,
    }


# --------------------------------------------------------------------------- public API
def send_member_verified(member: Member) -> dict:
    """POST the verified-member payload. Returns a status dict, never raises."""
    started = time.perf_counter()
    try:
        settings = get_settings()
        if not settings.webhook_enabled:
            # Nothing configured: no network call, no delivery attempt to report.
            return {"status": "disabled", "attempts": 0}

        url = settings.member_verified_webhook_url
        secret = settings.member_verified_webhook_secret
        max_attempts = max(1, int(settings.webhook_max_attempts or 1))
        backoff = max(0.0, float(settings.webhook_backoff_seconds or 0))

        body = json.dumps(build_payload(member), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        delivery_id = str(uuid.uuid4())

        http_status: int | None = None
        error: str | None = None
        attempt = 0

        with httpx.Client(timeout=settings.webhook_timeout_seconds) as client:
            for attempt in range(1, max_attempts + 1):
                timestamp = iso_timestamp()
                headers = _headers(secret, timestamp, body, delivery_id)
                try:
                    response = client.post(url, content=body, headers=headers)
                except Exception as exc:
                    http_status = None
                    error = _sanitize(f"{exc.__class__.__name__}: {exc}", secret)
                else:
                    http_status = getattr(response, "status_code", None)
                    if http_status is not None and 200 <= http_status < 300:
                        logger.info(
                            "Webhook %s delivered in %s attempt(s) (http_status=%s)",
                            EVENT_NAME,
                            attempt,
                            http_status,
                        )
                        return {
                            "status": "sent",
                            "attempts": attempt,
                            "http_status": http_status,
                            "error": None,
                            "duration_ms": _ms(started),
                        }
                    error = f"HTTP {http_status}" if http_status is not None else "invalid response object"
                if attempt < max_attempts:
                    _sleep(backoff * (2 ** (attempt - 1)))

        logger.warning("Webhook %s delivery failed after %s attempt(s): %s", EVENT_NAME, attempt, error)
        return {
            "status": "failed",
            "attempts": attempt,
            "http_status": http_status,
            "error": error,
            "duration_ms": _ms(started),
        }
    except Exception as exc:  # the caller (verification) must never see this
        logger.exception("Webhook %s delivery raised unexpectedly", EVENT_NAME)
        return {
            "status": "failed",
            "attempts": 0,
            "http_status": None,
            "error": _sanitize(f"{exc.__class__.__name__}: {exc}", ""),
            "duration_ms": _ms(started),
        }
