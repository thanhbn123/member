"""Admin UI: auth guard, login/logout, filters, pagination, detail page and CSRF."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Member, utcnow
from app.services.admin import DEFAULT_PER_PAGE
from tests.conftest import ADMIN_PASSWORD

CSRF_INPUT = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


# --------------------------------------------------------------------------- helpers
def _member(db: Session, email: str) -> Member | None:
    db.expire_all()
    return db.execute(select(Member).where(Member.email == email)).scalar_one_or_none()


def _count_members(db: Session) -> int:
    db.expire_all()
    return int(db.execute(select(func.count(Member.id))).scalar_one())


def _csrf_from(html: str) -> str:
    match = CSRF_INPUT.search(html)
    assert match, "no csrf_token field in the rendered page"
    return match.group(1)


def _seed_members(db: Session, count: int) -> list[str]:
    """Insert members with strictly increasing ``created_at`` so paging is deterministic."""
    base = utcnow() - timedelta(days=1)
    emails: list[str] = []
    for index in range(count):
        email = f"page-user-{index:02d}@example.com"
        stamp = base + timedelta(minutes=index)
        db.add(
            Member(
                full_name=f"Page User {index:02d}",
                email=email,
                status="pending",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        emails.append(email)
    db.commit()
    return emails


# --------------------------------------------------------------------------- auth guard
@pytest.mark.parametrize("path", ["/admin/members", "/admin/members.csv", f"/admin/members/{uuid.uuid4()}"])
def test_admin_routes_redirect_to_login_when_logged_out(client, path):
    response = client.get(path, follow_redirects=False)

    assert response.status_code in (303, 307), response.text
    assert "/admin/login" in response.headers["location"]
    if path.endswith(".csv"):
        assert "text/csv" not in response.headers.get("content-type", "")


def test_admin_login_with_wrong_password_is_401_and_grants_nothing(client, web, app_settings):
    token = web.csrf_token("/admin/login")

    response = client.post(
        "/admin/login",
        data={"email": app_settings.admin_email, "password": "definitely-not-the-password",
              "csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 401, response.text
    assert "alert-error" in response.text
    blocked = client.get("/admin/members", follow_redirects=False)
    assert blocked.status_code in (303, 307)
    assert "/admin/login" in blocked.headers["location"]


def test_admin_login_with_unknown_email_is_401(client, web):
    token = web.csrf_token("/admin/login")

    response = client.post(
        "/admin/login",
        data={"email": "nobody@example.com", "password": "whatever", "csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 401


def test_admin_login_success_lists_registered_member(admin_client, web):
    registered, email = web.register(email="admin-list@example.com")
    assert registered.status_code == 303

    page = admin_client.get("/admin/members")

    assert page.status_code == 200, page.text
    assert email in page.text
    assert "Nguyễn Văn A" in page.text


# --------------------------------------------------------------------------- filters
def test_admin_search_filter_matches_email_and_name(admin_client, web):
    web.register(email="alice-search@example.com", full_name="Alice Searchable")
    web.register(email="bob-other@example.com", full_name="Bob Other")

    by_email = admin_client.get("/admin/members", params={"q": "alice-search"})
    assert by_email.status_code == 200
    assert "alice-search@example.com" in by_email.text
    assert "bob-other@example.com" not in by_email.text

    by_name = admin_client.get("/admin/members", params={"q": "BOB OTHER"})
    assert by_name.status_code == 200
    assert "bob-other@example.com" in by_name.text
    assert "alice-search@example.com" not in by_name.text


def test_admin_status_filter_splits_pending_and_verified(admin_client, web):
    web.register(email="pending-filter@example.com")
    verified, verified_email = web.register_and_verify(email="verified-filter@example.com")
    assert verified.status_code == 200

    pending = admin_client.get("/admin/members", params={"status": "pending"})
    assert pending.status_code == 200
    assert "pending-filter@example.com" in pending.text
    assert verified_email not in pending.text

    done = admin_client.get("/admin/members", params={"status": "verified"})
    assert done.status_code == 200
    assert verified_email in done.text
    assert "pending-filter@example.com" not in done.text


def test_admin_pagination_returns_the_right_slice_and_total(admin_client, db_session):
    _seed_members(db_session, 12)

    page_one = admin_client.get("/admin/members", params={"per_page": 10, "page": 1})
    assert page_one.status_code == 200
    assert "Tổng cộng 12 thành viên" in page_one.text
    assert "page-user-11@example.com" in page_one.text  # newest first
    assert "page-user-00@example.com" not in page_one.text

    page_two = admin_client.get("/admin/members", params={"per_page": 10, "page": 2})
    assert page_two.status_code == 200
    assert "Tổng cộng 12 thành viên" in page_two.text
    assert "page-user-01@example.com" in page_two.text
    assert "page-user-00@example.com" in page_two.text
    assert "page-user-11@example.com" not in page_two.text  # the slice does not repeat
    assert "Trang 2 / 2" in page_two.text


@pytest.mark.parametrize(
    "params",
    [
        {"status": "../../etc"},
        {"status": "'; DROP TABLE members; --"},
        {"per_page": "9999"},
        {"per_page": "-5"},
        {"per_page": "abc"},
        {"page": "0"},
        {"page": "-3"},
        {"page": "not-a-number"},
        {"date_from": "not-a-date"},
        {"date_to": "2024-13-45"},
        {"q": "x" * 500},
        {"utm_source": "a" * 400},
    ],
)
def test_admin_invalid_filters_do_not_crash(admin_client, web, db_session, params):
    web.register(email="filter-survivor@example.com")

    response = admin_client.get("/admin/members", params=params)

    assert response.status_code == 200, response.text
    assert _count_members(db_session) == 1, "a bad filter must never mutate data"


def test_admin_per_page_out_of_choices_falls_back_to_default(admin_client):
    response = admin_client.get("/admin/members", params={"per_page": 9999})

    assert response.status_code == 200
    assert f'value="{DEFAULT_PER_PAGE}" selected' in response.text


def test_admin_invalid_status_is_ignored(admin_client, web):
    web.register(email="ignored-status@example.com")

    response = admin_client.get("/admin/members", params={"status": "../../etc"})

    assert response.status_code == 200
    assert "ignored-status@example.com" in response.text, "an unknown status must not filter everything out"


# --------------------------------------------------------------------------- detail
def test_admin_member_detail_shows_attribution_and_events(admin_client, client, web, db_session):
    response = client.post(
        "/register?utm_source=facebook&utm_campaign=detail-campaign",
        data={
            "full_name": "Detail Member",
            "email": "detail@example.com",
            "csrf_token": web.csrf_token(),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    member = _member(db_session, "detail@example.com")
    assert member is not None

    page = admin_client.get(f"/admin/members/{member.id}")

    assert page.status_code == 200, page.text
    assert "detail@example.com" in page.text
    assert "facebook" in page.text
    assert "detail-campaign" in page.text
    assert "REGISTER_COMPLETED" in page.text
    assert "EMAIL_SENT" in page.text


def test_admin_member_detail_unknown_id_is_404(admin_client):
    response = admin_client.get("/admin/members/not-a-real-uuid")
    assert response.status_code == 404


# --------------------------------------------------------------------------- session / csrf
def test_admin_logout_clears_the_session(admin_client):
    page = admin_client.get("/admin/members")
    assert page.status_code == 200

    response = admin_client.post(
        "/admin/logout", data={"csrf_token": _csrf_from(page.text)}, follow_redirects=False
    )

    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]

    after = admin_client.get("/admin/members", follow_redirects=False)
    assert after.status_code in (303, 307)
    assert "/admin/login" in after.headers["location"]


@pytest.mark.parametrize("token", [None, "tampered-token-value"])
def test_admin_login_requires_a_valid_csrf_token(client, web, app_settings, token):
    data = {"email": app_settings.admin_email, "password": ADMIN_PASSWORD}
    if token is not None:
        data["csrf_token"] = web.csrf_token("/admin/login") + token

    response = client.post("/admin/login", data=data, follow_redirects=False)

    assert response.status_code == 403, response.text
    blocked = client.get("/admin/members", follow_redirects=False)
    assert blocked.status_code in (303, 307)
