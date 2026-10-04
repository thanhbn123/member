"""Public JSON API v1: envelope shape, registration, lookups, resend and API-key auth."""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.main import create_app
from app.models import Member

REGISTER_URL = "/api/v1/members/register"
HEX64 = re.compile(r"[0-9a-f]{64}")
REQUEST_ID = re.compile(r"[0-9a-f]{32}")


# --------------------------------------------------------------------------- helpers
def _body(email: str = "api@example.com", **overrides) -> dict:
    payload = {"full_name": "API Member", "email": email}
    payload.update(overrides)
    return payload


def _register(client, email: str = "api@example.com", **overrides):
    return client.post(REGISTER_URL, json=_body(email, **overrides))


def _member(db: Session, email: str) -> Member | None:
    db.expire_all()
    return db.execute(select(Member).where(Member.email == email)).scalar_one_or_none()


def _count_members(db: Session) -> int:
    db.expire_all()
    return int(db.execute(select(func.count(Member.id))).scalar_one())


def _assert_envelope(body: dict, success: bool) -> None:
    assert set(body) == {"success", "data", "error", "meta"}, body
    assert body["success"] is success
    assert isinstance(body["meta"], dict)
    if success:
        assert body["error"] is None
    else:
        assert body["data"] is None
        assert isinstance(body["error"], dict)


# --------------------------------------------------------------------------- register
def test_api_register_returns_201_envelope_and_sends_email(client, mailbox, db_session):
    response = _register(client, "api-new@example.com", phone="0901234567", company="API Co")

    assert response.status_code == 201, response.text
    body = response.json()
    _assert_envelope(body, success=True)
    assert REQUEST_ID.fullmatch(str(body["meta"].get("request_id")))

    data = body["data"]
    assert data["member"]["status"] == "pending"
    assert data["member"]["email"] == "api-new@example.com"
    assert data["duplicate"] is False
    assert data["verification_sent"] is True
    assert data["email_error"] is None

    member = _member(db_session, "api-new@example.com")
    assert member is not None
    assert str(member.id) == data["member"]["id"]
    assert member.status == "pending"
    assert member.source == "api"

    messages = mailbox.read()
    assert len(messages) == 1
    assert messages[0]["to"] == "api-new@example.com"
    assert "/verify-email?token=" in messages[0]["url"]


def test_api_register_duplicate_returns_200_and_keeps_one_row(client, mailbox, db_session):
    first = _register(client, "api-dup@example.com")
    assert first.status_code == 201

    second = _register(client, "api-dup@example.com")

    assert second.status_code == 200, "a duplicate must be reported with 200, not 201"
    body = second.json()
    _assert_envelope(body, success=True)
    assert body["data"]["duplicate"] is True
    assert _count_members(db_session) == 1
    assert body["data"]["member"]["id"] == first.json()["data"]["member"]["id"]


@pytest.mark.parametrize(
    "payload",
    [
        {"full_name": "", "email": "valid@example.com"},
        {"email": "valid@example.com"},
        {"full_name": "No Email"},
    ],
)
def test_api_register_validation_errors_have_details(client, db_session, payload):
    response = client.post(REGISTER_URL, json=payload)

    assert response.status_code == 422, response.text
    body = response.json()
    _assert_envelope(body, success=False)
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"]
    assert body["error"]["details"], "validation errors must carry a non-empty details list"
    assert all("message" in item for item in body["error"]["details"])
    assert _count_members(db_session) == 0


@pytest.mark.parametrize("bad_email", ["not-an-email", "a@b", ""])
def test_api_register_rejects_invalid_email(client, db_session, bad_email):
    response = client.post(REGISTER_URL, json={"full_name": "Bad Email", "email": bad_email})

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "validation_error"
    assert _count_members(db_session) == 0


def test_api_register_invalid_email_422_carries_error_details(client, db_session):
    """A schema-valid body with a bad address is still a ``validation_error`` with details.

    INTERNAL_CONTRACT §1: the error envelope of ``validation_error`` carries ``details``;
    an address rejected by ``app.normalize`` must not be the exception.
    """
    response = client.post(REGISTER_URL, json={"full_name": "Bad Email", "email": "not-an-email"})

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "validation_error"
    details = body["error"]["details"]
    assert details, f"validation_error must carry details, got {details!r}"
    assert details[0]["field"] == "email"
    assert details[0]["message"]
    assert _count_members(db_session) == 0


# --------------------------------------------------------------------------- lookup
def test_api_get_member_returns_attribution(client, db_session):
    created = _register(
        client,
        "api-lookup@example.com",
        utm_source="facebook",
        utm_medium="cpc",
        utm_campaign="launch",
        landing_url="https://example.com/landing",
    )
    assert created.status_code == 201
    member_id = created.json()["data"]["member"]["id"]

    response = client.get(f"/api/v1/members/{member_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    _assert_envelope(body, success=True)
    data = body["data"]
    assert data["id"] == member_id
    assert data["email"] == "api-lookup@example.com"
    assert data["status"] == "pending"
    attribution = data["attribution"]
    assert attribution is not None
    assert attribution["utm_source"] == "facebook"
    assert attribution["utm_medium"] == "cpc"
    assert attribution["utm_campaign"] == "launch"
    assert attribution["landing_url"] == "https://example.com/landing"
    assert HEX64.fullmatch(str(attribution["ip_hash"]))


@pytest.mark.parametrize("member_id", [str(uuid.uuid4()), "not-a-uuid", "12345", "%20"])
def test_api_get_unknown_or_malformed_member_is_404(client, member_id):
    response = client.get(f"/api/v1/members/{member_id}")

    assert response.status_code == 404, response.text
    body = response.json()
    _assert_envelope(body, success=False)
    assert body["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- resend
def test_api_resend_verification_for_pending_then_verified_member(client, mailbox, db_session):
    created = _register(client, "api-resend@example.com")
    member_id = created.json()["data"]["member"]["id"]

    pending = client.post(f"/api/v1/members/{member_id}/resend-verification")

    assert pending.status_code == 200, pending.text
    body = pending.json()
    _assert_envelope(body, success=True)
    assert body["data"]["verification_sent"] is True
    assert body["data"]["error"] is None

    token = mailbox.latest_token()
    assert client.get(f"/verify-email?token={token}").status_code == 200
    assert _member(db_session, "api-resend@example.com").status == "verified"

    verified = client.post(f"/api/v1/members/{member_id}/resend-verification")

    assert verified.status_code == 200
    body = verified.json()
    _assert_envelope(body, success=True)
    assert body["data"]["verification_sent"] is False
    assert body["data"]["error"] == "already_verified"


def test_api_resend_unknown_member_is_404(client):
    response = client.post(f"/api/v1/members/{uuid.uuid4()}/resend-verification")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- health
def test_api_health_envelope_has_no_secrets(client, app_settings):
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body, success=True)
    assert body["data"]["database"] == "ok"
    assert body["data"]["status"] == "ok"
    assert isinstance(body["data"]["webhook_enabled"], bool)
    assert isinstance(body["data"]["meta_enabled"], bool)

    secrets = [
        app_settings.secret_key,
        app_settings.ip_hash_salt,
        app_settings.admin_password_hash,
        app_settings.member_api_key,
        app_settings.meta_access_token,
    ]
    for secret in secrets:
        if secret:
            assert secret not in response.text


# --------------------------------------------------------------------------- api key
def test_api_key_is_enforced_when_configured(app, settings_env, mailbox):
    settings_env(MEMBER_API_KEY="s3cret-key")

    with TestClient(create_app()) as api:
        payload = _body("api-key@example.com")

        missing = api.post(REGISTER_URL, json=payload)
        assert missing.status_code == 401, missing.text
        body = missing.json()
        _assert_envelope(body, success=False)
        assert body["error"]["code"] == "unauthorized"

        wrong = api.post(REGISTER_URL, json=payload, headers={"X-API-Key": "wrong-key"})
        assert wrong.status_code == 401
        assert wrong.json()["error"]["code"] == "unauthorized"

        assert api.get("/api/v1/health").status_code == 200, "health must never need a key"

        good = api.post(REGISTER_URL, json=payload, headers={"X-API-Key": "s3cret-key"})
        assert good.status_code == 201, good.text
        assert good.json()["data"]["member"]["email"] == "api-key@example.com"

    assert len(mailbox.read()) == 1
