"""Public JSON API v1 - the integration surface for VIPORDER / VIP GROUP / other systems."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from app.attribution import attribution_from_request
from app.config import get_settings
from app.db import get_db
from app.deps import api_rate_limit, enforce_rate_limit, register_rate_limit, require_api_key
from app.models import Member
from app.normalize import NormalizationError
from app.schemas import (
    APIError,
    MemberDetailOut,
    MemberOut,
    RegisterRequest,
    ok,
)
from app.services.members import find_member_by_email, get_member, register_member, resend_verification

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["members"])

ALLOWED_SOURCES = {"web_form", "api", "viporder", "vipgroup", "import", "other"}


def _normalise_source(value: str | None) -> str:
    if not value:
        return "api"
    candidate = value.strip().lower()
    return candidate if candidate in ALLOWED_SOURCES else "other"


def _detail(db: Session, member: Member) -> dict:
    return MemberDetailOut.model_validate(member).model_dump(mode="json")


@router.get("/health", summary="Service health for integrations")
async def api_health(db: Session = Depends(get_db)) -> dict:
    from sqlalchemy import text

    settings = get_settings()
    database = "ok"
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # pragma: no cover - only on a broken database
        database = "error"
    return ok(
        {
            "status": "ok" if database == "ok" else "degraded",
            "app": settings.app_name,
            "env": settings.app_env,
            "database": database,
            "email_mode": settings.email_mode,
            "webhook_enabled": settings.webhook_enabled,
            "meta_enabled": settings.meta_enabled,
            "api_key_required": settings.api_key_required,
        }
    )


@router.post(
    "/members/register",
    status_code=status.HTTP_201_CREATED,
    summary="Register a member (creates a pending member and sends the verification email)",
    dependencies=[
        Depends(require_api_key),
        Depends(api_rate_limit),
        Depends(register_rate_limit),
    ],
)
async def api_register(
    payload: RegisterRequest, request: Request, db: Session = Depends(get_db)
) -> object:
    from fastapi.responses import JSONResponse

    try:
        outcome = register_member(
            db,
            full_name=payload.full_name,
            email=payload.email,
            phone=payload.phone,
            company=payload.company,
            consent_marketing=payload.consent_marketing,
            attribution=_attribution_from_body(payload, request),
            source=_normalise_source(payload.source),
        )
    except NormalizationError as exc:
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            str(exc),
            details=[{"field": exc.field or "body", "message": str(exc)}],
        ) from exc

    body = ok(
        {
            "member": MemberOut.model_validate(outcome.member).model_dump(mode="json"),
            "duplicate": outcome.duplicate,
            "verification_sent": outcome.verification_sent,
            # Never echo SMTP internals (host names, errno, credentials hints) to a caller;
            # the full text stays in the EMAIL_FAILED event metadata.
            "email_error": "send_failed" if outcome.email_error else None,
        },
        meta={"request_id": getattr(request.state, "request_id", None)},
    )
    code = status.HTTP_200_OK if outcome.duplicate else status.HTTP_201_CREATED
    return JSONResponse(status_code=code, content=body)


def _attribution_from_body(payload: RegisterRequest, request: Request):
    """Body fields win over cookies/headers for machine callers; missing ones are inferred."""
    inferred = attribution_from_request(request, payload.model_dump(exclude_none=True))
    return inferred


@router.get(
    "/members/{member_id}",
    summary="Fetch a member by id",
    dependencies=[Depends(require_api_key), Depends(api_rate_limit)],
)
async def api_get_member(member_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    member = get_member(db, _parse_uuid(member_id))
    if member is None:
        raise APIError(status.HTTP_404_NOT_FOUND, "not_found", "Member không tồn tại.")
    return ok(_detail(db, member), meta={"request_id": getattr(request.state, "request_id", None)})


@router.post(
    "/members/{member_id}/resend-verification",
    summary="Re-send the verification email for a pending member",
    dependencies=[Depends(require_api_key), Depends(api_rate_limit)],
)
async def api_resend_verification(
    member_id: str, request: Request, db: Session = Depends(get_db)
) -> dict:
    member = get_member(db, _parse_uuid(member_id))
    if member is None:
        raise APIError(status.HTTP_404_NOT_FOUND, "not_found", "Member không tồn tại.")
    # Per-member cap on top of the per-IP API limit: resending is cheap for an attacker and
    # expensive for the member (mail-bombing), so one member can be re-mailed at most 3x/hour.
    enforce_rate_limit(request, scope=f"resend:{member.id}", limit=3, window_seconds=3600)
    sent, error = resend_verification(db, member)
    return ok(
        {"member_id": str(member.id), "verification_sent": sent, "error": error},
        meta={"request_id": getattr(request.state, "request_id", None)},
    )


@router.get(
    "/members",
    summary="Look up a member by email (integration helper)",
    dependencies=[Depends(require_api_key), Depends(api_rate_limit)],
)
async def api_lookup_member(email: str, request: Request, db: Session = Depends(get_db)) -> dict:
    try:
        member = find_member_by_email(db, email)
    except NormalizationError as exc:
        # A malformed address is a client error: answer 422 with the documented
        # envelope instead of letting the normaliser escape as a 500.
        logger.debug("member lookup with an invalid email: %s", exc)
        raise APIError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "Email không hợp lệ",
            details=[{"field": exc.field or "email", "message": "Email không hợp lệ"}],
        ) from exc
    if member is None:
        raise APIError(status.HTTP_404_NOT_FOUND, "not_found", "Member không tồn tại.")
    return ok(_detail(db, member), meta={"request_id": getattr(request.state, "request_id", None)})


def _parse_uuid(value: str) -> uuid.UUID | str:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise APIError(status.HTTP_404_NOT_FOUND, "not_found", "Member không tồn tại.") from None
