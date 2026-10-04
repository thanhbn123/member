"""Verified-member webhook: disabled, delivered (signed) and failing - verification must survive all."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import tests.conftest as conftest
from app.config import get_settings
from app.integrations import meta, webhook
from app.main import create_app
from app.models import Member, MemberEvent
from app.security import verify_signature
from tests.conftest import WebActions

SECRET = "whsec_test"
WEBHOOK_ENV = {
    "MEMBER_VERIFIED_WEBHOOK_SECRET": SECRET,
    "WEBHOOK_MAX_ATTEMPTS": "2",
    "WEBHOOK_BACKOFF_SECONDS": "0",
    "META_PIXEL_ID": "",
    "META_ACCESS_TOKEN": "",
}


# --------------------------------------------------------------------------- helpers
class _ExplodingClient:
    """Any outbound HTTP attempt is recorded and then blows up."""

    def __init__(self, calls: list[str], error: type[Exception] = RuntimeError) -> None:
        self._calls = calls
        self._error = error

    def __enter__(self) -> _ExplodingClient:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def post(self, url, *args, **kwargs):
        self._calls.append(str(url))
        raise self._error(f"unexpected outbound HTTP call to {url}")


class _ExplodingHTTPX:
    """Stands in for the ``httpx`` module inside the integration modules only."""

    def __init__(self, calls: list[str], error: type[Exception] = RuntimeError) -> None:
        self._calls = calls
        self._error = error

    def Client(self, *args, **kwargs) -> _ExplodingClient:  # noqa: N802 - mimics httpx.Client
        return _ExplodingClient(self._calls, self._error)


def _block_outbound_http(monkeypatch, calls: list[str], error: type[Exception] = RuntimeError) -> None:
    """Replace ``httpx`` inside the integration modules (never inside TestClient itself)."""
    fake = _ExplodingHTTPX(calls, error)
    monkeypatch.setattr(webhook, "httpx", fake)
    monkeypatch.setattr(meta, "httpx", fake)


def _capture_servers(monkeypatch) -> list:
    """Expose the ``webhook_server`` instances (the fixture only yields its ``start`` helper)."""
    captured: list = []
    original_init = conftest.RecordingServer.__init__

    def capturing_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captured.append(self)

    monkeypatch.setattr(conftest.RecordingServer, "__init__", capturing_init)
    return captured


def _member(db: Session, email: str) -> Member | None:
    db.expire_all()
    return db.execute(select(Member).where(Member.email == email)).scalar_one_or_none()


def _events(db: Session, event_type: str) -> list[MemberEvent]:
    db.expire_all()
    stmt = select(MemberEvent).where(MemberEvent.event_type == event_type)
    return list(db.execute(stmt).scalars().all())


def _register_and_verify(api, actions: WebActions, mailbox, email: str):
    response = api.post(
        "/register?utm_source=facebook&utm_campaign=webhook-test",
        data={"full_name": "Webhook Member", "email": email, "csrf_token": actions.csrf_token()},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    token = mailbox.latest_token()
    return api.get(f"/verify-email?token={token}", follow_redirects=False)


# --------------------------------------------------------------------------- disabled
def test_disabled_webhook_performs_no_outbound_call(monkeypatch, settings_env, db_session, mailbox):
    settings_env(MEMBER_VERIFIED_WEBHOOK_URL="", MEMBER_VERIFIED_WEBHOOK_SECRET="",
                 META_PIXEL_ID="", META_ACCESS_TOKEN="")

    calls: list[str] = []
    _block_outbound_http(monkeypatch, calls, error=AssertionError)

    with TestClient(create_app()) as api:
        verified = _register_and_verify(api, WebActions(api, mailbox), mailbox, "webhook-off@example.com")

    assert verified.status_code == 200, verified.text
    assert calls == [], "the disabled webhook must not attempt any delivery"
    assert _member(db_session, "webhook-off@example.com").status == "verified"
    assert _events(db_session, "WEBHOOK_SENT") == []
    assert _events(db_session, "WEBHOOK_FAILED") == []


# --------------------------------------------------------------------------- delivered
def test_enabled_webhook_is_delivered_signed_and_recorded(
    monkeypatch, settings_env, db_session, mailbox, webhook_server
):
    servers = _capture_servers(monkeypatch)
    base_url = webhook_server()
    settings_env(MEMBER_VERIFIED_WEBHOOK_URL=base_url, **WEBHOOK_ENV)

    with TestClient(create_app()) as api:
        verified = _register_and_verify(api, WebActions(api, mailbox), mailbox, "webhook-on@example.com")

    assert verified.status_code == 200, verified.text
    assert _member(db_session, "webhook-on@example.com").status == "verified"

    assert len(servers) == 1
    requests = servers[0].requests
    assert len(requests) == 1, "exactly one delivery for a successful verification"

    record = requests[0]
    headers = record["headers"]
    body = record["body"]

    assert record["path"] == "/hook"
    assert headers["x-member-event"] == "member.verified"
    assert headers["x-member-timestamp"]
    assert headers["x-member-delivery"]
    assert headers["content-type"] == "application/json"
    assert verify_signature(SECRET, headers["x-member-timestamp"], body, headers["x-member-signature"]), (
        "X-Member-Signature must verify with app.security.verify_signature"
    )

    payload = json.loads(body)
    assert payload["event"] == "member.verified"
    assert payload["member"]["email"] == "webhook-on@example.com"
    assert payload["member"]["status"] == "verified"
    assert payload["member"]["email_verified_at"]
    assert payload["attribution"]["utm_source"] == "facebook"
    assert payload["attribution"]["utm_campaign"] == "webhook-test"
    assert "ip_hash" not in payload["attribution"], "the salted ip_hash must never leave the service"

    sent = _events(db_session, "WEBHOOK_SENT")
    assert len(sent) == 1, "a successful delivery must be audited"
    assert sent[0].metadata_json["status"] == "sent"
    assert sent[0].member_id is not None


# --------------------------------------------------------------------------- failing
def test_webhook_http_500_does_not_break_verification(
    monkeypatch, settings_env, db_session, mailbox, webhook_server
):
    servers = _capture_servers(monkeypatch)
    base_url = webhook_server(status_code=500)
    settings_env(MEMBER_VERIFIED_WEBHOOK_URL=base_url, **WEBHOOK_ENV)

    with TestClient(create_app()) as api:
        verified = _register_and_verify(api, WebActions(api, mailbox), mailbox, "webhook-500@example.com")

    assert verified.status_code == 200, "a broken webhook must never break email verification"
    member = _member(db_session, "webhook-500@example.com")
    assert member.status == "verified"
    assert member.email_verified_at is not None

    assert len(servers[0].requests) == 2, "WEBHOOK_MAX_ATTEMPTS=2 retries a 5xx"
    failed = _events(db_session, "WEBHOOK_FAILED")
    assert len(failed) == 1
    assert failed[0].metadata_json["status"] == "failed"
    assert failed[0].metadata_json["http_status"] == 500
    assert _events(db_session, "WEBHOOK_SENT") == []


def test_webhook_connection_error_does_not_break_verification(
    settings_env, db_session, mailbox, closed_port
):
    settings_env(
        MEMBER_VERIFIED_WEBHOOK_URL=f"http://127.0.0.1:{closed_port}/hook",
        **WEBHOOK_ENV,
    )

    with TestClient(create_app()) as api:
        verified = _register_and_verify(
            api, WebActions(api, mailbox), mailbox, "webhook-closed@example.com"
        )

    assert verified.status_code == 200, "an unreachable webhook must never break email verification"
    member = _member(db_session, "webhook-closed@example.com")
    assert member.status == "verified"
    assert member.email_verified_at is not None

    failed = _events(db_session, "WEBHOOK_FAILED")
    assert len(failed) == 1, "the failed delivery must be audited"
    assert failed[0].metadata_json["status"] == "failed"
    assert failed[0].metadata_json["error"]
    assert _events(db_session, "WEBHOOK_SENT") == []


def test_verification_succeeds_even_when_the_webhook_client_explodes(
    monkeypatch, settings_env, db_session, mailbox
):
    """The dispatch layer must swallow anything the webhook raises (belt and braces)."""
    settings_env(
        MEMBER_VERIFIED_WEBHOOK_URL="http://127.0.0.1:9/hook",
        **WEBHOOK_ENV,
    )

    calls: list[str] = []
    _block_outbound_http(monkeypatch, calls, error=RuntimeError)

    with TestClient(create_app()) as api:
        verified = _register_and_verify(
            api, WebActions(api, mailbox), mailbox, "webhook-boom@example.com"
        )

    assert calls, "the webhook was enabled, so a delivery must have been attempted"
    assert verified.status_code == 200
    assert _member(db_session, "webhook-boom@example.com").status == "verified"
    assert len(_events(db_session, "WEBHOOK_FAILED")) == 1


@pytest.mark.parametrize("secret", ["", "whsec_test"])
def test_webhook_requires_both_url_and_secret(settings_env, secret):
    settings_env(
        MEMBER_VERIFIED_WEBHOOK_URL="http://127.0.0.1:9/hook",
        MEMBER_VERIFIED_WEBHOOK_SECRET=secret,
    )

    assert get_settings().webhook_enabled is (secret == "whsec_test")
