"""FastAPI dependencies: CSRF, rate limits, API key, admin session."""

from __future__ import annotations

import hmac
from collections.abc import Callable

from fastapi import HTTPException, Request, status

from app.config import get_settings
from app.models import Member
from app.ratelimit import hit
from app.security import CSRF_COOKIE_NAME, CSRF_FORM_FIELD, client_ip, csrf_tokens_match, hash_ip


# --------------------------------------------------------------------------- CSRF
async def require_csrf(request: Request) -> None:
    """Double-submit cookie validation for browser (HTML form) POSTs."""
    settings = get_settings()
    if not settings.csrf_enabled:
        return
    form = await request.form()
    form_token = form.get(CSRF_FORM_FIELD)
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not csrf_tokens_match(cookie_token, str(form_token) if form_token is not None else None):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token không hợp lệ hoặc đã hết hạn. Vui lòng tải lại trang và thử lại.",
        )


# --------------------------------------------------------------------------- rate limiting
def enforce_rate_limit(request: Request, *, scope: str, limit: int, window_seconds: int) -> None:
    settings = get_settings()
    ip = client_ip(request, settings.trusted_proxy_headers) or "unknown"
    key = f"{scope}:{hash_ip(ip, settings.ip_hash_salt) or ip}"
    allowed, retry_after = hit(key, limit, window_seconds)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Bạn đã gửi quá nhiều yêu cầu. Vui lòng thử lại sau.",
            headers={"Retry-After": str(retry_after)},
        )


def rate_limit_dependency(scope: str, limit_attr: str, window_attr: str) -> Callable:
    async def _dependency(request: Request) -> None:
        settings = get_settings()
        enforce_rate_limit(
            request,
            scope=scope,
            limit=getattr(settings, limit_attr),
            window_seconds=getattr(settings, window_attr),
        )

    return _dependency


register_rate_limit = rate_limit_dependency(
    "register", "register_rate_limit", "register_rate_window_seconds"
)
api_rate_limit = rate_limit_dependency("api", "api_rate_limit", "api_rate_window_seconds")
login_rate_limit = rate_limit_dependency("login", "login_rate_limit", "login_rate_window_seconds")


# --------------------------------------------------------------------------- API key
async def require_api_key(request: Request) -> None:
    """Optional machine-to-machine auth: enforced only when MEMBER_API_KEY is configured."""
    settings = get_settings()
    if not settings.api_key_required:
        return
    provided = request.headers.get("x-api-key", "")
    if not provided or not hmac.compare_digest(provided, settings.member_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key không hợp lệ hoặc thiếu header X-API-Key.",
        )


# --------------------------------------------------------------------------- admin session
ADMIN_SESSION_KEY = "admin_authenticated"
ADMIN_EMAIL_KEY = "admin_email"


def admin_logged_in(request: Request) -> bool:
    return bool(request.session.get(ADMIN_SESSION_KEY))


async def require_admin(request: Request) -> None:
    """Guard for /admin/* pages. Redirects browsers to the login page."""
    if not admin_logged_in(request):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Cần đăng nhập",
            headers={"Location": f"{get_settings().public_base_url}/admin/login"},
        )


def load_last_member(request: Request, db) -> Member | None:
    """Member that was just verified in this browser session (used by /welcome)."""
    from app.services.members import get_member

    member_id = request.session.get("last_member_id")
    if not member_id:
        return None
    return get_member(db, member_id)
