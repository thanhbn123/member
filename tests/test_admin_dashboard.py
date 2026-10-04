"""Admin dashboard, quick actions and the member management form.

Covers ``GET /admin/dashboard`` (counters, the 14-day series, top channels, newest
members), ``POST /admin/members/{id}/resend-verification`` and
``GET/POST /admin/members/{id}`` (edit + audit trail), plus the security invariants
that make those routes safe: admin session, CSRF, allow-listed ``?msg=``/``?next=``,
no inline style/script (CSP nonce rules).
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import timedelta
from urllib.parse import parse_qsl, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EventType, Member, MemberAttribution, MemberEvent, utcnow
from app.services.admin import (
    FLASH_MESSAGES,
    NOTE_MAX_LENGTH,
    STATUS_LABELS,
    VALID_STATUSES,
    parse_member_edit,
    resolve_flash,
)

ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]  # set by tests/conftest.py before the app is imported

CSRF_INPUT = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
CHART_COL = re.compile(
    r'<div class="chart-col">\s*<span class="chart-value">(\d+)</span>\s*'
    r'<div class="bar bar-(\d+)"[^>]*></div>\s*<span class="chart-label">([^<]+)</span>',
    re.S,
)
RECENT_ROW = re.compile(
    r'<td><a href="/admin/members/([0-9a-fA-F-]{36})">([^<]*)</a></td>\s*'
    r'<td class="nowrap">([^<]*)</td>\s*<td><span class="pill pill-([a-z]+)">',
    re.S,
)
UTM_ROW = re.compile(r'<tr>\s*<td>(.*?)</td>\s*<td class="nowrap">(\d+)</td>', re.S)
CSV_HREF = re.compile(r'href="(/admin/members\.csv[^"]*)"')
STYLE_ATTRIBUTE = re.compile(r"<[^>]+\sstyle\s*=", re.I)


# --------------------------------------------------------------------------- helpers
def _csrf(client, path: str = "/admin/dashboard") -> str:
    """CSRF token rendered on an admin page (one token per request, shared by its forms)."""
    page = client.get(path)
    assert page.status_code == 200, f"GET {path} -> {page.status_code}: {page.text[:300]}"
    match = CSRF_INPUT.search(page.text)
    assert match, f"no csrf_token field rendered on {path}"
    return match.group(1)


def _member(db: Session, email: str) -> Member | None:
    db.expire_all()
    return db.execute(select(Member).where(Member.email == email)).scalar_one_or_none()


def _events(db: Session, member_id: uuid.UUID, event_type: str | None = None) -> list[MemberEvent]:
    db.expire_all()
    statement = select(MemberEvent).where(MemberEvent.member_id == member_id)
    if event_type is not None:
        statement = statement.where(MemberEvent.event_type == event_type)
    return list(db.execute(statement.order_by(MemberEvent.id)).scalars())


def _emails(mailbox) -> list[dict]:
    """Every console email seen so far (the capture object accumulates across reads)."""
    return mailbox.read()


def _seed(
    db: Session,
    email: str,
    *,
    status: str = "pending",
    consent: bool = False,
    created_at=None,
    notes: str | None = None,
    utm_source: str | None = None,
    utm_campaign: str | None = None,
    full_name: str | None = None,
    attribution: bool = True,
) -> Member:
    """Insert one member (and optionally an attribution row) with an explicit timestamp."""
    stamp = created_at or utcnow()
    member = Member(
        full_name=full_name or email.split("@")[0],
        email=email,
        status=status,
        consent_marketing=consent,
        notes=notes,
        created_at=stamp,
        updated_at=stamp,
    )
    if status == "verified":
        member.email_verified_at = stamp
    db.add(member)
    db.flush()
    if attribution:
        db.add(
            MemberAttribution(
                member_id=member.id,
                utm_source=utm_source,
                utm_campaign=utm_campaign,
                created_at=stamp,
            )
        )
    db.commit()
    return member


def _stat(html: str, key: str) -> str:
    match = re.search(rf'class="stat-value stat-{key}">([^<]+)<', html)
    assert match, f"stat card {key!r} is not rendered"
    return match.group(1).strip()


def _chart(html: str) -> list[tuple[int, int, str]]:
    """(count, decile, label) for every bar, oldest first."""
    return [(int(c), int(d), label) for c, d, label in CHART_COL.findall(html)]


def _table_rows(html: str, heading: str) -> list[tuple[str, int]]:
    start = html.index(heading)
    chunk = html[start : html.index("</table>", start)]
    return [(value, int(count)) for value, count in UTM_ROW.findall(chunk)]


def _recent_rows(html: str) -> list[tuple[str, str, str, str]]:
    start = html.index("10 thành viên mới nhất")
    chunk = html[start : html.index("</table>", start)]
    return [
        (member_id, name, email, status)
        for member_id, name, email, status in RECENT_ROW.findall(chunk)
    ]


# --------------------------------------------------------------------------- access
def test_dashboard_redirects_anonymous_to_login(client):
    # `client` alone: the admin_client fixture would log this same instance in first.
    anonymous = client.get("/admin/dashboard", follow_redirects=False)

    assert anonymous.status_code == 303, anonymous.text
    assert "/admin/login" in anonymous.headers["location"]


def test_dashboard_renders_for_an_admin(admin_client):
    page = admin_client.get("/admin/dashboard")

    assert page.status_code == 200, page.text
    for label in (
        "Bảng điều khiển",
        "Tổng thành viên",
        "Chờ xác minh",
        "Đã xác minh",
        "Tỷ lệ xác minh",
        "Đăng ký 7 ngày qua",
        "Đồng ý nhận tin",
        "Đăng ký theo ngày",
        "Top 5 UTM source",
        "Top 5 UTM campaign",
        "10 thành viên mới nhất",
    ):
        assert label in page.text, f"missing dashboard label: {label!r}"


def test_dashboard_exposes_the_quick_links(admin_client):
    page = admin_client.get("/admin/dashboard")

    assert 'href="/admin/members"' in page.text
    assert 'href="/admin/members.csv"' in page.text
    assert 'href="/register" target="_blank" rel="noopener"' in page.text
    assert 'href="/admin/dashboard"' in page.text  # navigation link back to itself
    assert "Đăng xuất" in page.text


def test_dashboard_stats_count_seeded_members(admin_client, db_session):
    now = utcnow()
    _seed(db_session, "pending-1@example.com", status="pending", created_at=now - timedelta(days=1))
    _seed(db_session, "pending-2@example.com", status="pending", created_at=now - timedelta(days=2))
    _seed(
        db_session,
        "verified-1@example.com",
        status="verified",
        consent=True,
        created_at=now - timedelta(days=3),
    )
    _seed(db_session, "blocked-1@example.com", status="blocked", created_at=now - timedelta(days=20))
    _seed(
        db_session,
        "unsub-1@example.com",
        status="unsubscribed",
        consent=True,
        created_at=now - timedelta(days=30),
    )

    page = admin_client.get("/admin/dashboard")

    assert page.status_code == 200, page.text
    assert _stat(page.text, "total") == "5"
    assert _stat(page.text, "pending") == "2"
    assert _stat(page.text, "verified") == "1"
    assert _stat(page.text, "rate") == "20.0%"
    assert _stat(page.text, "week") == "3", "only the three members inside the 7 day window count"
    assert _stat(page.text, "consent") == "2"
    assert _stat(page.text, "blocked") == "1"
    assert _stat(page.text, "unsubscribed") == "1"


def test_dashboard_hides_unsubscribed_and_blocked_cards_when_there_are_none(admin_client, db_session):
    _seed(db_session, "only-pending@example.com")

    page = admin_client.get("/admin/dashboard")

    assert "Đã huỷ đăng ký" not in page.text
    assert "Bị chặn" not in page.text


def test_dashboard_verification_rate_is_zero_without_members(admin_client):
    page = admin_client.get("/admin/dashboard")

    assert _stat(page.text, "total") == "0"
    assert _stat(page.text, "rate") == "0.0%"
    assert "Chưa có thành viên nào." in page.text
    assert "Chưa có dữ liệu attribution." in page.text
    assert len(_chart(page.text)) == 14, "an empty dashboard still renders 14 zero bars"


def test_dashboard_counts_a_member_registered_over_http(admin_client, web):
    before = _stat(admin_client.get("/admin/dashboard").text, "total")

    response, email = web.register(email="dashboard-new@example.com", full_name="Dashboard New")
    assert response.status_code == 303, response.text

    page = admin_client.get("/admin/dashboard")
    assert int(_stat(page.text, "total")) == int(before) + 1
    assert email in page.text
    assert "Dashboard New" in page.text


# --------------------------------------------------------------------------- chart
def test_dashboard_series_is_zero_filled_and_buckets_by_utc_day(admin_client, db_session):
    now = utcnow()
    for index in range(3):
        _seed(db_session, f"today-{index}@example.com", created_at=now - timedelta(minutes=index + 1))
    _seed(db_session, "three-days@example.com", created_at=now - timedelta(days=3))
    _seed(db_session, "thirteen-days@example.com", created_at=now - timedelta(days=13))
    _seed(db_session, "outside@example.com", created_at=now - timedelta(days=20))

    series = _chart(admin_client.get("/admin/dashboard").text)

    assert len(series) == 14
    today = now.date()
    labels = [label for _count, _decile, label in series]
    assert labels[0] == (today - timedelta(days=13)).strftime("%d/%m")
    assert labels[-1] == today.strftime("%d/%m")

    counts = [count for count, _decile, _label in series]
    assert counts[-1] == 3, "three members registered today"
    assert counts[-4] == 1, "one member three days ago"
    assert counts[0] == 1, "one member thirteen days ago"
    assert sum(counts) == 5, "the member older than the window must not be counted"
    assert counts.count(0) == 11, "every empty day is still rendered"

    deciles = [decile for _count, decile, _label in series]
    assert deciles[-1] == 100, "the busiest day fills the chart"
    assert [d for c, d in zip(counts, deciles, strict=True) if c == 0] == [0] * 11
    assert all(decile > 0 for count, decile in zip(counts, deciles, strict=True) if count)


def test_dashboard_series_bars_use_decile_classes_never_inline_styles(admin_client, db_session):
    now = utcnow()
    _seed(db_session, "peak-1@example.com", created_at=now - timedelta(minutes=1))
    _seed(db_session, "peak-2@example.com", created_at=now - timedelta(minutes=2))
    _seed(db_session, "half@example.com", created_at=now - timedelta(days=2))

    page = admin_client.get("/admin/dashboard")

    assert 'class="bar bar-100"' in page.text
    assert 'class="bar bar-50"' in page.text
    assert "style=" not in page.text


# --------------------------------------------------------------------------- top channels
def test_dashboard_top_utm_sources_are_ordered_and_capped_at_five(admin_client, db_session):
    now = utcnow()
    for index in range(3):
        _seed(db_session, f"fb-{index}@example.com", utm_source="facebook", created_at=now)
    for index in range(2):
        _seed(db_session, f"gg-{index}@example.com", utm_source="google", created_at=now)
    _seed(db_session, "tt@example.com", utm_source="tiktok", created_at=now)
    _seed(db_session, "zl@example.com", utm_source="zalo", created_at=now)
    _seed(db_session, "ml@example.com", utm_source="email", created_at=now)
    _seed(db_session, "unknown@example.com", utm_source=None, created_at=now)

    page = admin_client.get("/admin/dashboard")
    rows = _table_rows(page.text, "Top 5 UTM source")

    assert rows == [
        ("facebook", 3),
        ("google", 2),
        ("email", 1),  # ties are ordered by value, so the result is engine independent
        ("tiktok", 1),
        ("zalo", 1),
    ], rows
    source_table = page.text.split("Top 5 UTM source")[1].split("</table>")[0]
    assert "không xác định" not in source_table, "the sixth bucket must not be shown"


def test_dashboard_top_utm_source_shows_unknown_when_it_is_in_the_top_five(admin_client, db_session):
    now = utcnow()
    for index in range(2):
        _seed(db_session, f"no-utm-{index}@example.com", utm_source=None, created_at=now)
    _seed(db_session, "with-utm@example.com", utm_source="facebook", created_at=now)

    rows = _table_rows(admin_client.get("/admin/dashboard").text, "Top 5 UTM source")

    assert rows == [("không xác định", 2), ("facebook", 1)], rows


def test_dashboard_top_utm_sources_tolerates_members_without_attribution(admin_client, db_session):
    _seed(db_session, "no-attribution@example.com", attribution=False)

    page = admin_client.get("/admin/dashboard")

    assert page.status_code == 200
    assert _table_rows(page.text, "Top 5 UTM source") == []
    assert "Chưa có dữ liệu attribution." in page.text
    assert "no-attribution@example.com" in page.text


def test_dashboard_top_utm_campaign_is_a_separate_ranking(admin_client, db_session):
    now = utcnow()
    for index in range(2):
        _seed(db_session, f"camp-{index}@example.com", utm_campaign="tet-2026", created_at=now)
    _seed(db_session, "camp-other@example.com", utm_campaign="black-friday", created_at=now)
    _seed(db_session, "camp-none@example.com", utm_campaign=None, created_at=now)

    rows = _table_rows(admin_client.get("/admin/dashboard").text, "Top 5 UTM campaign")

    assert rows == [("tet-2026", 2), ("black-friday", 1), ("không xác định", 1)], rows


# --------------------------------------------------------------------------- recent members
def test_dashboard_shows_the_ten_newest_members_only(admin_client, db_session):
    now = utcnow()
    emails = []
    for index in range(12):
        created = now - timedelta(minutes=index)
        email = f"recent-{index:02d}@example.com"
        emails.append(email)
        _seed(db_session, email, created_at=created, utm_source="facebook" if index == 0 else None)

    rows = _recent_rows(admin_client.get("/admin/dashboard").text)

    assert len(rows) == 10
    shown = [email for _id, _name, email, _status in rows]
    assert shown == emails[:10], "newest first, capped at ten"
    assert emails[10] not in shown
    assert emails[11] not in shown
    assert all(re.fullmatch(r"[0-9a-fA-F-]{36}", member_id) for member_id, *_ in rows)
    assert all(status in VALID_STATUSES for *_rest, status in rows)


# --------------------------------------------------------------------------- resend action
def test_resend_verification_posts_a_new_email_and_flashes_success(
    admin_client, web, mailbox, db_session
):
    response, email = web.register(email="resend-pending@example.com")
    assert response.status_code == 303
    member = _member(db_session, email)
    assert member is not None
    sent_before = len(_emails(mailbox))

    posted = admin_client.post(
        f"/admin/members/{member.id}/resend-verification",
        data={"csrf_token": _csrf(admin_client, "/admin/members"), "next": "/admin/members"},
        follow_redirects=False,
    )

    assert posted.status_code == 303, posted.text
    assert posted.headers["location"] == "/admin/members?msg=verification_sent"

    landing = admin_client.get(posted.headers["location"])
    assert landing.status_code == 200
    assert "Đã gửi lại email xác minh." in landing.text
    assert "alert-success" in landing.text

    messages = _emails(mailbox)
    assert len(messages) == sent_before + 1, "exactly one new verification email"
    assert messages[-1]["to"] == email
    assert messages[-1]["url"], "the new email carries a fresh verification link"


def test_resend_verification_from_the_dashboard_returns_to_the_dashboard(
    admin_client, web, mailbox, db_session
):
    response, email = web.register(email="resend-dash@example.com")
    assert response.status_code == 303
    member = _member(db_session, email)
    assert member is not None
    sent_before = len(_emails(mailbox))

    posted = admin_client.post(
        f"/admin/members/{member.id}/resend-verification",
        data={"csrf_token": _csrf(admin_client), "next": "/admin/dashboard"},
        follow_redirects=False,
    )

    assert posted.status_code == 303
    assert posted.headers["location"] == "/admin/dashboard?msg=verification_sent"
    assert "alert-success" in admin_client.get(posted.headers["location"]).text
    assert len(_emails(mailbox)) == sent_before + 1


def test_resend_verification_for_a_verified_member_is_a_noop(admin_client, web, mailbox, db_session):
    response, email = web.register_and_verify(email="resend-verified@example.com")
    assert response.status_code == 200
    member = _member(db_session, email)
    assert member is not None
    assert member.status == "verified"
    sent_before = len(_emails(mailbox))

    posted = admin_client.post(
        f"/admin/members/{member.id}/resend-verification",
        data={"csrf_token": _csrf(admin_client, "/admin/members"), "next": "/admin/members"},
        follow_redirects=False,
    )

    assert posted.status_code == 303
    assert posted.headers["location"] == "/admin/members?msg=already_verified"
    assert "đã xác minh email rồi" in admin_client.get(posted.headers["location"]).text
    assert len(_emails(mailbox)) == sent_before, "a verified member must not receive another email"


def test_resend_verification_refuses_unsubscribed_and_blocked_members(
    admin_client, mailbox, db_session
):
    now = utcnow()
    members = [
        _seed(db_session, "resend-blocked@example.com", status="blocked", created_at=now),
        _seed(db_session, "resend-unsub@example.com", status="unsubscribed", created_at=now),
    ]
    sent_before = len(_emails(mailbox))

    for member in members:
        posted = admin_client.post(
            f"/admin/members/{member.id}/resend-verification",
            data={"csrf_token": _csrf(admin_client, "/admin/members"), "next": "/admin/members"},
            follow_redirects=False,
        )
        assert posted.status_code == 303
        assert posted.headers["location"] == "/admin/members?msg=not_pending"
        assert "chờ xác minh" in admin_client.get(posted.headers["location"]).text

    assert len(_emails(mailbox)) == sent_before


def test_resend_verification_for_an_unknown_member_flashes_not_found(admin_client):
    posted = admin_client.post(
        f"/admin/members/{uuid.uuid4()}/resend-verification",
        data={"csrf_token": _csrf(admin_client), "next": "/admin/dashboard"},
        follow_redirects=False,
    )

    assert posted.status_code == 303
    assert posted.headers["location"] == "/admin/dashboard?msg=member_not_found"


def test_resend_verification_ignores_an_offsite_next_target(admin_client, web, mailbox, db_session):
    response, email = web.register(email="resend-offsite@example.com")
    assert response.status_code == 303
    member = _member(db_session, email)
    assert member is not None

    for hostile in (
        "https://evil.example.com/steal",
        "//evil.example.com",
        "/admin/login",
        "/admin/members/../../etc/passwd",
        "javascript:alert(1)",
        "/admin/members?status=verified",
    ):
        posted = admin_client.post(
            f"/admin/members/{member.id}/resend-verification",
            data={"csrf_token": _csrf(admin_client, "/admin/members"), "next": hostile},
            follow_redirects=False,
        )
        assert posted.status_code == 303
        assert posted.headers["location"] == "/admin/members?msg=verification_sent"
        assert "evil.example.com" not in posted.headers["location"]
        assert "javascript:" not in posted.headers["location"]


def test_members_list_only_offers_resend_for_pending_members(admin_client, web, db_session):
    response, pending_email = web.register(email="list-pending@example.com")
    assert response.status_code == 303
    verified, verified_email = web.register_and_verify(email="list-verified@example.com")
    assert verified.status_code == 200
    pending = _member(db_session, pending_email)
    verified_member = _member(db_session, verified_email)
    assert pending is not None and verified_member is not None

    page = admin_client.get("/admin/members")

    assert f'action="/admin/members/{pending.id}/resend-verification"' in page.text
    assert 'name="next" value="/admin/members"' in page.text
    assert verified_email in page.text
    assert f'action="/admin/members/{verified_member.id}/resend-verification"' not in page.text


# --------------------------------------------------------------------------- edit form
def test_member_detail_renders_the_management_form(admin_client, db_session):
    member = _seed(db_session, "form@example.com", notes="ghi chú cũ")

    page = admin_client.get(f"/admin/members/{member.id}")

    assert page.status_code == 200, page.text
    assert "Quản lý thành viên" in page.text
    assert 'name="status"' in page.text and "<select" in page.text
    assert 'name="notes"' in page.text
    assert 'name="consent_marketing"' in page.text
    assert "Lưu thay đổi" in page.text
    for value in VALID_STATUSES:
        assert f'<option value="{value}"' in page.text, f"status {value} missing from the select"
    assert f'maxlength="{NOTE_MAX_LENGTH}"' in page.text
    assert "ghi chú cũ" in page.text
    assert STYLE_ATTRIBUTE.search(page.text) is None


def test_member_edit_updates_fields_and_writes_exactly_one_audit_event(admin_client, db_session):
    member = _seed(db_session, "edit@example.com")
    before = member.updated_at

    posted = admin_client.post(
        f"/admin/members/{member.id}",
        data={
            "csrf_token": _csrf(admin_client, f"/admin/members/{member.id}"),
            "status": "verified",
            "notes": "  Đã gọi xác nhận  ",
            "consent_marketing": "1",
        },
        follow_redirects=False,
    )

    assert posted.status_code == 303, posted.text
    assert posted.headers["location"] == f"/admin/members/{member.id}?msg=member_updated"
    landing = admin_client.get(posted.headers["location"])
    assert "Đã lưu thay đổi của thành viên." in landing.text

    refreshed = _member(db_session, "edit@example.com")
    assert refreshed is not None
    assert refreshed.status == "verified"
    assert refreshed.notes == "Đã gọi xác nhận", "notes are stored stripped"
    assert refreshed.consent_marketing is True
    assert refreshed.updated_at > before

    events = _events(db_session, member.id, EventType.MEMBER_UPDATED.value)
    assert len(events) == 1, "exactly one MEMBER_UPDATED event"
    assert events[0].metadata_json == {
        "actor": ADMIN_EMAIL,
        "changed": {
            "status": ["pending", "verified"],
            "notes": [None, "Đã gọi xác nhận"],
            "consent_marketing": [False, True],
        },
    }
    assert "MEMBER_UPDATED" in landing.text, "the audit event is visible on the detail page"


def test_member_edit_only_records_the_fields_that_changed(admin_client, db_session):
    member = _seed(db_session, "partial@example.com", consent=True, notes="giữ nguyên")

    posted = admin_client.post(
        f"/admin/members/{member.id}",
        data={
            "csrf_token": _csrf(admin_client, f"/admin/members/{member.id}"),
            "status": "blocked",
            "notes": "giữ nguyên",
            "consent_marketing": "1",
        },
        follow_redirects=False,
    )

    assert posted.status_code == 303
    events = _events(db_session, member.id, EventType.MEMBER_UPDATED.value)
    assert len(events) == 1
    assert events[0].metadata_json is not None
    assert events[0].metadata_json["changed"] == {"status": ["pending", "blocked"]}


def test_member_edit_can_clear_the_notes_and_uncheck_consent(admin_client, db_session):
    member = _seed(db_session, "clear@example.com", notes="xoá đi", consent=True)

    posted = admin_client.post(
        f"/admin/members/{member.id}",
        data={
            "csrf_token": _csrf(admin_client, f"/admin/members/{member.id}"),
            "status": "pending",
            "notes": "   ",
            # an unchecked checkbox is simply absent from the form body
        },
        follow_redirects=False,
    )

    assert posted.status_code == 303
    refreshed = _member(db_session, "clear@example.com")
    assert refreshed is not None
    assert refreshed.notes is None
    assert refreshed.consent_marketing is False
    events = _events(db_session, member.id, EventType.MEMBER_UPDATED.value)
    assert len(events) == 1
    assert events[0].metadata_json is not None
    assert events[0].metadata_json["changed"] == {
        "notes": ["xoá đi", None],
        "consent_marketing": [True, False],
    }


def test_member_edit_without_any_change_writes_nothing(admin_client, db_session):
    member = _seed(db_session, "noop@example.com", notes="như cũ", consent=True)
    before = member.updated_at

    posted = admin_client.post(
        f"/admin/members/{member.id}",
        data={
            "csrf_token": _csrf(admin_client, f"/admin/members/{member.id}"),
            "status": "pending",
            "notes": "như cũ",
            "consent_marketing": "1",
        },
        follow_redirects=False,
    )

    assert posted.status_code == 303
    assert posted.headers["location"] == f"/admin/members/{member.id}?msg=no_change"
    assert "Không có thay đổi nào để lưu." in admin_client.get(posted.headers["location"]).text
    refreshed = _member(db_session, "noop@example.com")
    assert refreshed is not None
    assert refreshed.updated_at == before
    assert _events(db_session, member.id, EventType.MEMBER_UPDATED.value) == []


def test_member_edit_rejects_an_invalid_status_without_writing(admin_client, db_session):
    member = _seed(db_session, "bad-status@example.com", notes="an toàn")
    before = member.updated_at

    posted = admin_client.post(
        f"/admin/members/{member.id}",
        data={
            "csrf_token": _csrf(admin_client, f"/admin/members/{member.id}"),
            "status": "superuser",
            "notes": "đổi trộm",
            "consent_marketing": "1",
        },
        follow_redirects=False,
    )

    assert posted.status_code == 303
    assert posted.headers["location"] == f"/admin/members/{member.id}?msg=invalid_status"
    landing = admin_client.get(posted.headers["location"])
    assert "Trạng thái không hợp lệ" in landing.text
    assert "alert-error" in landing.text

    refreshed = _member(db_session, "bad-status@example.com")
    assert refreshed is not None
    assert refreshed.status == "pending"
    assert refreshed.notes == "an toàn"
    assert refreshed.consent_marketing is False
    assert refreshed.updated_at == before
    assert _events(db_session, member.id, EventType.MEMBER_UPDATED.value) == []


def test_member_edit_rejects_notes_over_the_limit_without_writing(admin_client, db_session):
    member = _seed(db_session, "long-notes@example.com")

    posted = admin_client.post(
        f"/admin/members/{member.id}",
        data={
            "csrf_token": _csrf(admin_client, f"/admin/members/{member.id}"),
            "status": "verified",
            "notes": "x" * (NOTE_MAX_LENGTH + 1),
        },
        follow_redirects=False,
    )

    assert posted.status_code == 303
    assert posted.headers["location"] == f"/admin/members/{member.id}?msg=notes_too_long"
    assert "Ghi chú quá dài" in admin_client.get(posted.headers["location"]).text

    refreshed = _member(db_session, "long-notes@example.com")
    assert refreshed is not None
    assert refreshed.status == "pending"
    assert refreshed.notes is None
    assert _events(db_session, member.id, EventType.MEMBER_UPDATED.value) == []


def test_member_edit_accepts_notes_exactly_at_the_limit(admin_client, db_session):
    member = _seed(db_session, "limit-notes@example.com")
    note = "x" * NOTE_MAX_LENGTH

    posted = admin_client.post(
        f"/admin/members/{member.id}",
        data={
            "csrf_token": _csrf(admin_client, f"/admin/members/{member.id}"),
            "status": "pending",
            "notes": note,
        },
        follow_redirects=False,
    )

    assert posted.status_code == 303
    assert posted.headers["location"] == f"/admin/members/{member.id}?msg=member_updated"
    refreshed = _member(db_session, "limit-notes@example.com")
    assert refreshed is not None
    assert refreshed.notes == note


def test_member_edit_unknown_member_is_404(admin_client):
    response = admin_client.post(
        f"/admin/members/{uuid.uuid4()}",
        data={"csrf_token": _csrf(admin_client), "status": "verified"},
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- security invariants
def test_resend_and_edit_require_an_admin_session(client):
    member_id = uuid.uuid4()
    for path in (f"/admin/members/{member_id}", f"/admin/members/{member_id}/resend-verification"):
        response = client.post(
            path, data={"status": "verified", "next": "/admin/dashboard"}, follow_redirects=False
        )
        assert response.status_code == 303, response.text
        assert "/admin/login" in response.headers["location"]


def test_resend_and_edit_are_403_without_a_valid_csrf_token(client, admin_client, db_session):
    member = _seed(db_session, "csrf@example.com")
    valid = _csrf(admin_client, "/admin/members")

    for path, payload in (
        (f"/admin/members/{member.id}", {"status": "verified"}),
        (f"/admin/members/{member.id}/resend-verification", {"next": "/admin/members"}),
    ):
        for token in (None, f"{valid}tampered"):
            data = dict(payload)
            if token is not None:
                data["csrf_token"] = token
            response = client.post(path, data=data, follow_redirects=False)
            assert response.status_code == 403, f"{path} accepted csrf={token!r}: {response.text[:200]}"

    refreshed = _member(db_session, "csrf@example.com")
    assert refreshed is not None
    assert refreshed.status == "pending", "a CSRF failure must not write"
    assert _events(db_session, member.id, EventType.MEMBER_UPDATED.value) == []


def test_hostile_msg_values_render_no_alert(admin_client, db_session):
    _seed(db_session, "injection@example.com")
    payloads = (
        "<script>alert(1)</script>",
        '"><script>alert(2)</script>',
        "verification_sent<script>alert(3)</script>",
        "x" * 200,
        "'; DROP TABLE members; --",
    )

    for path in ("/admin/dashboard", "/admin/members"):
        for payload in payloads:
            response = admin_client.get(path, params={"msg": payload})
            assert response.status_code == 200, response.text
            assert "alert(" not in response.text, f"{path}?msg={payload!r} reflected the payload"
            assert "DROP TABLE" not in response.text
            assert payload not in response.text
            for marker in ("alert-success", "alert-error", "alert-info"):
                assert marker not in response.text, f"{path}?msg={payload!r} rendered {marker}"


def test_known_msg_codes_render_their_allow_listed_message(admin_client):
    page = admin_client.get("/admin/dashboard", params={"msg": "verification_sent"})

    assert "Đã gửi lại email xác minh." in page.text
    banner = page.text.split("alert-success")[1].split("</div>")[0]
    assert "verification_sent" not in banner, "the code itself is never rendered"


def test_admin_pages_keep_the_csp_rules_and_ship_no_inline_script(admin_client, db_session):
    member = _seed(db_session, "csp@example.com")

    for path in ("/admin/dashboard", "/admin/members", f"/admin/members/{member.id}"):
        response = admin_client.get(path)
        assert response.status_code == 200, response.text
        assert STYLE_ATTRIBUTE.search(response.text) is None, f"{path} uses a style attribute"
        assert "<script" not in response.text, f"{path} renders an inline script"
        policy = response.headers["content-security-policy"]
        assert "'unsafe-inline'" not in policy
        assert "nonce-" in policy
        assert response.headers["x-frame-options"] == "DENY"


def test_member_detail_navigation_links_the_dashboard_and_the_list(admin_client, db_session):
    member = _seed(db_session, "nav@example.com")

    page = admin_client.get(f"/admin/members/{member.id}")

    assert 'href="/admin/dashboard"' in page.text
    assert 'href="/admin/members"' in page.text
    assert 'action="/admin/logout"' in page.text
    assert 'aria-current="page"' in page.text


# --------------------------------------------------------------------------- service helpers
def test_status_labels_cover_every_member_status():
    assert set(STATUS_LABELS) == VALID_STATUSES
    assert set(VALID_STATUSES) == {"pending", "verified", "unsubscribed", "blocked"}


def test_resolve_flash_only_accepts_allow_listed_codes():
    assert resolve_flash(None) is None
    assert resolve_flash("") is None
    assert resolve_flash("<script>alert(1)</script>") is None
    assert resolve_flash("x" * 100) is None
    assert resolve_flash("member_updated").text == FLASH_MESSAGES["member_updated"][1]
    for code, (level, text) in FLASH_MESSAGES.items():
        flash = resolve_flash(code)
        assert flash is not None and flash.level == level and flash.text == text
        assert flash.css == f"alert-{level}"


def test_parse_member_edit_matrix():
    edit, error = parse_member_edit(
        {"status": " VERIFIED ", "notes": " ok ", "consent_marketing": "on"}
    )
    assert error is None and edit is not None
    assert (edit.status, edit.notes, edit.consent_marketing) == ("verified", "ok", True)

    assert parse_member_edit({"status": "nope"})[1] == "invalid_status"
    assert parse_member_edit({})[1] == "invalid_status"
    assert parse_member_edit({"status": "pending", "notes": "x" * 2001})[1] == "notes_too_long"

    empty_notes, _ = parse_member_edit({"status": "pending", "notes": ""})
    assert empty_notes is not None and empty_notes.notes is None
    missing_consent, _ = parse_member_edit({"status": "pending"})
    assert missing_consent is not None and missing_consent.consent_marketing is False
    off_consent, _ = parse_member_edit({"status": "pending", "consent_marketing": "0"})
    assert off_consent is not None and off_consent.consent_marketing is False
    on_consent, _ = parse_member_edit({"status": "pending", "consent_marketing": "true"})
    assert on_consent is not None and on_consent.consent_marketing is True


def test_csv_export_still_returns_the_edited_member(admin_client, db_session):
    member = _seed(db_session, "csv-after-edit@example.com", notes="trước")

    admin_client.post(
        f"/admin/members/{member.id}",
        data={
            "csrf_token": _csrf(admin_client, f"/admin/members/{member.id}"),
            "status": "verified",
            "notes": "sau",
        },
        follow_redirects=False,
    )

    export = admin_client.get("/admin/members.csv")

    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert "csv-after-edit@example.com" in export.text
    assert "verified" in export.text


def test_members_list_csv_link_keeps_the_active_filters(admin_client, db_session):
    _seed(db_session, "kept@example.com", utm_source="facebook")
    _seed(db_session, "other@example.com", utm_source="google")

    page = admin_client.get("/admin/members", params={"utm_source": "facebook", "status": "pending"})

    assert page.status_code == 200
    match = CSV_HREF.search(page.text)
    assert match, "no CSV link on the member list"
    href = match.group(1).replace("&amp;", "&")  # Jinja escapes the query separator
    params = dict(parse_qsl(urlparse(href).query))
    assert params.get("utm_source") == "facebook"
    assert params.get("status") == "pending"
