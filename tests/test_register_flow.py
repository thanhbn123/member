"""Registration flow (``POST /register``): happy path, validation, duplicates, attribution, XSS."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import EmailVerificationToken, Member, MemberAttribution, MemberEvent

RAW_IP_MARKERS = ("127.0.0.1", "testclient", "localhost")
HEX64 = re.compile(r"[0-9a-f]{64}")


# --------------------------------------------------------------------------- helpers
def _member(db: Session, email: str) -> Member | None:
    db.expire_all()
    return db.execute(select(Member).where(Member.email == email)).scalar_one_or_none()


def _count_members(db: Session) -> int:
    db.expire_all()
    return int(db.execute(select(func.count(Member.id))).scalar_one())


def _events(db: Session, member_id=None, event_type: str | None = None) -> list[MemberEvent]:
    db.expire_all()
    stmt = select(MemberEvent)
    if member_id is not None:
        stmt = stmt.where(MemberEvent.member_id == member_id)
    if event_type is not None:
        stmt = stmt.where(MemberEvent.event_type == event_type)
    return list(db.execute(stmt).scalars().all())


def _tokens(db: Session, member_id) -> list[EmailVerificationToken]:
    db.expire_all()
    stmt = select(EmailVerificationToken).where(EmailVerificationToken.member_id == member_id)
    return list(db.execute(stmt).scalars().all())


def _attribution(db: Session, member: Member) -> MemberAttribution | None:
    db.expire_all()
    refreshed = db.get(Member, member.id)
    assert refreshed is not None
    return refreshed.attribution


def _column_values(instance) -> dict[str, str]:
    return {column.name: str(getattr(instance, column.name)) for column in instance.__table__.columns}


def _assert_no_raw_ip(instance) -> None:
    for column, value in _column_values(instance).items():
        for marker in RAW_IP_MARKERS:
            assert marker not in value, f"raw client identity {marker!r} leaked into column {column!r}"


# --------------------------------------------------------------------------- happy path
def test_register_success_creates_pending_member(web, mailbox, db_session):
    response, _ = web.register(email="  User.Name@Example.COM  ", phone="+84 90 123 4567")

    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith("/check-email")

    member = _member(db_session, "user.name@example.com")
    assert member is not None, "no member row was created"
    assert member.status == "pending"
    assert member.email == "user.name@example.com"  # lower-cased + trimmed
    assert member.phone == "+84901234567"  # separators stripped, '+' kept
    assert member.full_name == "Nguyễn Văn A"
    assert member.consent_marketing is False

    started = _events(db_session, event_type="REGISTER_STARTED")
    assert len(started) == 1
    assert started[0].metadata_json["source"] == "web_form"
    completed = _events(db_session, member_id=member.id, event_type="REGISTER_COMPLETED")
    assert len(completed) == 1
    assert completed[0].metadata_json["source"] == "web_form"

    message = mailbox.latest()
    assert message["to"] == "user.name@example.com"
    assert message["url"] is not None
    assert "/verify-email?token=" in message["url"]


@pytest.mark.parametrize("bad_email", ["not-an-email", "a@b", ""])
def test_register_invalid_email_rerenders_and_creates_nothing(client, web, db_session, bad_email):
    # posted by hand: `web.register` substitutes a generated address for an empty one
    response = client.post(
        "/register",
        data={
            "full_name": "Bad Email",
            "email": bad_email,
            "phone": "",
            "company": "",
            "csrf_token": web.csrf_token(),
        },
        follow_redirects=False,
    )

    assert response.status_code == 422, response.text
    assert 'id="register-form"' in response.text  # the form is re-rendered
    assert "alert-error" in response.text
    assert _count_members(db_session) == 0


# --------------------------------------------------------------------------- duplicates
def test_duplicate_email_reissues_token_and_supersedes_old(web, mailbox, db_session):
    first, email = web.register(email="dup@example.com")
    assert first.status_code == 303
    first_token = mailbox.latest_token()

    second, _ = web.register(email="dup@example.com")
    assert second.status_code == 303, "duplicate registration must not reveal the existing account"
    assert _count_members(db_session) == 1

    second_token = mailbox.latest_token()
    assert second_token != first_token, "a duplicate registration must issue a NEW token"

    member = _member(db_session, email)
    assert member is not None
    tokens = _tokens(db_session, member.id)
    assert len(tokens) == 2
    usable = [token for token in tokens if token.used_at is None]
    superseded = [token for token in tokens if token.used_at is not None]
    assert len(usable) == 1, "exactly one token may stay usable"
    assert len(superseded) == 1, "the previous token must be superseded (used_at set)"

    old = web.client.get(f"/verify-email?token={first_token}", follow_redirects=False)
    assert old.status_code == 400, "a superseded token must not verify"
    assert _member(db_session, email).status == "pending"

    new = web.client.get(f"/verify-email?token={second_token}", follow_redirects=False)
    assert new.status_code == 200
    assert _member(db_session, email).status == "verified"


def test_duplicate_after_verification_sends_no_email_and_stays_verified(web, mailbox, db_session):
    verified, email = web.register_and_verify(email="already@example.com")
    assert verified.status_code == 200
    assert len(mailbox.read()) == 1

    member = _member(db_session, email)
    assert member is not None
    verified_at = member.email_verified_at
    assert verified_at is not None

    again, _ = web.register(email=email)
    assert again.status_code == 303
    assert "sent=0" in again.headers["location"], "the redirect must flag that no email was sent"
    assert len(mailbox.read()) == 1, "no second verification email may be sent to a verified member"

    member = _member(db_session, email)
    assert member.status == "verified"
    assert member.email_verified_at == verified_at


# --------------------------------------------------------------------------- attribution
def test_register_captures_full_attribution(client, web, db_session):
    query = "utm_source=facebook&utm_medium=cpc&utm_campaign=test&utm_content=ad1&utm_term=kw"
    response = client.post(
        f"/register?{query}",
        data={
            "full_name": "Attribution User",
            "email": "attribution@example.com",
            "csrf_token": web.csrf_token(),
        },
        headers={"Referer": "https://google.com/search", "User-Agent": "pytest-agent/1.0"},
        cookies={"_fbp": "fb.1.123.456", "_fbc": "fb.1.123.789"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    member = _member(db_session, "attribution@example.com")
    assert member is not None
    attribution = _attribution(db_session, member)
    assert attribution is not None

    assert attribution.utm_source == "facebook"
    assert attribution.utm_medium == "cpc"
    assert attribution.utm_campaign == "test"
    assert attribution.utm_content == "ad1"
    assert attribution.utm_term == "kw"
    assert attribution.fbp == "fb.1.123.456"
    assert attribution.fbc == "fb.1.123.789"
    assert attribution.referrer == "https://google.com/search"
    assert attribution.user_agent == "pytest-agent/1.0"
    assert attribution.landing_url, "landing_url must be captured"
    assert "/register" in attribution.landing_url
    assert "utm_source=facebook" in attribution.landing_url

    assert attribution.ip_hash is not None
    assert HEX64.fullmatch(attribution.ip_hash), f"ip_hash is not a sha256 hex digest: {attribution.ip_hash!r}"
    assert attribution.ip_hash not in RAW_IP_MARKERS

    _assert_no_raw_ip(attribution)
    _assert_no_raw_ip(member)


def test_first_touch_attribution_is_preserved(web, client, db_session):
    payload = {"full_name": "First Touch", "email": "first-touch@example.com"}
    first = client.post(
        "/register?utm_source=facebook&utm_campaign=launch",
        data={**payload, "csrf_token": web.csrf_token()},
        follow_redirects=False,
    )
    assert first.status_code == 303

    second = client.post(
        "/register?utm_source=google&utm_medium=cpc",
        data={**payload, "csrf_token": web.csrf_token()},
        follow_redirects=False,
    )
    assert second.status_code == 303

    member = _member(db_session, "first-touch@example.com")
    assert member is not None
    attribution = _attribution(db_session, member)
    assert attribution is not None
    assert attribution.utm_source == "facebook", "first-touch utm_source must never be overwritten"
    assert attribution.utm_campaign == "launch"
    assert attribution.utm_medium == "cpc", "a previously missing field may still be filled in"


# --------------------------------------------------------------------------- XSS
def test_xss_full_name_stored_raw_but_escaped_in_admin(admin_client, web, db_session):
    payload = "<script>alert(1)</script>"
    response, email = web.register(email="xss@example.com", full_name=payload)
    assert response.status_code == 303

    member = _member(db_session, email)
    assert member is not None
    assert member.full_name == payload, "the raw value must be stored (never silently mutated)"

    listing = admin_client.get("/admin/members")
    assert listing.status_code == 200
    assert payload not in listing.text, "unescaped script tag rendered in the member list"
    assert "&lt;script&gt;" in listing.text

    detail = admin_client.get(f"/admin/members/{member.id}")
    assert detail.status_code == 200
    assert payload not in detail.text, "unescaped script tag rendered in the member detail"
    assert "&lt;script&gt;" in detail.text
