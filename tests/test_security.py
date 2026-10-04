"""Security: CSRF, rate limits, headers, body size, token hygiene, secret leakage, auth guards."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import create_app
from app.models import EmailVerificationToken, Member, MemberAttribution
from tests.conftest import WebActions

REQUEST_ID = re.compile(r"[0-9a-f]{32}")
CSP_NONCE = re.compile(r"'nonce-([A-Za-z0-9_\-]+)'")
RAW_IP_MARKERS = ("127.0.0.1", "localhost", "192.168.", "::1")

JUNK_TOKENS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "'; DROP TABLE members; --",
    "' OR '1'='1",
    "%00",
    "../../etc/passwd",
    "a" * 3000,
]

SECRET_VALUES = {
    "SECRET_KEY": "zz-secret-value-zz",
    "IP_HASH_SALT": "zz-ip-salt-zz",
    "ADMIN_PASSWORD_HASH": "scrypt$16384$8$1$zzhashzz$zzdigestzz",
    "MEMBER_API_KEY": "zz-api-key-zz",
    "META_ACCESS_TOKEN": "zz-meta-token-zz",
}


# --------------------------------------------------------------------------- helpers
def _member(db: Session, email: str) -> Member | None:
    db.expire_all()
    return db.execute(select(Member).where(Member.email == email)).scalar_one_or_none()


def _count_members(db: Session) -> int:
    db.expire_all()
    return len(db.execute(select(Member)).scalars().all())


# --------------------------------------------------------------------------- CSRF
def test_register_post_without_csrf_token_is_403(client, db_session):
    client.get("/register")  # establish the double-submit cookie

    response = client.post(
        "/register",
        data={"full_name": "No CSRF", "email": "no-csrf@example.com"},
        follow_redirects=False,
    )

    assert response.status_code == 403, response.text
    assert _count_members(db_session) == 0


def test_register_post_with_tampered_csrf_token_is_403(client, web, db_session):
    token = web.csrf_token()

    response = client.post(
        "/register",
        data={
            "full_name": "Tampered CSRF",
            "email": "tampered-csrf@example.com",
            "csrf_token": token + "x",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403, response.text
    assert _count_members(db_session) == 0


# --------------------------------------------------------------------------- rate limit
def test_register_rate_limit_returns_429_on_the_third_post(settings_env, db_session, mailbox):
    settings_env(REGISTER_RATE_LIMIT="2", REGISTER_RATE_WINDOW_SECONDS="3600")

    with TestClient(create_app()) as api:
        actions = WebActions(api, mailbox)
        assert actions.register(email="rate-limit-1@example.com")[0].status_code == 303
        assert actions.register(email="rate-limit-2@example.com")[0].status_code == 303

        blocked = actions.register(email="rate-limit-3@example.com")[0]

    assert blocked.status_code == 429, blocked.text
    assert _count_members(db_session) == 2, "the throttled registration must not create a member"


def test_html_429_keeps_the_retry_after_header(settings_env, db_session, mailbox):
    """A throttled browser POST advertises ``Retry-After`` (RFC 6585), not just the JSON API."""
    settings_env(REGISTER_RATE_LIMIT="1", REGISTER_RATE_WINDOW_SECONDS="3600")

    with TestClient(create_app()) as api:
        actions = WebActions(api, mailbox)
        assert actions.register(email="retry-after-1@example.com")[0].status_code == 303
        blocked = actions.register(email="retry-after-2@example.com")[0]

    assert blocked.status_code == 429, blocked.text
    retry_after = blocked.headers.get("retry-after")
    assert retry_after, "the HTML 429 must keep the Retry-After header set by enforce_rate_limit"
    assert int(retry_after) >= 1


# --------------------------------------------------------------------------- headers
def test_security_headers_and_request_id(client):
    response = client.get("/register")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "cross-origin-opener-policy" in response.headers
    assert "permissions-policy" in response.headers

    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp

    assert REQUEST_ID.fullmatch(response.headers["x-request-id"])

    nonce_match = CSP_NONCE.search(csp)
    assert nonce_match, f"the CSP carries no per-request nonce: {csp}"
    assert f'nonce="{nonce_match.group(1)}"' in response.text, "the nonce in the HTML must match the CSP"


# --------------------------------------------------------------------------- body size
def test_oversized_html_request_is_413(settings_env):
    settings_env(MAX_REQUEST_BYTES="2000")

    with TestClient(create_app()) as api:
        response = api.post(
            "/register",
            data={"full_name": "x" * 5000, "email": "too-big@example.com"},
            follow_redirects=False,
        )

    assert response.status_code == 413, response.text
    assert "413" in response.text


def test_oversized_api_request_is_413_envelope(settings_env):
    settings_env(MAX_REQUEST_BYTES="2000")

    with TestClient(create_app()) as api:
        response = api.post(
            "/api/v1/members/register",
            json={"full_name": "x" * 5000, "email": "too-big@example.com"},
        )

    assert response.status_code == 413, response.text
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "payload_too_large"


# --------------------------------------------------------------------------- token hygiene
@pytest.mark.parametrize("junk", JUNK_TOKENS)
def test_junk_verification_tokens_never_500_and_are_never_echoed(client, junk):
    response = client.get("/verify-email", params={"token": junk}, follow_redirects=False)

    assert response.status_code in (400, 410), response.text
    assert junk not in response.text, "the raw token must never be reflected into the HTML"
    assert "<script>alert(1)</script>" not in response.text


def test_verification_never_500s_without_a_token(client):
    response = client.get("/verify-email?token", follow_redirects=False)
    assert response.status_code == 400


# --------------------------------------------------------------------------- privacy
def test_no_raw_client_ip_is_persisted(client, web, db_session):
    web.register(email="privacy@example.com", phone="0901234567")

    db_session.expire_all()
    rows = list(db_session.execute(select(MemberAttribution)).scalars().all())
    assert len(rows) == 1
    for row in rows:
        values = {column.name: str(getattr(row, column.name)) for column in row.__table__.columns}
        assert values["ip_hash"], "the salted ip_hash must be stored"
        assert re.fullmatch(r"[0-9a-f]{64}", values["ip_hash"])
        assert "testclient" not in values["ip_hash"], "the raw client host must not be stored"
        for column, value in values.items():
            for marker in RAW_IP_MARKERS:
                assert marker not in value, f"raw client IP {marker!r} leaked into {column!r}"

    member = _member(db_session, "privacy@example.com")
    assert member is not None
    member_values = " ".join(
        str(getattr(member, column.name)) for column in member.__table__.columns
    )
    for marker in RAW_IP_MARKERS:
        assert marker not in member_values

    tokens = list(db_session.execute(select(EmailVerificationToken)).scalars().all())
    assert tokens
    for token in tokens:
        assert re.fullmatch(r"[0-9a-f]{64}", token.token_hash), "only the token hash may be stored"


# --------------------------------------------------------------------------- secret leakage
def test_health_endpoints_never_expose_secrets(settings_env):
    settings_env(**SECRET_VALUES)

    with TestClient(create_app()) as api:
        plain = api.get("/health")
        envelope = api.get("/api/v1/health")

    assert plain.status_code == 200
    assert envelope.status_code == 200
    assert plain.json()["database"] == "ok"
    assert envelope.json()["data"]["database"] == "ok"
    assert envelope.json()["data"]["api_key_required"] is True

    for response in (plain, envelope):
        for name, secret in SECRET_VALUES.items():
            assert secret not in response.text, f"{name} leaked through {response.request.url.path}"


# --------------------------------------------------------------------------- auth guards
def test_admin_exposing_routes_require_authentication(client, web, db_session):
    web.register(email="guarded@example.com")
    member = _member(db_session, "guarded@example.com")
    assert member is not None

    for path in ("/admin/members", f"/admin/members/{member.id}", "/admin/members.csv"):
        response = client.get(path, follow_redirects=False)

        assert response.status_code in (303, 307), response.text
        assert "/admin/login" in response.headers["location"]
        assert "guarded@example.com" not in response.text
        assert "text/csv" not in response.headers.get("content-type", "")


# --------------------------------------------------------------------------- hash encoding
def test_scrypt_hash_uses_a_shell_safe_separator():
    """The encoded hash travels through .env files, Compose env_file and `source`.

    A "$" separator gets interpolated away by Docker Compose ("$1", "$16384") and by shell
    `source`, which silently corrupts the hash and locks the operator out of /admin (this
    happened in production). The canonical encoding must therefore contain no "$".
    """
    from app.security import hash_password, parse_password_hash, verify_password

    encoded = hash_password("Correct-Horse-Battery-9")
    assert "$" not in encoded
    assert encoded.startswith("scrypt:")
    parts = parse_password_hash(encoded)
    assert parts is not None and len(parts) == 6
    assert verify_password("Correct-Horse-Battery-9", encoded) is True
    assert verify_password("wrong", encoded) is False


def test_legacy_dollar_separated_hashes_still_verify():
    from app.security import hash_password, verify_password

    legacy = hash_password("Legacy-Passw0rd").replace(":", "$")
    assert verify_password("Legacy-Passw0rd", legacy) is True


def test_malformed_hash_never_authenticates_and_blocks_production(monkeypatch):
    from app.security import parse_password_hash, verify_password

    for broken in ("", "x", "scrypt63844VSq", "scrypt:16384:8:1:only-five", "bcrypt$1$2$3$4$5"):
        assert parse_password_hash(broken) is None
        assert verify_password("anything", broken) is False

    # ...and a corrupted value is refused at startup instead of locking the operator out later.
    import pytest

    from app.config import Settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "a" * 64)
    monkeypatch.setenv("IP_HASH_SALT", "b" * 32)
    monkeypatch.setenv("MEMBER_API_KEY", "c" * 48)
    monkeypatch.setenv("EMAIL_MODE", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://members.example.com")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "scrypt63844VSq")
    with pytest.raises(Exception) as excinfo:
        Settings(_env_file=None)
    assert "ADMIN_PASSWORD_HASH is malformed" in str(excinfo.value)
