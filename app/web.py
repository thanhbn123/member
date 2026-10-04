"""Template rendering helpers shared by every router."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import get_settings

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.trim_blocks = True
templates.env.lstrip_blocks = True


@dataclass(frozen=True, slots=True)
class Brand:
    """Everything a template may know about the customer's identity - all from config."""

    name: str
    tagline: str
    logo_url: str
    primary_color: str
    support_email: str
    favicon_url: str = ""

    def absolute_logo_url(self, base_url: str) -> str:
        """Absolute logo URL for contexts without a page base (email clients)."""
        if not self.logo_url:
            return ""
        if self.logo_url.startswith(("http://", "https://")):
            return self.logo_url
        return f"{base_url.rstrip('/')}/{self.logo_url.lstrip('/')}"


def get_brand() -> Brand:
    settings = get_settings()
    return Brand(
        name=settings.brand_name or settings.app_name,
        tagline=settings.brand_tagline,
        logo_url=settings.brand_logo_url,
        primary_color=settings.brand_primary_color,
        support_email=settings.brand_support_email,
        favicon_url=settings.brand_favicon_url or settings.brand_logo_url,
    )


def mask_email(email: str) -> str:
    """`nguyen.van.a@example.com` -> `ngu***@example.com` (never leak the full address)."""
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    visible = local[:1] if len(local) <= 2 else local[:3]
    return f"{visible}***@{domain}"


def base_context(request: Request) -> dict[str, Any]:
    settings = get_settings()
    return {
        "request": request,
        "brand": get_brand(),
        "app_name": settings.app_name,
        "public_base_url": settings.public_base_url,
        "ga4_measurement_id": settings.ga4_measurement_id,
        "meta_pixel_id": settings.meta_pixel_id,
        "year": datetime.now(UTC).year,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
    }


def render(
    request: Request,
    name: str,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    **extra: Any,
):
    context = base_context(request)
    context.update(extra)
    return templates.TemplateResponse(
        request, name, context, status_code=status_code, headers=headers
    )
