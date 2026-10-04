"""Admin queries (filter / search / paginate) and CSV export.

CSV export is hardened against formula injection (OWASP): any cell starting with
``=``, ``+``, ``-``, ``@``, TAB or CR is prefixed with an apostrophe so spreadsheet
apps treat it as text instead of executing it.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import Member, MemberAttribution, MemberStatus

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
PER_PAGE_CHOICES = (10, 25, 50, 100)
DEFAULT_PER_PAGE = 25


@dataclass(slots=True)
class MemberFilters:
    q: str | None = None
    status: str | None = None
    utm_source: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE

    def as_query(self) -> dict[str, str]:
        """Filter state serialised back into a query string (pagination, CSV link)."""
        params = {
            "q": self.q,
            "status": self.status,
            "utm_source": self.utm_source,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "per_page": str(self.per_page),
        }
        return {key: value for key, value in params.items() if value}


def safe_cell(value: Any) -> str:
    """Neutralise CSV formula injection and normalise None/whitespace."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    text = str(value)
    if text and text.lstrip().startswith(FORMULA_PREFIXES) and not _is_number(text):
        return "'" + text
    return text


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def parse_filters(params: dict[str, Any]) -> MemberFilters:
    def clean(key: str, max_length: int = 200) -> str | None:
        raw = params.get(key)
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        return text[:max_length]

    try:
        page = max(1, int(str(params.get("page") or 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(str(params.get("per_page") or DEFAULT_PER_PAGE))
    except (TypeError, ValueError):
        per_page = DEFAULT_PER_PAGE
    if per_page not in PER_PAGE_CHOICES:
        per_page = DEFAULT_PER_PAGE

    status_value = clean("status", 20)
    valid_statuses = {item.value for item in MemberStatus}
    if status_value not in valid_statuses:
        status_value = None

    return MemberFilters(
        q=clean("q", 100),
        status=status_value,
        utm_source=clean("utm_source", 255),
        date_from=_clean_date(clean("date_from", 10)),
        date_to=_clean_date(clean("date_to", 10)),
        page=page,
        per_page=per_page,
    )


def _clean_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def _apply_filters(stmt: Select, filters: MemberFilters) -> Select:
    if filters.q:
        like = f"%{filters.q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Member.email).like(like),
                func.lower(Member.full_name).like(like),
                func.lower(func.coalesce(Member.phone, "")).like(like),
                func.lower(func.coalesce(Member.company, "")).like(like),
            )
        )
    if filters.status:
        stmt = stmt.where(Member.status == filters.status)
    if filters.utm_source:
        stmt = stmt.join(MemberAttribution, MemberAttribution.member_id == Member.id).where(
            MemberAttribution.utm_source == filters.utm_source
        )
    if filters.date_from:
        stmt = stmt.where(Member.created_at >= _start_of_day(filters.date_from))
    if filters.date_to:
        stmt = stmt.where(Member.created_at <= _end_of_day(filters.date_to))
    return stmt


def _start_of_day(value: str) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    return parsed


def _end_of_day(value: str) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)


def query_members(db: Session, filters: MemberFilters) -> tuple[list[Member], int]:
    base = _apply_filters(select(Member), filters)
    total = db.execute(
        _apply_filters(select(func.count(Member.id)), filters).order_by(None)
    ).scalar_one()
    rows = (
        db.execute(
            base.options(selectinload(Member.attribution))
            .order_by(Member.created_at.desc(), Member.id.desc())
            .limit(filters.per_page)
            .offset((filters.page - 1) * filters.per_page)
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


def list_utm_sources(db: Session, limit: int = 100) -> list[str]:
    rows = db.execute(
        select(MemberAttribution.utm_source)
        .where(MemberAttribution.utm_source.is_not(None))
        .group_by(MemberAttribution.utm_source)
        .order_by(func.count(MemberAttribution.id).desc())
        .limit(limit)
    ).scalars()
    return [row for row in rows if row]


CSV_HEADERS = [
    "id",
    "full_name",
    "email",
    "phone",
    "company",
    "status",
    "consent_marketing",
    "email_verified_at",
    "created_at",
    "source",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "landing_url",
    "referrer",
    "fbp",
    "fbc",
]


def _row(member: Member) -> list[str]:
    attribution = member.attribution
    return [
        safe_cell(str(member.id)),
        safe_cell(member.full_name),
        safe_cell(member.email),
        safe_cell(member.phone),
        safe_cell(member.company),
        safe_cell(member.status),
        safe_cell(member.consent_marketing),
        safe_cell(member.email_verified_at),
        safe_cell(member.created_at),
        safe_cell(member.source),
        safe_cell(attribution.utm_source if attribution else None),
        safe_cell(attribution.utm_medium if attribution else None),
        safe_cell(attribution.utm_campaign if attribution else None),
        safe_cell(attribution.utm_content if attribution else None),
        safe_cell(attribution.utm_term if attribution else None),
        safe_cell(_truncate(attribution.landing_url if attribution else None, 500)),
        safe_cell(_truncate(attribution.referrer if attribution else None, 500)),
        safe_cell(attribution.fbp if attribution else None),
        safe_cell(attribution.fbc if attribution else None),
    ]


def _truncate(value: str | None, limit: int) -> str | None:
    if not value:
        return value
    return value if len(value) <= limit else value[: limit - 1] + "…"


def iter_csv(members: Iterable[Member]) -> Iterator[str]:
    """Yield the CSV document (with UTF-8 BOM so Excel opens Vietnamese text correctly)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(CSV_HEADERS)
    yield "\ufeff" + buffer.getvalue()
    for member in members:
        buffer.seek(0)
        buffer.truncate(0)
        writer.writerow(_row(member))
        yield buffer.getvalue()


def export_filename(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"members-{stamp}.csv"
