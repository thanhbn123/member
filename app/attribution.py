"""Attribution capture: UTM, click ids, landing URL, referrer, fbp/fbc, hashed IP.

Attribution is *not* user-supplied identity data: it comes from marketing links,
cookies and headers, and it is rendered back into the registration form on an
unauthenticated GET. Every field below is therefore **clamped, never rejected** -
an over-long ``?utm_source=`` or ``Referer`` must not be able to turn a page view
or a registration into a 500 (the strict validators in ``app.normalize`` stay in
charge of email / phone / full_name / company, which a client can fix and retry).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from fastapi import Request

from app.config import get_settings
from app.normalize import clamp_text
from app.security import client_ip, hash_ip
from app.services.members import AttributionInput

logger = logging.getLogger(__name__)

UTM_MAX_LENGTH = 255  # member_attribution.utm_* is String(255)
URL_MAX_LENGTH = 2048  # browsers keep URLs well below this; Text column on purpose
USER_AGENT_MAX_LENGTH = 512  # enough to fingerprint a browser, short of a DoS vector
FBCLID_MAX_LENGTH = 255  # member_attribution.fbc is String(255)
CLICK_ID_MAX_LENGTH = 255  # fbp / fbc cookies are String(255) as well

FORM_FIELDS = (
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


def _first(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def clamp_utm(value: str | None) -> str | None:
    """Truncate a UTM value to the stored column width (silent, never raises)."""
    return clamp_text(value, max_length=UTM_MAX_LENGTH)


def clamp_url(value: str | None) -> str | None:
    """Truncate a landing URL / referrer (silent, never raises)."""
    return clamp_text(value, max_length=URL_MAX_LENGTH)


def clamp_user_agent(value: str | None) -> str | None:
    """Truncate a User-Agent header (silent, never raises)."""
    return clamp_text(value, max_length=USER_AGENT_MAX_LENGTH)


def clamp_fbclid(value: str | None) -> str | None:
    """Truncate a Meta click id (silent, never raises)."""
    return clamp_text(value, max_length=FBCLID_MAX_LENGTH)


def clamp_click_id(value: str | None) -> str | None:
    """Truncate an fbp/fbc cookie or form value to the stored column width."""
    return clamp_text(value, max_length=CLICK_ID_MAX_LENGTH)


def _fbc_from_fbclid(fbclid: str | None) -> str | None:
    """Meta's documented format when the _fbc cookie is absent: fb.1.<ms>.<fbclid>."""
    if not fbclid:
        return None
    return clamp_click_id(f"fb.1.{int(time.time() * 1000)}.{fbclid}")


def attribution_from_request(
    request: Request, form: Mapping[str, Any] | None = None
) -> AttributionInput:
    """Collect attribution from the query string, the form, cookies and headers.

    The form/JS values are only a fallback: the server-side sources (query string,
    cookies, headers) win, so a tampered hidden field cannot forge the query string.

    Never raises: every value goes through a clamping helper, so hostile-but-plausible
    marketing input (256-char UTM, 2 KB referrer, 600-char User-Agent, ...) is stored
    truncated instead of surfacing as an unauthenticated 500.
    """
    settings = get_settings()
    form = form or {}
    params = request.query_params

    def pick(field: str) -> str | None:
        return _first(params.get(field), form.get(field))

    cookies = request.cookies
    fbclid = clamp_fbclid(_first(params.get("fbclid"), form.get("fbclid")))

    return AttributionInput(
        utm_source=clamp_utm(pick("utm_source")),
        utm_medium=clamp_utm(pick("utm_medium")),
        utm_campaign=clamp_utm(pick("utm_campaign")),
        utm_content=clamp_utm(pick("utm_content")),
        utm_term=clamp_utm(pick("utm_term")),
        landing_url=clamp_url(_first(form.get("landing_url"), str(request.url))),
        referrer=clamp_url(_first(form.get("referrer"), request.headers.get("referer"))),
        fbp=clamp_click_id(_first(form.get("fbp"), cookies.get("_fbp"))),
        fbc=clamp_click_id(_first(form.get("fbc"), cookies.get("_fbc"), _fbc_from_fbclid(fbclid))),
        user_agent=clamp_user_agent(request.headers.get("user-agent")),
        ip_hash=hash_ip(client_ip(request, settings.trusted_proxy_headers), settings.ip_hash_salt),
    )


def attribution_defaults(request: Request) -> dict[str, str]:
    """Prefill values for the registration form (works without JavaScript).

    This runs on an unauthenticated GET, so it is defensive by construction: even an
    unexpected failure anywhere in the capture degrades to empty fields, never a 500.
    """
    try:
        attribution = attribution_from_request(request)
    except Exception:  # pragma: no cover - defence in depth for a public page
        logger.exception("attribution capture failed; using empty defaults")
        return dict.fromkeys(FORM_FIELDS, "")
    return {field: getattr(attribution, field) or "" for field in FORM_FIELDS}
