"""Member lifecycle: registration, verification token issuing, email verification.

All transactions are owned here. Side effects (email, webhook, Meta) happen *after*
the database commit and can never roll a member back or break the verification flow.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.email import send_verification_email
from app.models import (
    EmailVerificationToken,
    EventType,
    Member,
    MemberAttribution,
    MemberStatus,
    utcnow,
)
from app.normalize import normalize_email, normalize_phone
from app.security import generate_token, hash_token
from app.services.events import record_event

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AttributionInput:
    """Everything we are allowed to know about where a registration came from."""

    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    utm_content: str | None = None
    utm_term: str | None = None
    landing_url: str | None = None
    referrer: str | None = None
    fbp: str | None = None
    fbc: str | None = None
    user_agent: str | None = None
    ip_hash: str | None = None


@dataclass(slots=True)
class RegisterOutcome:
    member: Member
    duplicate: bool
    verification_sent: bool
    verification_url: str | None
    email_error: str | None


@dataclass(slots=True)
class VerifyOutcome:
    status: str  # "verified" | "already_verified" | "invalid" | "expired" | "used"
    member: Member | None


# --------------------------------------------------------------------------- lookups
def find_member_by_email(db: Session, email: str) -> Member | None:
    normalized = normalize_email(email)
    return db.execute(select(Member).where(Member.email == normalized)).scalar_one_or_none()


def get_member(db: Session, member_id: uuid.UUID | str) -> Member | None:
    try:
        key = member_id if isinstance(member_id, uuid.UUID) else uuid.UUID(str(member_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return db.get(Member, key)


# --------------------------------------------------------------------------- tokens
def issue_verification_token(db: Session, member: Member) -> tuple[str, EmailVerificationToken]:
    """Create a fresh token and invalidate any unused token of that member.

    Superseding keeps exactly one usable link per member: an old email can never be
    replayed after a newer one was requested.
    """
    settings = get_settings()
    now = utcnow()
    db.execute(
        update(EmailVerificationToken)
        .where(EmailVerificationToken.member_id == member.id, EmailVerificationToken.used_at.is_(None))
        .values(used_at=now)
    )
    raw_token = generate_token()
    token = EmailVerificationToken(
        member_id=member.id,
        token_hash=hash_token(raw_token),
        expires_at=now + timedelta(hours=settings.verification_token_ttl_hours),
    )
    db.add(token)
    db.flush()
    return raw_token, token


def verification_url(raw_token: str) -> str:
    base = get_settings().public_base_url.rstrip("/")
    return f"{base}/verify-email?token={raw_token}"


# --------------------------------------------------------------------------- attribution
def _attribution_values(attribution: AttributionInput) -> dict:
    return {
        "utm_source": attribution.utm_source,
        "utm_medium": attribution.utm_medium,
        "utm_campaign": attribution.utm_campaign,
        "utm_content": attribution.utm_content,
        "utm_term": attribution.utm_term,
        "landing_url": attribution.landing_url,
        "referrer": attribution.referrer,
        "fbp": attribution.fbp,
        "fbc": attribution.fbc,
        "user_agent": attribution.user_agent,
        "ip_hash": attribution.ip_hash,
    }


def _apply_attribution(db: Session, member: Member, attribution: AttributionInput | None) -> None:
    """First-touch wins; a later registration only fills fields that are still missing."""
    if attribution is None:
        return
    values = _attribution_values(attribution)
    existing = member.attribution
    if existing is None:
        db.add(MemberAttribution(member_id=member.id, **values))
        db.flush()
        return
    for field, value in values.items():
        if value and not getattr(existing, field):
            setattr(existing, field, value)


# --------------------------------------------------------------------------- registration
def register_member(
    db: Session,
    *,
    full_name: str,
    email: str,
    phone: str | None = None,
    company: str | None = None,
    consent_marketing: bool = False,
    attribution: AttributionInput | None = None,
    source: str = "web_form",
) -> RegisterOutcome:
    """Create (or re-notify) a pending member and send the verification email.

    Raises ``NormalizationError`` when the input cannot be normalised.
    """
    normalized_email = normalize_email(email)
    normalized_phone = normalize_phone(phone)
    started_metadata = {"source": source, "utm_source": attribution.utm_source if attribution else None}

    existing = db.execute(select(Member).where(Member.email == normalized_email)).scalar_one_or_none()

    if existing is not None:
        started_metadata["duplicate"] = True
        record_event(db, EventType.REGISTER_STARTED, existing.id, started_metadata)
        _apply_attribution(db, existing, attribution)

        if existing.status == MemberStatus.VERIFIED.value:
            db.commit()
            return RegisterOutcome(
                member=existing,
                duplicate=True,
                verification_sent=False,
                verification_url=None,
                email_error=None,
            )

        raw_token, _ = issue_verification_token(db, existing)
        db.commit()
        url = verification_url(raw_token)
        sent, error = _deliver_verification(db, existing, url)
        return RegisterOutcome(
            member=existing,
            duplicate=True,
            verification_sent=sent,
            verification_url=url,
            email_error=error,
        )

    # The id is generated up front so the very first event is attached to the member: the
    # admin timeline then shows the whole funnel. The event is written *after* the member row
    # exists, because member_events.member_id is a foreign key.
    member_id = uuid.uuid4()
    member = Member(
        id=member_id,
        full_name=full_name,
        email=normalized_email,
        phone=normalized_phone,
        company=company or None,
        status=MemberStatus.PENDING.value,
        consent_marketing=bool(consent_marketing),
        source=source,
    )
    db.add(member)
    try:
        db.flush()
    except IntegrityError:
        # Two concurrent registrations for the same address: the unique index on
        # members.email wins the race. Fall back to the duplicate path instead of 500.
        db.rollback()
        existing = db.execute(
            select(Member).where(Member.email == normalized_email)
        ).scalar_one_or_none()
        if existing is None:  # pragma: no cover - the row vanished between the two queries
            raise
        return register_member(
            db,
            full_name=full_name,
            email=normalized_email,
            phone=phone,
            company=company,
            consent_marketing=consent_marketing,
            attribution=attribution,
            source=source,
        )

    record_event(db, EventType.REGISTER_STARTED, member_id, started_metadata)
    _apply_attribution(db, member, attribution)
    raw_token, _ = issue_verification_token(db, member)
    record_event(db, EventType.REGISTER_COMPLETED, member.id, {"source": source})
    db.commit()

    url = verification_url(raw_token)
    sent, error = _deliver_verification(db, member, url)
    return RegisterOutcome(
        member=member,
        duplicate=False,
        verification_sent=sent,
        verification_url=url,
        email_error=error,
    )


def resend_verification(db: Session, member: Member) -> tuple[bool, str | None]:
    """Issue a new token and re-send the verification email. Returns (sent, error)."""
    if member.status == MemberStatus.VERIFIED.value:
        return False, "already_verified"
    raw_token, _ = issue_verification_token(db, member)
    db.commit()
    return _deliver_verification(db, member, verification_url(raw_token))


def _deliver_verification(db: Session, member: Member, url: str) -> tuple[bool, str | None]:
    """Send the email and log the outcome. Never raises, never fails the registration."""
    try:
        result = send_verification_email(member, url)
    except Exception as exc:  # defensive: the email backend must not break registration
        logger.exception("verification email crashed for member %s", member.id)
        result_error = f"{type(exc).__name__}: {exc}"
        result_sent, backend, result_detail = False, "unknown", None
    else:
        result_error, result_sent, backend = result.error, result.sent, result.backend
        result_detail = result.detail

    try:
        if result_sent:
            record_event(
                db,
                EventType.EMAIL_SENT,
                member.id,
                {"backend": backend, "provider_response": result_detail},
            )
        else:
            record_event(db, EventType.EMAIL_FAILED, member.id, {"backend": backend, "error": result_error})
        db.commit()
    except Exception:  # pragma: no cover - event logging must never break the request
        logger.exception("could not persist email event for member %s", member.id)
        db.rollback()
    return result_sent, result_error


# --------------------------------------------------------------------------- verification
def verify_email(db: Session, raw_token: str) -> VerifyOutcome:
    """Atomically consume a one-time token and mark the member verified."""
    if not raw_token:
        return VerifyOutcome(status="invalid", member=None)

    token_hash = hash_token(raw_token)
    now = utcnow()
    row = db.execute(
        select(EmailVerificationToken).where(EmailVerificationToken.token_hash == token_hash)
    ).scalar_one_or_none()

    if row is None:
        return VerifyOutcome(status="invalid", member=None)
    if row.used_at is not None:
        member = db.get(Member, row.member_id)
        return VerifyOutcome(status="used", member=member)
    if row.expires_at <= now:
        member = db.get(Member, row.member_id)
        return VerifyOutcome(status="expired", member=member)

    # Atomic claim: exactly one concurrent request can flip used_at from NULL.
    claimed = db.execute(
        update(EmailVerificationToken)
        .where(
            EmailVerificationToken.id == row.id,
            EmailVerificationToken.used_at.is_(None),
            EmailVerificationToken.expires_at > now,
        )
        .values(used_at=now)
    )
    if claimed.rowcount != 1:
        db.rollback()
        return VerifyOutcome(status="used", member=db.get(Member, row.member_id))

    member = db.get(Member, row.member_id)
    if member is None:  # pragma: no cover - FK cascade makes this unreachable
        db.rollback()
        return VerifyOutcome(status="invalid", member=None)

    if member.status == MemberStatus.VERIFIED.value and member.email_verified_at is not None:
        db.commit()
        return VerifyOutcome(status="already_verified", member=member)

    member.status = MemberStatus.VERIFIED.value
    member.email_verified_at = now
    record_event(db, EventType.EMAIL_VERIFIED, member.id, {"token_id": row.id})
    db.commit()

    _notify_verified(member)
    return VerifyOutcome(status="verified", member=member)


def _notify_verified(member: Member) -> None:
    """Fire outbound integrations after commit. Failures are recorded, never raised."""
    try:
        from app.integrations.dispatch import notify_member_verified
    except Exception:  # pragma: no cover - integrations are optional
        logger.exception("integration dispatch unavailable")
        return
    try:
        with session_scope() as db:
            fresh = db.get(Member, member.id)
            if fresh is not None:
                notify_member_verified(fresh, db)
    except Exception:  # pragma: no cover - defence in depth
        logger.exception("integration dispatch failed for member %s", member.id)
