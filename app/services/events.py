"""Event log: the audit trail every module writes to instead of ad-hoc logging."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import EventType, MemberEvent


def record_event(
    db: Session,
    event_type: EventType | str,
    member_id: uuid.UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> MemberEvent:
    """Append an event row (flushed, never committed - the caller owns the transaction)."""
    value = event_type.value if isinstance(event_type, EventType) else str(event_type)
    event = MemberEvent(member_id=member_id, event_type=value, metadata_json=metadata or None)
    db.add(event)
    db.flush()
    return event
