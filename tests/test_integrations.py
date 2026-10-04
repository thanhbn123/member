"""Tests for :mod:`app.integrations` (webhook, Meta Conversions API, dispatch).

Self-contained on purpose: this module builds its own SQLite database and settings
environment (``monkeypatch.setenv`` + ``reset_settings_cache`` + ``dispose_engine``)
and does not rely on any shared fixture from ``tests/conftest.py``. No test performs
a real network call: ``httpx.Client.post`` is always replaced.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app import db as app_db
from app.config import get_settings, reset_settings_cache
from app.integrations import dispatch, meta, webhook
from app.models import Base, EventType, Member, MemberAttribution, MemberEvent
from app.security import verify_signature

WEBHOOK_URL = "https://hooks.acme.test/member-verified"
WEBHOOK_SECRET = "test-webhook-secret"
META_PIXEL_ID = "1234567890"
META_TOKEN = "EAAG-test-access-token-do-not-log"


# --------------------------------------------------------------------------- fixtures
@pytest.fixture()
def settings_env(monkeypatch, tmp_path):
    """Isolated settings + SQLite database; both integrations disabled by default."""
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("BRAND_NAME", "Acme Test Brand")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'integrations-tests.db'}")
    monkeypatch.setenv("MEMBER_VERIFIED_WEBHOOK_URL", "")
    monkeypatch.setenv("MEMBER_VERIFIED_WEBHOOK_SECRET", "")
    monkeypatch.setenv("WEBHOOK_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("WEBHOOK_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("WEBHOOK_BACKOFF_SECONDS", "1.0")
    monkeypatch.setenv("META_PIXEL_ID", "")
    monkeypatch.setenv("META_ACCESS_TOKEN", "")
    monkeypatch.setenv("META_API_VERSION", "v21.0")
    monkeypatch.setenv("META_TEST_EVENT_CODE", "")
    monkeypatch.setenv("META_TIMEOUT_SECONDS", "10")
    reset_settings_cache()
    app_db.dispose_engine()
    try:
        yield get_settings()
    finally:
        reset_settings_cache()
        app_db.dispose_engine()


@pytest.fixture()
def db_session(settings_env):
    engine = app_db.get_engine()
    Base.metadata.create_all(engine)
    session = app_db.get_session_factory()()
    try:
        yield session
    finally:
        session.close()
        app_db.dispose_engine()
        reset_settings_cache()


@pytest.fixture()
def member(db_session):
    """A verified member with full attribution, persisted in SQLite."""
    row = Member(
        id=uuid.uuid4(),
        full_name="Nguyễn Văn A",
        email="a@example.com",
        phone="+84912345678",
        company="Acme",
        status="verified",
        consent_marketing=True,
        email_verified_at=datetime.now(UTC),
    )
    row.attribution = MemberAttribution(
        utm_source="facebook",
        utm_medium="cpc",
        utm_campaign="launch",
        landing_url="https://acme.test/register?utm_source=facebook",
        referrer="https://facebook.com/",
        fbp="fb.1.123.456",
        fbc="fb.1.123.abc",
        user_agent="pytest-agent/1.0",
        ip_hash="salted-hash-must-not-leak",
    )
    db_session.add(row)
    db_session.commit()
    return row


# --------------------------------------------------------------------------- helpers
def _enable_webhook(monkeypatch) -> None:
    monkeypatch.setenv("MEMBER_VERIFIED_WEBHOOK_URL", WEBHOOK_URL)
    monkeypatch.setenv("MEMBER_VERIFIED_WEBHOOK_SECRET", WEBHOOK_SECRET)
    reset_settings_cache()


def _enable_meta(monkeypatch, *, test_event_code: str = "") -> None:
    monkeypatch.setenv("META_PIXEL_ID", META_PIXEL_ID)
    monkeypatch.setenv("META_ACCESS_TOKEN", META_TOKEN)
    monkeypatch.setenv("META_TEST_EVENT_CODE", test_event_code)
    reset_settings_cache()


def _capture_post(monkeypatch, responder):
    """Replace ``httpx.Client.post``; ``responder(index, url, kwargs)`` returns a response."""
    calls: list[dict] = []

    def fake_post(self, url, **kwargs):
        calls.append({"url": str(url), "kwargs": kwargs, "timeout": self.timeout})
        return responder(len(calls), url, kwargs)

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    return calls


def _forbid_post(monkeypatch) -> None:
    def fake_post(self, *args, **kwargs):
        raise AssertionError("httpx.Client.post must not be called while disabled")

    monkeypatch.setattr(httpx.Client, "post", fake_post)


# --------------------------------------------------------------------------- webhook
def test_webhook_is_enabled_requires_url_and_secret(settings_env, monkeypatch):
    assert webhook.is_enabled() is False

    monkeypatch.setenv("MEMBER_VERIFIED_WEBHOOK_URL", WEBHOOK_URL)
    reset_settings_cache()
    assert webhook.is_enabled() is False  # secret still missing

    monkeypatch.setenv("MEMBER_VERIFIED_WEBHOOK_SECRET", WEBHOOK_SECRET)
    reset_settings_cache()
    assert webhook.is_enabled() is True


def test_webhook_disabled_makes_no_network_call(monkeypatch, member):
    _forbid_post(monkeypatch)

    result = webhook.send_member_verified(member)

    assert result["status"] == "disabled"
    assert result["attempts"] == 0


def test_webhook_success_signs_the_exact_bytes_sent(monkeypatch, member):
    _enable_webhook(monkeypatch)
    calls = _capture_post(monkeypatch, lambda index, url, kwargs: httpx.Response(200, json={"ok": True}))

    result = webhook.send_member_verified(member)

    assert result["status"] == "sent"
    assert result["attempts"] == 1
    assert result["http_status"] == 200
    assert result["error"] is None
    assert result["duration_ms"] >= 0
    assert len(calls) == 1

    call = calls[0]
    assert call["url"] == WEBHOOK_URL
    assert float(call["timeout"].read) == 10.0
    body = call["kwargs"]["content"]
    headers = call["kwargs"]["headers"]
    assert isinstance(body, bytes)
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Member-Event"] == "member.verified"
    # the signature covers "<timestamp>.<body>" with the exact bytes that were sent
    assert verify_signature(
        WEBHOOK_SECRET, headers["X-Member-Timestamp"], body, headers["X-Member-Signature"]
    )
    assert not verify_signature(
        WEBHOOK_SECRET, headers["X-Member-Timestamp"], body + b" ", headers["X-Member-Signature"]
    )
    assert not verify_signature(
        WEBHOOK_SECRET, "1999-01-01T00:00:00+00:00", body, headers["X-Member-Signature"]
    )
    assert uuid.UUID(headers["X-Member-Delivery"])

    payload = json.loads(body)
    assert payload["event"] == "member.verified"
    assert payload["occurred_at"]
    assert payload["member"]["id"] == str(member.id)
    assert payload["member"]["full_name"] == "Nguyễn Văn A"
    assert payload["member"]["email"] == member.email
    assert payload["member"]["phone"] == member.phone
    assert payload["member"]["status"] == "verified"
    assert payload["member"]["consent_marketing"] is True
    assert payload["member"]["email_verified_at"]
    assert payload["member"]["created_at"]
    assert payload["attribution"]["utm_source"] == "facebook"
    assert payload["attribution"]["utm_campaign"] == "launch"
    assert payload["attribution"]["landing_url"].startswith("https://acme.test/register")
    assert payload["attribution"]["fbp"] == "fb.1.123.456"
    assert "ip_hash" not in payload["attribution"]  # the salted IP hash is never published
    assert "ip_hash" not in body.decode("utf-8")
    assert "salted-hash-must-not-leak" not in body.decode("utf-8")
    assert WEBHOOK_SECRET not in body.decode("utf-8")


def test_webhook_retries_with_exponential_backoff_then_fails(monkeypatch, member):
    _enable_webhook(monkeypatch)
    delays: list[float] = []
    monkeypatch.setattr(webhook, "_sleep", delays.append)
    calls = _capture_post(monkeypatch, lambda index, url, kwargs: httpx.Response(500, text="nope"))

    result = webhook.send_member_verified(member)

    assert result["status"] == "failed"
    assert result["attempts"] == get_settings().webhook_max_attempts == 3
    assert result["http_status"] == 500
    assert result["error"] == "HTTP 500"
    assert len(calls) == 3
    assert delays == [1.0, 2.0]  # backoff * 2**attempt
    # identical signed bytes on every attempt, one delivery id per event
    assert {call["kwargs"]["content"] for call in calls} == {calls[0]["kwargs"]["content"]}
    assert len({call["kwargs"]["headers"]["X-Member-Delivery"] for call in calls}) == 1


def test_webhook_succeeds_after_a_retry(monkeypatch, member):
    _enable_webhook(monkeypatch)
    monkeypatch.setattr(webhook, "_sleep", lambda seconds: None)
    calls = _capture_post(
        monkeypatch,
        lambda index, url, kwargs: httpx.Response(200) if index == 2 else httpx.Response(503),
    )

    result = webhook.send_member_verified(member)

    assert result["status"] == "sent"
    assert result["attempts"] == 2
    assert result["http_status"] == 200
    assert len(calls) == 2


def test_webhook_transport_error_is_reported_not_raised(monkeypatch, member):
    _enable_webhook(monkeypatch)
    monkeypatch.setattr(webhook, "_sleep", lambda seconds: None)

    def responder(index, url, kwargs):
        raise httpx.ConnectError("connection refused")

    _capture_post(monkeypatch, responder)

    result = webhook.send_member_verified(member)

    assert result["status"] == "failed"
    assert result["attempts"] == 3
    assert result["http_status"] is None
    assert result["error"] is not None
    assert "connection refused" in result["error"]
    assert WEBHOOK_SECRET not in result["error"]


def test_webhook_survives_a_detached_member_without_attribution(monkeypatch, member):
    _enable_webhook(monkeypatch)
    calls = _capture_post(monkeypatch, lambda index, url, kwargs: httpx.Response(204))

    detached = SimpleNamespace(
        id=member.id,
        full_name=member.full_name,
        email=member.email,
        phone=None,
        company=None,
        status="verified",
        consent_marketing=False,
        email_verified_at=None,
        created_at=None,
    )
    monkeypatch.setattr(webhook, "_sleep", lambda seconds: None)

    result = webhook.send_member_verified(detached)

    assert result["status"] == "sent"
    payload = json.loads(calls[0]["kwargs"]["content"])
    assert payload["attribution"] == {}
    assert payload["member"]["status"] == "verified"


# --------------------------------------------------------------------------- meta
def test_meta_disabled_makes_no_network_call(monkeypatch, member):
    _forbid_post(monkeypatch)

    result = meta.send_complete_registration(member, member.attribution)

    assert result == {
        "status": "disabled",
        "reason": "META_PIXEL_ID/META_ACCESS_TOKEN not configured",
    }
    assert meta.is_enabled() is False


def test_meta_enabled_requires_both_pixel_and_token(settings_env, monkeypatch):
    monkeypatch.setenv("META_PIXEL_ID", META_PIXEL_ID)
    reset_settings_cache()
    assert meta.is_enabled() is False

    monkeypatch.setenv("META_ACCESS_TOKEN", META_TOKEN)
    reset_settings_cache()
    assert meta.is_enabled() is True


def test_meta_posts_hashed_user_data(monkeypatch, member):
    _enable_meta(monkeypatch)
    calls = _capture_post(
        monkeypatch, lambda index, url, kwargs: httpx.Response(200, json={"events_received": 1})
    )

    result = meta.send_complete_registration(member, member.attribution)

    assert result["status"] == "sent"
    assert result["http_status"] == 200
    assert result["error"] is None
    assert len(calls) == 1

    call = calls[0]
    assert call["url"] == f"https://graph.facebook.com/v21.0/{META_PIXEL_ID}/events"
    assert call["kwargs"]["params"] == {"access_token": META_TOKEN}
    assert float(call["timeout"].read) == 10.0

    payload = call["kwargs"]["json"]
    assert "test_event_code" not in payload
    event = payload["data"][0]
    assert event["event_name"] == "CompleteRegistration"
    assert event["action_source"] == "website"
    assert event["event_id"] == str(member.id)  # stable id -> Meta can de-duplicate
    assert isinstance(event["event_time"], int)
    assert event["user_data"]["em"] == [hashlib.sha256(member.email.encode()).hexdigest()]
    assert event["user_data"]["ph"] == [hashlib.sha256(b"84912345678").hexdigest()]
    assert event["user_data"]["fbp"] == "fb.1.123.456"
    assert event["user_data"]["fbc"] == "fb.1.123.abc"
    assert event["user_data"]["client_user_agent"] == "pytest-agent/1.0"
    assert "client_ip_address" not in event["user_data"]  # raw IPs are never stored
    assert event["custom_data"]["utm_source"] == "facebook"
    assert "ip_hash" not in json.dumps(payload)


def test_meta_includes_test_event_code_and_source_url(monkeypatch, member):
    _enable_meta(monkeypatch, test_event_code="TEST12345")
    calls = _capture_post(monkeypatch, lambda index, url, kwargs: httpx.Response(200, json={}))

    source_url = "https://acme.test/verify-email?token=abc"
    result = meta.send_complete_registration(member, member.attribution, event_source_url=source_url)

    assert result["status"] == "sent"
    payload = calls[0]["kwargs"]["json"]
    assert payload["test_event_code"] == "TEST12345"
    assert payload["data"][0]["event_source_url"] == source_url


def test_meta_transport_failure_never_leaks_the_access_token(monkeypatch, member):
    _enable_meta(monkeypatch)
    leaky_url = f"https://graph.facebook.com/v21.0/{META_PIXEL_ID}/events?access_token={META_TOKEN}"

    def responder(index, url, kwargs):
        raise httpx.ConnectError(f"failed to connect to {leaky_url}")

    _capture_post(monkeypatch, responder)

    result = meta.send_complete_registration(member, member.attribution)

    assert result["status"] == "failed"
    assert result["http_status"] is None
    assert META_TOKEN not in result["error"]
    assert "***" in result["error"]


def test_meta_http_error_is_reported_and_sanitized(monkeypatch, member):
    _enable_meta(monkeypatch)
    _capture_post(
        monkeypatch,
        lambda index, url, kwargs: httpx.Response(400, text=f"bad access_token {META_TOKEN}"),
    )

    result = meta.send_complete_registration(member, member.attribution)

    assert result["status"] == "failed"
    assert result["http_status"] == 400
    assert META_TOKEN not in result["error"]


def test_meta_missing_attribution_still_sends(monkeypatch, member):
    _enable_meta(monkeypatch)
    calls = _capture_post(monkeypatch, lambda index, url, kwargs: httpx.Response(200, json={}))

    result = meta.send_complete_registration(member, None)

    assert result["status"] == "sent"
    event = calls[0]["kwargs"]["json"]["data"][0]
    assert event["user_data"]["em"]
    assert "fbp" not in event["user_data"]
    assert "custom_data" not in event


# --------------------------------------------------------------------------- dispatch
def test_dispatch_records_and_commits_events(db_session, member, monkeypatch):
    monkeypatch.setattr(
        webhook,
        "send_member_verified",
        lambda m: {"status": "sent", "attempts": 1, "http_status": 200, "error": None, "duration_ms": 3.0},
    )
    monkeypatch.setattr(
        meta,
        "send_complete_registration",
        lambda *args, **kwargs: {"status": "failed", "http_status": 500, "error": "HTTP 500"},
    )

    results = dispatch.notify_member_verified(member, db_session)

    assert [r["status"] for r in results] == ["sent", "failed"]
    events = {e.event_type: e for e in db_session.query(MemberEvent).all()}
    assert EventType.WEBHOOK_SENT.value in events
    assert EventType.META_EVENT_FAILED.value in events
    assert events[EventType.WEBHOOK_SENT.value].member_id == member.id
    assert events[EventType.WEBHOOK_SENT.value].metadata_json["status"] == "sent"
    assert events[EventType.META_EVENT_FAILED.value].metadata_json["http_status"] == 500

    other = app_db.get_session_factory()()
    try:
        assert other.query(MemberEvent).count() == 2  # committed, not merely flushed
    finally:
        other.close()


def test_dispatch_skips_event_rows_for_disabled_integrations(monkeypatch, db_session, member):
    _forbid_post(monkeypatch)

    results = dispatch.notify_member_verified(member, db_session)

    assert [r["status"] for r in results] == ["disabled", "disabled"]
    assert db_session.query(MemberEvent).count() == 0


def test_dispatch_never_raises_when_integrations_explode(monkeypatch, db_session, member):
    def boom(*args, **kwargs):
        raise RuntimeError("integration exploded")

    monkeypatch.setattr(webhook, "send_member_verified", boom)
    monkeypatch.setattr(meta, "send_complete_registration", boom)

    results = dispatch.notify_member_verified(member, db_session)

    assert len(results) == 2
    assert all(result["status"] == "failed" for result in results)
    assert all("integration exploded" in result["error"] for result in results)
    event_types = {event.event_type for event in db_session.query(MemberEvent).all()}
    assert event_types == {EventType.WEBHOOK_FAILED.value, EventType.META_EVENT_FAILED.value}


def test_dispatch_survives_a_broken_event_log(monkeypatch, db_session, member):
    monkeypatch.setattr(webhook, "send_member_verified", lambda m: {"status": "sent", "attempts": 1})
    monkeypatch.setattr(meta, "send_complete_registration", lambda *args, **kwargs: {"status": "sent"})

    def broken_record_event(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(dispatch, "record_event", broken_record_event)

    results = dispatch.notify_member_verified(member, db_session)

    assert [r["status"] for r in results] == ["sent", "sent"]


def test_dispatch_reports_non_dict_results_as_failed(monkeypatch, db_session, member):
    monkeypatch.setattr(webhook, "send_member_verified", lambda m: "oops")
    monkeypatch.setattr(meta, "send_complete_registration", lambda *args, **kwargs: None)

    results = dispatch.notify_member_verified(member, db_session)

    assert [r["status"] for r in results] == ["failed", "failed"]
    assert all("unexpected result type" in r["error"] for r in results)


def test_dispatch_without_db_still_returns_results(monkeypatch, member):
    monkeypatch.setattr(webhook, "send_member_verified", lambda m: {"status": "sent", "attempts": 1})
    monkeypatch.setattr(meta, "send_complete_registration", lambda *args, **kwargs: {"status": "sent"})

    results = dispatch.notify_member_verified(member)

    assert [r["status"] for r in results] == ["sent", "sent"]


def test_dispatch_never_raises_for_a_bare_object(db_session):
    results = dispatch.notify_member_verified(SimpleNamespace(id="not-a-uuid", email=None), db_session)

    assert [r["status"] for r in results] == ["disabled", "disabled"]


def test_package_reexports(settings_env):
    from app.integrations import (  # noqa: PLC0415 - explicit re-export check
        meta_is_enabled,
        notify_member_verified,
        send_complete_registration,
        send_member_verified,
        webhook_is_enabled,
    )

    assert notify_member_verified is dispatch.notify_member_verified
    assert send_member_verified is webhook.send_member_verified
    assert send_complete_registration is meta.send_complete_registration
    assert meta_is_enabled() is False
    assert webhook_is_enabled() is False
