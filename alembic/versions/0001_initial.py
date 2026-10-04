"""initial schema: members, attribution, events, verification tokens

Revision ID: 0001_initial
Revises: None
Create Date: 2025-10-04

Hand-checked DDL that matches ``app/models.py`` column for column, index for
index. Portable between SQLite (dev/test) and PostgreSQL (staging/prod):

* ``sa.Uuid()``      -> CHAR(32) on SQLite, UUID on PostgreSQL
* ``sa.JSON()``      -> JSON on both
* ``sa.DateTime(timezone=True)`` matches ``app.dbtypes.UTCDateTime``, whose
  dialect implementation is ``DateTime(timezone=True)`` on both backends.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the four core tables and every index/constraint."""
    op.create_table(
        "members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("consent_marketing", sa.Boolean(), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_members_created_at", "members", ["created_at"], unique=False)
    op.create_index("ix_members_email", "members", ["email"], unique=True)
    op.create_index("ix_members_phone", "members", ["phone"], unique=False)
    op.create_index("ix_members_source", "members", ["source"], unique=False)
    op.create_index("ix_members_status", "members", ["status"], unique=False)

    op.create_table(
        "member_attribution",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("utm_source", sa.String(length=255), nullable=True),
        sa.Column("utm_medium", sa.String(length=255), nullable=True),
        sa.Column("utm_campaign", sa.String(length=255), nullable=True),
        sa.Column("utm_content", sa.String(length=255), nullable=True),
        sa.Column("utm_term", sa.String(length=255), nullable=True),
        sa.Column("landing_url", sa.Text(), nullable=True),
        sa.Column("referrer", sa.Text(), nullable=True),
        sa.Column("fbp", sa.String(length=255), nullable=True),
        sa.Column("fbc", sa.String(length=255), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("member_id"),
    )
    op.create_index(
        "ix_member_attribution_created_at", "member_attribution", ["created_at"], unique=False
    )
    op.create_index("ix_member_attribution_ip_hash", "member_attribution", ["ip_hash"], unique=False)
    op.create_index(
        "ix_member_attribution_utm_campaign", "member_attribution", ["utm_campaign"], unique=False
    )
    op.create_index(
        "ix_member_attribution_utm_source", "member_attribution", ["utm_source"], unique=False
    )

    op.create_table(
        "member_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_member_events_created_at", "member_events", ["created_at"], unique=False)
    op.create_index("ix_member_events_event_type", "member_events", ["event_type"], unique=False)
    op.create_index(
        "ix_member_events_member_created", "member_events", ["member_id", "created_at"], unique=False
    )
    op.create_index("ix_member_events_member_id", "member_events", ["member_id"], unique=False)

    op.create_table(
        "email_verification_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_email_verification_tokens_token_hash"),
    )
    op.create_index(
        "ix_email_verification_tokens_member_id",
        "email_verification_tokens",
        ["member_id"],
        unique=False,
    )
    op.create_index(
        "ix_email_verification_tokens_token_hash",
        "email_verification_tokens",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    """Drop everything created by :func:`upgrade` (reverse dependency order)."""
    op.drop_index("ix_email_verification_tokens_token_hash", table_name="email_verification_tokens")
    op.drop_index("ix_email_verification_tokens_member_id", table_name="email_verification_tokens")
    op.drop_table("email_verification_tokens")

    op.drop_index("ix_member_events_member_id", table_name="member_events")
    op.drop_index("ix_member_events_member_created", table_name="member_events")
    op.drop_index("ix_member_events_event_type", table_name="member_events")
    op.drop_index("ix_member_events_created_at", table_name="member_events")
    op.drop_table("member_events")

    op.drop_index("ix_member_attribution_utm_source", table_name="member_attribution")
    op.drop_index("ix_member_attribution_utm_campaign", table_name="member_attribution")
    op.drop_index("ix_member_attribution_ip_hash", table_name="member_attribution")
    op.drop_index("ix_member_attribution_created_at", table_name="member_attribution")
    op.drop_table("member_attribution")

    op.drop_index("ix_members_status", table_name="members")
    op.drop_index("ix_members_source", table_name="members")
    op.drop_index("ix_members_phone", table_name="members")
    op.drop_index("ix_members_email", table_name="members")
    op.drop_index("ix_members_created_at", table_name="members")
    op.drop_table("members")
