"""Input normalisation and validation helpers (server side, always applied)."""

from __future__ import annotations

import re

from email_validator import EmailNotValidError, validate_email

_WHITESPACE = re.compile(r"\s+")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PHONE_ALLOWED = re.compile(r"[^\d+]")


class NormalizationError(ValueError):
    """Raised when an input cannot be normalised into a valid value.

    ``field`` is the machine-readable field name (``email``, ``phone``, ...) so the
    JSON API can return ``error.details = [{"field": ..., "message": ...}]``.
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


def clean_text(
    value: str | None,
    *,
    max_length: int,
    required: bool = False,
    field: str = "value",
    field_name: str | None = None,
) -> str | None:
    """Strip control characters, collapse whitespace and enforce a length limit."""
    if value is None:
        if required:
            raise NormalizationError(f"{field} là bắt buộc", field_name)
        return None
    cleaned = _CONTROL_CHARS.sub("", str(value))
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    if not cleaned:
        if required:
            raise NormalizationError(f"{field} là bắt buộc", field_name)
        return None
    if len(cleaned) > max_length:
        raise NormalizationError(f"{field} tối đa {max_length} ký tự", field_name)
    return cleaned


def clamp_text(value: str | None, *, max_length: int) -> str | None:
    """Like :func:`clean_text`, but truncate instead of raising.

    Used for *attribution* only (UTM values, landing URL, referrer, user agent, click
    ids): those arrive from marketing links, are stored as metadata and are never
    rendered as HTML, so an over-long value must be silently shortened rather than
    turning an unauthenticated page view into a 500. Identity fields stay strict.
    """
    if value is None:
        return None
    cleaned = _CONTROL_CHARS.sub("", str(value))
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    if not cleaned:
        return None
    return cleaned[:max_length]


def normalize_email(value: str | None) -> str:
    """Lower-case, IDN-normalised, deliverable-shaped email address."""
    cleaned = clean_text(value, max_length=320, required=True, field="Email", field_name="email")
    assert cleaned is not None
    try:
        result = validate_email(cleaned, check_deliverability=False)
    except EmailNotValidError as exc:
        raise NormalizationError("Email không hợp lệ", "email") from exc
    normalized = result.normalized.lower()
    if len(normalized) > 320:
        raise NormalizationError("Email không hợp lệ", "email")
    return normalized


def normalize_phone(value: str | None) -> str | None:
    """Keep an optional leading '+', strip separators, validate digit count.

    Accepts local Vietnamese style numbers (leading 0) and E.164. No country
    assumption is baked into storage beyond digit count.
    """
    cleaned = clean_text(value, max_length=64, required=False, field="Số điện thoại", field_name="phone")
    if not cleaned:
        return None
    digits_only = _PHONE_ALLOWED.sub("", cleaned)
    has_plus = digits_only.startswith("+")
    digits = digits_only.lstrip("+")
    if not digits.isdigit():
        raise NormalizationError("Số điện thoại không hợp lệ", "phone")
    if not 8 <= len(digits) <= 15:
        raise NormalizationError("Số điện thoại phải có từ 8 đến 15 chữ số", "phone")
    return f"+{digits}" if has_plus else digits


def normalize_utm(value: str | None, *, max_length: int = 255) -> str | None:
    return clean_text(value, max_length=max_length, field="Giá trị UTM", field_name="utm")


def normalize_url(value: str | None, *, max_length: int = 2048) -> str | None:
    """Landing URL / referrer are stored as metadata only; they are never rendered as HTML."""
    return clean_text(value, max_length=max_length, field="URL", field_name="url")


def normalize_fbclid(value: str | None) -> str | None:
    return clean_text(value, max_length=255, field="fbclid", field_name="fbclid")
