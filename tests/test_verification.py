"""Email verification: happy path, invalid/expired/reused tokens, races and resends."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db import get_session_factory
from app.models import EmailVerificationToken, Member, MemberEvent, utcnow
from app.security import hash_token
from app.services.members import resend_verification, verify_email

JUNK_TOKENS = ["", "unknown-token-xyz", "<script>alert(1)</script>", "' OR 1=1 --", "../../etc/passwd"]


# --------------------------------------------------------------------------- helpers
def _member(db: Session, email: str) -> Member | None:
    db.expire_all()
    return db.execute(select(Member).where(Member.email == email)).scalar_one_or_none()


def _token_row(db: Session, raw_token: str) -> EmailVerificationToken | None:
    db.expire_all()
    stmt = select(EmailVerificationToken).where(
        EmailVerificationToken.token_hash == hash_token(raw_token)
    )
    return db.execute(stmt).scalar_one_or_none()


def _tokens(db: Session, member_id) -> list[EmailVerificationToken]:
    db.expire_all()
    stmt = select(EmailVerificationToken).where(EmailVerificationToken.member_id == member_id)
    return list(db.execute(stmt).scalars().all())


def _event_types(db: Session, member_id) -> list[str]:
    db.expire_all()
    stmt = select(MemberEvent.event_type).where(MemberEvent.member_id == member_id)
    return [str(value) for value in db.execute(stmt).scalars().all()]


# --------------------------------------------------------------------------- valid token
def test_valid_token_verifies_member_once(web, mailbox, db_session):
    _, email = web.register(email="verify-ok@example.com")
    token = mailbox.latest_token()

    response = web.client.get(f"/verify-email?token={token}", follow_redirects=False)

    assert response.status_code == 200, response.text
    assert "Xác minh thành công" in response.text

    member = _member(db_session, email)
    assert member is not None
    assert member.status == "verified"
    assert member.email_verified_at is not None
    assert member.is_verified is True

    row = _token_row(db_session, token)
    assert row is not None
    assert row.used_at is not None
    assert "EMAIL_VERIFIED" in _event_types(db_session, member.id)


# --------------------------------------------------------------------------- invalid tokens
@pytest.mark.parametrize("junk", JUNK_TOKENS)
def test_invalid_token_is_rejected_and_member_untouched(web, client, db_session, junk):
    _, email = web.register(email="verify-bad@example.com")
    before = _tokens(db_session, _member(db_session, email).id)

    response = client.get("/verify-email", params={"token": junk}, follow_redirects=False)

    assert response.status_code == 400, response.text
    if junk:
        assert junk not in response.text, "the raw token must never be echoed back into the HTML"

    member = _member(db_session, email)
    assert member.status == "pending"
    assert member.email_verified_at is None
    after = _tokens(db_session, member.id)
    assert [token.used_at for token in after] == [token.used_at for token in before]
    assert all(token.used_at is None for token in after)
    assert "EMAIL_VERIFIED" not in _event_types(db_session, member.id)


def test_missing_token_parameter_is_rejected(client):
    response = client.get("/verify-email", follow_redirects=False)
    assert response.status_code == 400
    assert "Xác minh thành công" not in response.text
    assert "Liên kết không hợp lệ" in response.text


# --------------------------------------------------------------------------- expired token
def test_expired_token_returns_410(web, mailbox, db_session):
    _, email = web.register(email="verify-expired@example.com")
    token = mailbox.latest_token()
    row = _token_row(db_session, token)
    assert row is not None

    db_session.execute(
        update(EmailVerificationToken)
        .where(EmailVerificationToken.id == row.id)
        .values(expires_at=utcnow() - timedelta(hours=1))
    )
    db_session.commit()

    response = web.client.get(f"/verify-email?token={token}", follow_redirects=False)

    assert response.status_code == 410, response.text
    member = _member(db_session, email)
    assert member.status == "pending", "an expired token must not verify the member"
    assert member.email_verified_at is None
    assert _token_row(db_session, token).used_at is None
    assert "EMAIL_VERIFIED" not in _event_types(db_session, member.id)


# --------------------------------------------------------------------------- reuse
def test_token_reuse_is_rejected_and_member_stays_verified(web, mailbox, db_session):
    _, email = web.register(email="verify-reuse@example.com")
    token = mailbox.latest_token()

    first = web.client.get(f"/verify-email?token={token}", follow_redirects=False)
    assert first.status_code == 200
    member = _member(db_session, email)
    verified_at = member.email_verified_at

    second = web.client.get(f"/verify-email?token={token}", follow_redirects=False)

    assert second.status_code == 400
    assert "đã được sử dụng" in second.text or "đã dùng" in second.text
    member = _member(db_session, email)
    assert member.status == "verified"
    assert member.email_verified_at == verified_at, "a replay must not re-verify or alter the member"
    assert _event_types(db_session, member.id).count("EMAIL_VERIFIED") == 1


# --------------------------------------------------------------------------- race
def test_concurrent_verification_claims_the_token_exactly_once(web, mailbox, db_session):
    """Two sessions race for the same token: exactly one wins, the loser gets ``used``."""
    _, email = web.register(email="verify-race@example.com")
    token = mailbox.latest_token()
    member = _member(db_session, email)
    assert member is not None

    factory = get_session_factory()
    loser = factory()
    winner = factory()
    try:
        # the loser reads the token while it is still unused ...
        stale = loser.execute(
            select(EmailVerificationToken).where(
                EmailVerificationToken.token_hash == hash_token(token)
            )
        ).scalar_one()
        assert stale.used_at is None

        # ... the winner claims it first ...
        first_outcome = verify_email(winner, token)
        assert first_outcome.status == "verified"
        assert first_outcome.member is not None

        # ... and the loser's conditional UPDATE must not match any row.
        second_outcome = verify_email(loser, token)
        assert second_outcome.status == "used"
    finally:
        loser.close()
        winner.close()

    db_session.expire_all()
    verified = db_session.execute(
        select(func.count(Member.id)).where(Member.status == "verified")
    ).scalar_one()
    used = db_session.execute(
        select(func.count(EmailVerificationToken.id)).where(
            EmailVerificationToken.used_at.is_not(None)
        )
    ).scalar_one()
    assert verified == 1, "exactly one member may end up verified"
    assert used == 1, "exactly one token may be consumed"

    member = _member(db_session, email)
    assert member.status == "verified"
    assert member.email_verified_at is not None
    assert _event_types(db_session, member.id).count("EMAIL_VERIFIED") == 1


# --------------------------------------------------------------------------- resend
def test_resend_after_verification_is_a_noop(web, mailbox, db_session):
    verified, email = web.register_and_verify(email="resend-done@example.com")
    assert verified.status_code == 200
    assert len(mailbox.read()) == 1
    member = _member(db_session, email)

    sent, error = resend_verification(db_session, member)

    assert sent is False
    assert error == "already_verified"
    assert len(mailbox.read()) == 1, "no extra email may be sent to a verified member"
    assert len(_tokens(db_session, member.id)) == 1, "no new token may be issued"
    assert _member(db_session, email).status == "verified"


def test_resend_for_pending_member_issues_a_fresh_token(web, mailbox, db_session):
    _, email = web.register(email="resend-pending@example.com")
    first_token = mailbox.latest_token()
    member = _member(db_session, email)

    sent, error = resend_verification(db_session, member)

    assert sent is True
    assert error is None
    second_token = mailbox.latest_token()
    assert second_token != first_token
    assert len(_tokens(db_session, member.id)) == 2

    assert web.client.get(f"/verify-email?token={first_token}").status_code == 400
    assert web.client.get(f"/verify-email?token={second_token}").status_code == 200
    assert _member(db_session, email).status == "verified"
