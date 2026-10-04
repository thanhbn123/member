"""Public (browser) routes: landing, registration form, verification, welcome."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.attribution import attribution_defaults, attribution_from_request
from app.config import get_settings
from app.db import get_db
from app.deps import load_last_member, register_rate_limit, require_csrf
from app.normalize import NormalizationError, clean_text
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


@router.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse("/register", status_code=307)


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
