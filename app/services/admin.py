"""Admin queries (filter / search / paginate), dashboard aggregates, member edits, CSV export.

CSV export is hardened against formula injection (OWASP): any cell starting with
``=``, ``+``, ``-``, ``@``, TAB or CR is prefixed with an apostrophe so spreadsheet
apps treat it as text instead of executing it.

Every dashboard number is computed here (never in the template) so the same query runs
against SQLite in tests and PostgreSQL in production, and so the labels the admin reads
come from one allow-listed place.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import EventType, Member, MemberAttribution, MemberStatus, utcnow
from app.services.events import record_event

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
PER_PAGE_CHOICES = (10, 25, 50, 100)
DEFAULT_PER_PAGE = 25
# Pagination is a UI concern: nothing useful lives past this page, and an unbounded
# offset overflows SQLite (OverflowError) / PostgreSQL (int8) and kills the request.
MAX_PAGE = 10_000


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
    page = min(page, MAX_PAGE)  # clamp before the offset reaches the database
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


# --------------------------------------------------------------------------- dashboard
DASHBOARD_WINDOW_DAYS = 7
CHART_DAYS = 14
TOP_ATTRIBUTION_LIMIT = 5
RECENT_MEMBERS_LIMIT = 10
NOTE_MAX_LENGTH = 2000
# ``?msg=`` and ``?next=`` are never echoed: the first is resolved through this
# allow-list, the second is compared against a fixed set of paths in the router.
FLASH_CODE_MAX_LENGTH = 64

VALID_STATUSES = frozenset(item.value for item in MemberStatus)
STATUS_LABELS: dict[str, str] = {
    MemberStatus.PENDING.value: "Chờ xác minh",
    MemberStatus.VERIFIED.value: "Đã xác minh",
    MemberStatus.UNSUBSCRIBED.value: "Đã huỷ đăng ký",
    MemberStatus.BLOCKED.value: "Bị chặn",
}
ATTRIBUTION_COUNT_FIELDS = ("utm_source", "utm_campaign")
CONSENT_TRUE_VALUES = frozenset({"1", "true", "on", "yes"})


@dataclass(slots=True, frozen=True)
class DashboardStats:
    """Headline counters for ``/admin/dashboard`` (computed with a single query)."""

    total: int = 0
    pending: int = 0
    verified: int = 0
    unsubscribed: int = 0
    blocked: int = 0
    last_days: int = 0
    consent_marketing: int = 0

    @property
    def verification_rate(self) -> float:
        """Verified share in percent (one decimal); 0.0 when there is nothing to divide."""
        if self.total <= 0:
            return 0.0
        return round(self.verified * 100 / self.total, 1)


@dataclass(slots=True, frozen=True)
class DayCount:
    """One bar of the registrations-per-day chart."""

    day: date
    count: int
    decile: int = 0  # 0, 10, …, 100 -> CSS class ``.bar-<decile>``

    @property
    def bar_class(self) -> str:
        return f"bar-{self.decile}"

    @property
    def label(self) -> str:
        return self.day.strftime("%d/%m")


@dataclass(slots=True, frozen=True)
class CountRow:
    """One row of a "top N" table. ``value is None`` means "not captured"."""

    value: str | None
    count: int
    percent: float


@dataclass(slots=True, frozen=True)
class Flash:
    """One allow-listed banner. ``level`` drives the alert style, never the query string."""

    code: str
    level: str  # "success" | "info" | "error"
    text: str

    @property
    def css(self) -> str:
        return f"alert-{self.level}"


# Code -> (level, Vietnamese message). Nothing outside this table can ever be rendered.
FLASH_MESSAGES: dict[str, tuple[str, str]] = {
    "verification_sent": ("success", "Đã gửi lại email xác minh."),
    "already_verified": ("info", "Thành viên này đã xác minh email rồi, không cần gửi lại."),
    "not_pending": (
        "info",
        "Chỉ gửi lại được email xác minh cho thành viên đang ở trạng thái chờ xác minh.",
    ),
    "member_updated": ("success", "Đã lưu thay đổi của thành viên."),
    "no_change": ("info", "Không có thay đổi nào để lưu."),
    "invalid_status": ("error", "Trạng thái không hợp lệ. Chưa lưu thay đổi nào."),
    "notes_too_long": (
        "error",
        f"Ghi chú quá dài (tối đa {NOTE_MAX_LENGTH} ký tự). Chưa lưu thay đổi nào.",
    ),
    "error": ("error", "Không gửi được email xác minh. Vui lòng kiểm tra cấu hình email và thử lại."),
    "member_not_found": ("error", "Không tìm thấy thành viên này."),
}


def resolve_flash(code: str | None) -> Flash | None:
    """Map an allow-listed ``?msg=`` code to its message.

    A hostile or unknown code (``?msg=<script>alert(1)</script>``) resolves to ``None``
    and the template renders no alert at all - raw query text never reaches the HTML.
    """
    if code is None:
        return None
    candidate = str(code)
    if len(candidate) > FLASH_CODE_MAX_LENGTH:
        return None
    entry = FLASH_MESSAGES.get(candidate)
    if entry is None:
        return None
    level, text = entry
    return Flash(code=candidate, level=level, text=text)


def dashboard_stats(
    db: Session, *, days: int = DASHBOARD_WINDOW_DAYS, now: datetime | None = None
) -> DashboardStats:
    """Count members by status plus the rolling registration/consent windows."""
    reference = now or utcnow()
    since = reference - timedelta(days=days)

    def flag(condition: Any) -> Any:
        # coalesce keeps the result an int (0) instead of NULL when the table is empty.
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    row = db.execute(
        select(
            func.count(Member.id),
            flag(Member.status == MemberStatus.PENDING.value),
            flag(Member.status == MemberStatus.VERIFIED.value),
            flag(Member.status == MemberStatus.UNSUBSCRIBED.value),
            flag(Member.status == MemberStatus.BLOCKED.value),
            flag(Member.created_at >= since),
            flag(Member.consent_marketing.is_(True)),
        )
    ).one()
    values = [int(value or 0) for value in row]
    return DashboardStats(
        total=values[0],
        pending=values[1],
        verified=values[2],
        unsubscribed=values[3],
        blocked=values[4],
        last_days=values[5],
        consent_marketing=values[6],
    )


def daily_registrations(
    db: Session, *, days: int = CHART_DAYS, now: datetime | None = None
) -> list[DayCount]:
    """Registrations per UTC day, oldest first, zero-filled to exactly ``days`` bars.

    The day bucket is computed in Python from one indexed read of ``created_at``:
    SQLite and PostgreSQL disagree about date truncation (``strftime`` vs ``date_trunc``),
    and a dialect branch here would be one more thing that can differ silently between
    the test engine and the production engine.
    """
    window = max(1, days)
    reference = now or utcnow()
    today = _utc_date(reference)
    first_day = today - timedelta(days=window - 1)
    start = datetime.combine(first_day, time.min, tzinfo=UTC)

    counts: dict[date, int] = defaultdict(int)
    for stamp in db.execute(select(Member.created_at).where(Member.created_at >= start)).scalars():
        if stamp is None:  # pragma: no cover - created_at is NOT NULL
            continue
        day = _utc_date(stamp)
        if day >= first_day:
            counts[day] += 1

    peak = max(counts.values(), default=0)
    series: list[DayCount] = []
    for offset in range(window):
        day = first_day + timedelta(days=offset)
        count = counts.get(day, 0)
        series.append(DayCount(day=day, count=count, decile=_decile(count, peak)))
    return series


def _utc_date(value: datetime) -> date:
    """Calendar day in UTC (the timezone every stored timestamp is normalised to)."""
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(UTC).date()


def _decile(count: int, peak: int) -> int:
    """Snap a bar height to a decile so CSS can express it as a class.

    ``style="height: 42%"`` would need ``style-src 'unsafe-inline'`` and a per-request
    nonce does **not** cover style *attributes*, so the height travels as a class name
    (``.bar-10`` … ``.bar-100``) and stays fully inside the CSP. A non-zero day always
    gets at least one decile, a zero day gets ``.bar-0`` (the empty track).
    """
    if count <= 0 or peak <= 0:
        return 0
    return min(10, max(1, round(count * 10 / peak))) * 10


def top_attribution(db: Session, field: str, *, limit: int = TOP_ATTRIBUTION_LIMIT) -> list[CountRow]:
    """Most frequent values of ``field`` on the attribution table.

    A missing value (``NULL``) is a real bucket and is rendered as "không xác định".
    The ranking is finished in Python - unknown values last, then alphabetical - because
    SQLite and PostgreSQL order ``NULL`` differently and the top 5 must not depend on
    the engine. No SQL ``LIMIT`` is applied before the ranking: a tie outside the window
    would otherwise silently change which rows are "top".
    """
    if field not in ATTRIBUTION_COUNT_FIELDS:
        raise ValueError(f"unsupported attribution field: {field!r}")
    column = getattr(MemberAttribution, field)
    count_column = func.count(MemberAttribution.id)
    rows = db.execute(
        select(column, count_column).group_by(column).order_by(count_column.desc(), column.asc())
    ).all()
    total = sum(int(row[1]) for row in rows)
    ranked = sorted(rows, key=lambda row: (-int(row[1]), row[0] is None, str(row[0] or "")))
    return [
        CountRow(
            value=row[0],
            count=int(row[1]),
            percent=round(int(row[1]) * 100 / total, 1) if total else 0.0,
        )
        for row in ranked[:limit]
    ]


# --------------------------------------------------------------------------- member edits
@dataclass(slots=True, frozen=True)
class MemberEdit:
    """A validated management form submission."""

    status: str
    notes: str | None
    consent_marketing: bool


def parse_member_edit(form: Mapping[str, Any]) -> tuple[MemberEdit | None, str | None]:
    """Validate the admin edit form: ``(edit, None)`` or ``(None, flash_code)``.

    An absent field counts as an empty field: browsers do not submit an unchecked
    checkbox at all and the textarea is always part of the form. Nothing is written
    by this function - an invalid submission never touches the member row.
    """
    status = str(form.get("status") or "").strip().lower()
    if status not in VALID_STATUSES:
        return None, "invalid_status"
    notes = str(form.get("notes") or "").strip()
    if len(notes) > NOTE_MAX_LENGTH:
        return None, "notes_too_long"
    consent = str(form.get("consent_marketing") or "").strip().lower() in CONSENT_TRUE_VALUES
    return MemberEdit(status=status, notes=notes or None, consent_marketing=consent), None


def update_member(
    db: Session, member: Member, edit: MemberEdit, *, actor: str = ""
) -> dict[str, list[Any]]:
    """Apply a validated edit, audit it and commit. Returns the changed-field map.

    Only fields that actually change are written, and a submission that changes nothing
    writes no row and records no event (``{}``): an audit trail full of empty changes is
    worse than no entry.
    """
    changed: dict[str, list[Any]] = {}
    if member.status != edit.status:
        changed["status"] = [member.status, edit.status]
    if (member.notes or None) != edit.notes:
        changed["notes"] = [member.notes, edit.notes]
    if bool(member.consent_marketing) != edit.consent_marketing:
        changed["consent_marketing"] = [bool(member.consent_marketing), edit.consent_marketing]
    if not changed:
        return {}
    member.status = edit.status
    member.notes = edit.notes
    member.consent_marketing = edit.consent_marketing
    member.updated_at = utcnow()  # explicit: the audit row and the stamp must agree
    record_event(db, EventType.MEMBER_UPDATED, member.id, {"actor": actor, "changed": changed})
    db.commit()
    return changed


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
