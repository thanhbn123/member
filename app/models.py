"""ORM models for the member service.

Portable between SQLite (local dev/test) and PostgreSQL (production) - no
dialect specific types, no server side defaults.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.dbtypes import UTCDateTime


def utcnow() -> datetime:
    """Timezone aware UTC now (stored as UTC everywhere)."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class MemberStatus(enum.StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    UNSUBSCRIBED = "unsubscribed"
    BLOCKED = "blocked"


class EventType(enum.StrEnum):
    REGISTER_STARTED = "REGISTER_STARTED"
    REGISTER_COMPLETED = "REGISTER_COMPLETED"
    EMAIL_SENT = "EMAIL_SENT"
    EMAIL_FAILED = "EMAIL_FAILED"
    EMAIL_VERIFIED = "EMAIL_VERIFIED"
    LOGIN = "LOGIN"
    EXPORT = "EXPORT"
    WEBHOOK_SENT = "WEBHOOK_SENT"
    WEBHOOK_FAILED = "WEBHOOK_FAILED"
    META_EVENT_SENT = "META_EVENT_SENT"
    META_EVENT_FAILED = "META_EVENT_FAILED"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class Member(Base, TimestampMixin):
    __tablename__ = "members"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MemberStatus.PENDING.value, index=True
    )
    consent_marketing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="web_form", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    attribution: Mapped[MemberAttribution | None] = relationship(
        back_populates="member", uselist=False, cascade="all, delete-orphan"
    )
    events: Mapped[list[MemberEvent]] = relationship(
        back_populates="member", cascade="all, delete-orphan"
    )
    tokens: Mapped[list[EmailVerificationToken]] = relationship(
        back_populates="member", cascade="all, delete-orphan"
    )

    @property
    def is_verified(self) -> bool:
        return self.status == MemberStatus.VERIFIED.value and self.email_verified_at is not None

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Member {self.email} status={self.status}>"


class MemberAttribution(Base):
    __tablename__ = "member_attribution"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("members.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    utm_source: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    utm_medium: Mapped[str | None] = mapped_column(String(255), nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    utm_content: Mapped[str | None] = mapped_column(String(255), nullable=True)
    utm_term: Mapped[str | None] = mapped_column(String(255), nullable=True)
    landing_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    referrer: Mapped[str | None] = mapped_column(Text, nullable=True)
    fbp: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fbc: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )

    member: Mapped[Member] = relationship(back_populates="attribution")

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<MemberAttribution member_id={self.member_id} source={self.utm_source}>"


class MemberEvent(Base):
    __tablename__ = "member_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("members.id", ondelete="CASCADE"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )

    member: Mapped[Member | None] = relationship(back_populates="events")

    __table_args__ = (Index("ix_member_events_member_created", "member_id", "created_at"),)


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    member: Mapped[Member] = relationship(back_populates="tokens")

    __table_args__ = (UniqueConstraint("token_hash", name="uq_email_verification_tokens_token_hash"),)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<EmailVerificationToken member_id={self.member_id} used={self.used_at is not None}>"
