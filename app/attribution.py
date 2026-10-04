"""Attribution capture: UTM, click ids, landing URL, referrer, fbp/fbc, hashed IP."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from fastapi import Request

from app.config import get_settings
from app.normalize import normalize_fbclid, normalize_url, normalize_utm
from app.security import client_ip, hash_ip
from app.services.members import AttributionInput

UTM_FIELDS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")


def _first(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _fbc_from_fbclid(fbclid: str | None) -> str | None:
    """Meta's documented format when the _fbc cookie is absent: fb.1.<ms>.<fbclid>."""
    if not fbclid:
        return None
    return f"fb.1.{int(time.time() * 1000)}.{fbclid}"


def attribution_from_request(
    request: Request, form: Mapping[str, Any] | None = None
) -> AttributionInput:
    """Collect attribution from the query string, the form, cookies and headers.

    The form/JS values are only a fallback: the server-side sources (query string,
    cookies, headers) win, so a tampered hidden field cannot forge the query string.
    """
    settings = get_settings()
    form = form or {}
    params = request.query_params

    def pick(field: str) -> str | None:
        return _first(params.get(field), form.get(field))

    cookies = request.cookies
    fbclid = _first(params.get("fbclid"), form.get("fbclid"))

    return AttributionInput(
        utm_source=normalize_utm(pick("utm_source")),
        utm_medium=normalize_utm(pick("utm_medium")),
        utm_campaign=normalize_utm(pick("utm_campaign")),
        utm_content=normalize_utm(pick("utm_content")),
        utm_term=normalize_utm(pick("utm_term")),
        landing_url=normalize_url(_first(form.get("landing_url"), str(request.url))),
        referrer=normalize_url(_first(form.get("referrer"), request.headers.get("referer"))),
        fbp=_first(form.get("fbp"), cookies.get("_fbp")),
        fbc=_first(form.get("fbc"), cookies.get("_fbc"), _fbc_from_fbclid(normalize_fbclid(fbclid))),
        user_agent=normalize_url(request.headers.get("user-agent"), max_length=512),
        ip_hash=hash_ip(client_ip(request, settings.trusted_proxy_headers), settings.ip_hash_salt),
    )


def attribution_defaults(request: Request) -> dict[str, str]:
    """Prefill values for the registration form (works without JavaScript)."""
    attribution = attribution_from_request(request)
    return {
        "utm_source": attribution.utm_source or "",
        "utm_medium": attribution.utm_medium or "",
        "utm_campaign": attribution.utm_campaign or "",
        "utm_content": attribution.utm_content or "",
        "utm_term": attribution.utm_term or "",
        "landing_url": attribution.landing_url or "",
        "referrer": attribution.referrer or "",
        "fbp": attribution.fbp or "",
        "fbc": attribution.fbc or "",
    }
