"""Public (browser) routes: landing, registration form, verification, welcome."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.attribution import attribution_defaults, attribution_from_request
from app.config import Settings, get_settings
from app.db import get_db
from app.deps import load_last_member, register_rate_limit, require_csrf
from app.normalize import NormalizationError, clamp_text, clean_text
from app.services.members import register_member, verify_email
from app.web import mask_email, render

logger = logging.getLogger(__name__)
router = APIRouter(tags=["public"])

FORM_FIELDS = ("full_name", "email", "phone", "company")

VERIFY_MESSAGES: dict[str, tuple[str, str]] = {
    "invalid": ("Liên kết không hợp lệ", "Liên kết xác minh không đúng hoặc đã bị thay đổi."),
    "used": ("Liên kết đã được sử dụng", "Liên kết này đã được dùng trước đó. Mỗi liên kết chỉ dùng được một lần."),
    "expired": ("Liên kết đã hết hạn", "Liên kết xác minh đã hết hạn. Vui lòng đăng ký lại để nhận liên kết mới."),
}

# --------------------------------------------------------------------------- landing page
#
# The landing page is configuration driven (see Settings.landing_*). The strings below
# are brand-neutral *defaults* for an unconfigured install - exactly like BRAND_NAME
# defaulting to "MEMBER" - so a fresh clone still renders a complete page. Nothing here
# names a customer, a product or a price. They are plain data: parse_benefits() and
# landing_context() never depend on the brand.

BENEFIT_LIMIT = 6  # hard cap: the grid is designed for at most 6 cards
BENEFIT_ICON_MAX = 8
BENEFIT_TITLE_MAX = 80
BENEFIT_DESCRIPTION_MAX = 240
HERO_TITLE_MAX = 160
HERO_SUBTITLE_MAX = 300
CTA_TEXT_MAX = 40
CONTACT_MAX = 200
DEFAULT_CTA_TEXT = "Đăng ký ngay"

DEFAULT_BENEFITS: tuple[tuple[str, str, str], ...] = (
    ("⚡", "Đăng ký nhanh", "Điền thông tin trong một phút, không cần tạo tài khoản."),
    ("✉", "Xác minh email", "Mở email và bấm liên kết xác minh để kích hoạt thành viên."),
    ("🎁", "Ưu đãi thành viên", "Nhận thông tin và ưu đãi dành riêng cho thành viên."),
)

LANDING_TRUST_BULLETS: tuple[str, ...] = (
    "Miễn phí đăng ký",
    "Bảo mật thông tin",
    "Không gửi email rác",
)

LANDING_STEPS: tuple[dict[str, str], ...] = (
    {"title": "Đăng ký", "description": "Điền họ tên và email của bạn vào biểu mẫu đăng ký."},
    {"title": "Xác minh email", "description": "Mở email xác minh và bấm vào liên kết một lần chúng tôi gửi."},
    {"title": "Nhận ưu đãi", "description": "Tư cách thành viên được kích hoạt và thông tin ưu đãi được gửi đến bạn."},
)

LANDING_FAQ: tuple[dict[str, str], ...] = (
    {
        "q": "Đăng ký thành viên có mất phí không?",
        "a": "Không. Đăng ký hoàn toàn miễn phí, bạn chỉ cần một địa chỉ email hợp lệ.",
    },
    {
        "q": "Vì sao tôi cần xác minh email?",
        "a": "Liên kết xác minh giúp chắc chắn địa chỉ email là của bạn, nhờ đó không ai đăng ký thay bạn.",
    },
    {
        "q": "Thông tin của tôi được dùng để làm gì?",
        "a": "Chỉ để gửi thông tin và ưu đãi thành viên. Bạn có thể ngừng nhận email bất cứ lúc nào.",
    },
)

_TEL_ALLOWED = re.compile(r"[^0-9+]")


def parse_benefits(raw: str | None, *, limit: int = BENEFIT_LIMIT) -> list[dict[str, str]]:
    """Parse ``LANDING_BENEFITS`` into at most ``limit`` benefit cards.

    Format: ``icon|title|description`` items separated by ``;;``. Malformed items
    (missing/extra separators, empty fields) are dropped silently and over-long text is
    truncated, so a bad configuration can never 500 the public page. An empty - or fully
    malformed - value falls back to the brand-neutral :data:`DEFAULT_BENEFITS` trio.
    """
    items: list[dict[str, str]] = []
    for chunk in (raw or "").split(";;"):
        if len(items) >= limit:
            break
        parts = chunk.split("|")
        if len(parts) != 3:
            continue
        icon = clamp_text(parts[0], max_length=BENEFIT_ICON_MAX)
        title = clamp_text(parts[1], max_length=BENEFIT_TITLE_MAX)
        description = clamp_text(parts[2], max_length=BENEFIT_DESCRIPTION_MAX)
        if not (icon and title and description):
            continue
        items.append({"icon": icon, "title": title, "description": description})
    if items:
        return items
    return [
        {"icon": icon, "title": title, "description": description}
        for icon, title, description in DEFAULT_BENEFITS
    ]


def safe_http_url(value: str | None) -> str:
    """Return ``value`` only when it is a real ``http(s)`` URL, otherwise ``""``.

    Second line of defence behind ``Settings._http_url_only``: whatever the source of a
    configured URL, a template never receives ``javascript:``/``data:`` for an ``href``.
    """
    candidate = (value or "").strip()
    if not candidate:
        return ""
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    return candidate


def tel_href(value: str | None) -> str:
    """Build a ``tel:`` href from a configured phone number (``""`` when unusable)."""
    digits = _TEL_ALLOWED.sub("", value or "")
    if len(digits.lstrip("+")) < 6:
        return ""
    return f"tel:{digits}"


def landing_context(settings: Settings | None = None) -> dict[str, Any]:
    """Landing-page copy parsed from configuration (never raises, never brand-specific)."""
    settings = settings or get_settings()
    hero_title = (
        clamp_text(settings.landing_hero_title, max_length=HERO_TITLE_MAX)
        or clamp_text(settings.brand_tagline, max_length=HERO_TITLE_MAX)
        or settings.brand_name
    )
    phone = clamp_text(settings.brand_phone, max_length=64) or ""
    return {
        "hero_title": hero_title,
        "hero_subtitle": clamp_text(settings.landing_hero_subtitle, max_length=HERO_SUBTITLE_MAX) or "",
        "hero_image_url": safe_http_url(settings.landing_hero_image_url),
        "benefits": parse_benefits(settings.landing_benefits),
        "cta_text": clamp_text(settings.landing_cta_text, max_length=CTA_TEXT_MAX) or DEFAULT_CTA_TEXT,
        "show_form": settings.landing_show_form,
        "trust_bullets": LANDING_TRUST_BULLETS,
        "steps": LANDING_STEPS,
        "faq": LANDING_FAQ,
        "contact": {
            "phone": phone,
            "phone_href": tel_href(phone),
            "address": clamp_text(settings.brand_address, max_length=CONTACT_MAX) or "",
            "facebook_url": safe_http_url(settings.brand_facebook_url),
            "zalo_url": safe_http_url(settings.brand_zalo_url),
        },
    }


@router.get("/", include_in_schema=False)
async def index(request: Request) -> object:
    """Public landing page.

    It embeds the very same registration form as ``/register`` (identical fields, POST
    target, hidden attribution inputs and CSRF token), so a conversion straight from the
    landing page is indistinguishable from one on the ad landing page.
    """
    settings = get_settings()
    return render(
        request,
        "landing.html",
        form={},
        errors=[],
        attribution=attribution_defaults(request),
        success_message=None,
        error_message=None,
        consent_required=not settings.debug,
        **landing_context(settings),
    )


@router.get("/health", include_in_schema=False)
async def health(db: Session = Depends(get_db)) -> dict:
    """Liveness/readiness probe - never exposes secrets."""
    from sqlalchemy import text

    settings = get_settings()
    database = "ok"
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # pragma: no cover - only on a broken database
        database = "error"
    return {
        "status": "ok" if database == "ok" else "degraded",
        "app": settings.app_name,
        "env": settings.app_env,
        "version": __import__("app").__version__,
        "database": database,
    }


@router.get("/register", include_in_schema=False)
async def register_form(request: Request) -> object:
    settings = get_settings()
    return render(
        request,
        "register.html",
        form={},
        errors=[],
        attribution=attribution_defaults(request),
        success_message=None,
        error_message=None,
        consent_required=not settings.debug,
    )


@router.post("/register", include_in_schema=False, dependencies=[Depends(require_csrf), Depends(register_rate_limit)])
async def register_submit(request: Request, db: Session = Depends(get_db)) -> object:
    form = dict(await request.form())
    submitted = {key: str(form.get(key) or "") for key in FORM_FIELDS}
    consent = str(form.get("consent_marketing") or "").lower() in {"1", "true", "on", "yes"}

    errors: list[str] = []
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    try:
        full_name = clean_text(submitted["full_name"], max_length=200, required=True, field="Họ tên")
        email = submitted["email"]
        phone = clean_text(submitted["phone"], max_length=64, field="Số điện thoại")
        company = clean_text(submitted["company"], max_length=200, field="Công ty")
    except NormalizationError as exc:
        errors.append(str(exc))

    if not errors:
        try:
            outcome = register_member(
                db,
                full_name=full_name or "",
                email=email or "",
                phone=phone,
                company=company,
                consent_marketing=consent,
                attribution=attribution_from_request(request, form),
                source="web_form",
            )
        except NormalizationError as exc:
            errors.append(str(exc))
        else:
            # Do NOT remember the member here: /welcome must stay unreachable while the
            # address is still pending. ``last_member_id`` is set after a successful
            # verification (see /verify-email), which is the proof of ownership.
            target = f"/check-email?email={masked_query(outcome.member.email)}"
            if not outcome.verification_sent:
                target += "&sent=0"
            return RedirectResponse(target, status_code=303)

    return render(
        request,
        "register.html",
        status_code=422,
        form={**submitted, "consent_marketing": consent},
        errors=errors,
        attribution=attribution_defaults(request),
        success_message=None,
        error_message=None,
        consent_required=True,
    )


def masked_query(email: str) -> str:
    from urllib.parse import quote

    return quote(mask_email(email))


@router.get("/check-email", include_in_schema=False)
async def check_email(request: Request, email: str = "", sent: int = 1) -> object:
    return render(
        request,
        "check_email.html",
        email_masked=email or "",
        resend_url="/register",
        email_sent=bool(sent),
    )


@router.get("/verify-email", include_in_schema=False)
async def verify_email_route(
    request: Request, token: str = "", db: Session = Depends(get_db)
) -> object:
    outcome = verify_email(db, token)

    if outcome.status in {"verified", "already_verified"} and outcome.member is not None:
        request.session["last_member_id"] = str(outcome.member.id)
        return render(
            request,
            "verify_result.html",
            success=True,
            title="Xác minh thành công",
            message="Email của bạn đã được xác minh. Chào mừng bạn đến với "
            f"{get_settings().brand_name}!",
            member=outcome.member,
        )

    title, message = VERIFY_MESSAGES.get(outcome.status, VERIFY_MESSAGES["invalid"])
    status_code = 410 if outcome.status == "expired" else 400
    return render(
        request,
        "verify_result.html",
        status_code=status_code,
        success=False,
        title=title,
        message=message,
        member=outcome.member,
    )


@router.get("/welcome", include_in_schema=False)
async def welcome(request: Request, db: Session = Depends(get_db)) -> object:
    """Member welcome page - only the *verified* member is presented as a member.

    The session key is written by /verify-email, so a pending (or unknown) visitor gets
    the "verify your email" state instead of an empty success page.
    """
    member = load_last_member(request, db)
    verified = bool(member is not None and member.is_verified)
    return render(
        request,
        "welcome.html",
        member=member,
        verified=verified,
        email_masked=mask_email(member.email) if member is not None else "",
        cta_url=get_settings().public_base_url,
    )


@router.get("/robots.txt", include_in_schema=False)
async def robots() -> object:
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse("User-agent: *\nDisallow: /admin\nDisallow: /verify-email\n")
