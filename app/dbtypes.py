"""Custom column types shared by the models and Alembic migrations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC timestamps that behave identically on SQLite and PostgreSQL.

    * PostgreSQL -> ``TIMESTAMP WITH TIME ZONE`` (aware values passed straight through)
    * SQLite     -> naive UTC on disk, re-attached with UTC tzinfo on the way out

    Every datetime the application sees is therefore aware and comparable, which
    removes the classic "can't compare offset-naive and offset-aware" failure.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        value = value.astimezone(UTC)
        if dialect.name == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
