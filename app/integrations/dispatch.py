"""Post-verification fan-out: verified-member webhook + Meta Conversions API.

``notify_member_verified`` is called *after* the verification transaction has been
committed, and it must never raise: a broken webhook, a broken Meta call or even a
broken event insert can never undo (or block) an email verification.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.integrations import meta, webhook
from app.models import EventType
from app.services.events import record_event

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from app.models import Member

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 500
MAX_METADATA_LENGTH = 500

Result = dict[str, Any]
Runner = Callable[[Any], Result]


# --------------------------------------------------------------------------- runners
def _run_webhook(member: Member) -> Result:
    return webhook.send_member_verified(member)


def _run_meta(member: Member) -> Result:
    attribution = _attribution_for(member)
    event_source_url = getattr(attribution, "landing_url", None) or None
    return meta.send_complete_registration(member, attribution, event_source_url=event_source_url)


#: (name, runner, success event, failure event) - runners resolve the integration
#: modules at call time so tests can monkeypatch either side.
_TRACKED: tuple[tuple[str, Runner, EventType, EventType], ...] = (
    ("webhook", _run_webhook, EventType.WEBHOOK_SENT, EventType.WEBHOOK_FAILED),
    ("meta", _run_meta, EventType.META_EVENT_SENT, EventType.META_EVENT_FAILED),
)


# --------------------------------------------------------------------------- helpers
def _attribution_for(member: Any) -> Any:
    try:
        return getattr(member, "attribution", None)
    except Exception:
        return None


def _short_error(exc: BaseException) -> str:
    return f"{exc.__class__.__name__}: {exc}"[:MAX_ERROR_LENGTH]


def _member_id(member: Any) -> uuid.UUID | None:
    value = getattr(member, "id", None)
    return value if isinstance(value, uuid.UUID) else None


def _metadata(result: Result) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in ("status", "attempts", "http_status", "duration_ms", "error", "reason"):
        value = result.get(key)
        if value is None:
            continue
        if not isinstance(value, (str, int, float, bool)):
            value = str(value)
        if isinstance(value, str):
            value = value[:MAX_METADATA_LENGTH]
        metadata[key] = value
    return metadata


def _safe_call(name: str, runner: Runner, member: Any) -> Result:
    try:
        result = runner(member)
    except Exception as exc:
        logger.exception("Integration '%s' raised; verification is unaffected", name)
        return {"status": "failed", "error": _short_error(exc)}
    if not isinstance(result, dict):
        return {"status": "failed", "error": f"unexpected result type: {type(result).__name__}"}
    return result


def _record(
    db: Session, result: Result, member_id: uuid.UUID | None, sent: EventType, failed: EventType
) -> None:
    """Audit the attempt; a DB problem here must not escape either."""
    status = result.get("status")
    if status == "sent":
        event_type = sent
    elif status == "failed":
        event_type = failed
    else:  # disabled: nothing happened, keep the audit log clean
        return
    try:
        record_event(db, event_type, member_id, _metadata(result))
        db.commit()
    except Exception:
        logger.exception("Could not record %s event", event_type.value)
        try:
            db.rollback()
        except Exception:
            logger.debug("Rollback after a failed event insert also failed", exc_info=True)


# --------------------------------------------------------------------------- public API
def notify_member_verified(member: Member, db: Session | None = None) -> list[dict]:
    """Fire webhook then Meta, record the outcomes, and never raise."""
    results: list[dict] = []
    try:
        member_id = _member_id(member)
        for name, runner, sent_event, failed_event in _TRACKED:
            result = _safe_call(name, runner, member)
            results.append(result)
            if db is not None:
                _record(db, result, member_id, sent_event, failed_event)
    except Exception:  # absolute last line of defence
        logger.exception("Integration dispatch failed unexpectedly; verification is unaffected")
    return results
