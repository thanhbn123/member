"""Regression tests for the adversarial review findings (B1, B2, H1-H3, M1, M3, M4, L4, L5, M6/L7).

Every test encodes the *required* behaviour, never the current (buggy) one: a red test
here means the matching application fix has not landed yet. Assertions must not be
softened to make a test pass.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.main import create_app
from app.models import Member, MemberAttribution, utcnow
from app.security import hash_ip
from tests.conftest import WebActions

REPO_ROOT = Path(__file__).resolve().parents[1]
API_REGISTER = "/api/v1/members/register"
CSRF_INPUT = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
PAGE_INFO = re.compile(r"Trang (\d+) / (\d+)")


# --------------------------------------------------------------------------- helpers
def _csrf(client) -> str:
    response = client.get("/register")
    assert response.status_code == 200, response.text
    match = CSRF_INPUT.search(response.text)
    assert match, "no csrf_token field rendered on /register"
    return match.group(1)


def _form_payload(csrf: str, email: str, **overrides: str) -> dict[str, str]:
    payload = {
        "full_name": "Nguyễn Văn A",
        "email": email,
        "phone": "0901234567",
        "company": "Công ty TNHH ABC",
        "csrf_token": csrf,
    }
    payload.update(overrides)
    return payload


def _member(db: Session, email: str) -> Member | None:
    db.expire_all()
    return db.execute(select(Member).where(Member.email == email)).scalar_one_or_none()


def _count_members(db: Session, email: str | None = None) -> int:
    db.expire_all()
    stmt = select(func.count(Member.id))
    if email is not None:
        stmt = stmt.where(Member.email == email)
    return int(db.execute(stmt).scalar_one())


def _attribution(db: Session, member: Member | None) -> MemberAttribution | None:
    assert member is not None, "the member row is missing"
    db.expire_all()
    fresh = db.get(Member, member.id)
    assert fresh is not None
    return fresh.attribution


def _seed_members(db: Session, count: int) -> None:
    base = utcnow()
    for index in range(count):
        stamp = base.replace(microsecond=index)
        db.add(
            Member(
                full_name=f"Hostile Page User {index:02d}",
                email=f"hostile-page-{index:02d}@example.com",
                status="pending",
                created_at=stamp,
                updated_at=stamp,
            )
        )
    db.commit()


def _chunks(body: bytes, size: int = 8192) -> Iterator[bytes]:
    for start in range(0, len(body), size):
        yield body[start : start + size]


def _multipart_body(fields: dict[str, str], boundary: str, *, padding: int = 0) -> bytes:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    if padding:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="padding"\r\n\r\n'.encode())
        parts.append(b"x" * padding)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


# --------------------------------------------------------------------------- B1: over-long attribution
def test_b1_over_long_attribution_never_500(client):
    """The reviewer's repro: over-long UTM / click id / referer / user-agent must never 500."""
    cases: dict[str, dict] = {
        "utm_source=256": {"params": {"utm_source": "s" * 256}},
        "utm_content=2100": {"params": {"utm_content": "c" * 2100}},
        "fbclid=256": {"params": {"fbclid": "f" * 256}},
        "referer=2090": {"headers": {"Referer": "https://a.example/" + "r" * 2090}},
        "user-agent=600": {"headers": {"User-Agent": "u" * 600}},
    }

    responses: dict[str, httpx.Response] = {}
    for label, kwargs in cases.items():
        response = client.get("/register", follow_redirects=False, **kwargs)
        assert response.status_code != 500, f"{label} returned 500: {response.text[:400]}"
        assert response.status_code == 200, f"{label} returned {response.status_code}"
        assert 'id="register-form"' in response.text, f"{label} did not render the registration form"
        responses[label] = response

    for label, field in (("utm_source=256", "utm_source"), ("utm_content=2100", "utm_content")):
        match = re.search(rf'name="{field}"\s+value="([^"]*)"', responses[label].text)
        assert match, f"{field} is not rendered as a form field"
        assert len(match.group(1)) <= 255, f"{field} was rendered with {len(match.group(1))} characters"


def test_b1_over_long_utm_source_on_post_still_registers(client, web, db_session):
    response = client.post(
        "/register",
        params={"utm_source": "b" * 300},
        data=_form_payload(web.csrf_token(), "long-utm-post@example.com"),
        follow_redirects=False,
    )

    assert response.status_code != 500, response.text[:400]
    assert response.status_code == 303, response.text

    member = _member(db_session, "long-utm-post@example.com")
    assert member is not None, "the registration did not create a member"
    assert member.status == "pending"

    attribution = _attribution(db_session, member)
    assert attribution is not None, "the attribution row must still be written"
    assert attribution.utm_source is not None, "the over-long utm_source must be truncated, not dropped"
    assert len(attribution.utm_source) <= 255, f"stored {len(attribution.utm_source)} characters"


# --------------------------------------------------------------------------- B2: email lookup validation
@pytest.mark.parametrize("bad_email", ["x", "", "%", "notanemail", "😀"])
def test_b2_api_email_lookup_rejects_invalid_email(client, bad_email):
    response = client.get("/api/v1/members", params={"email": bad_email})

    assert response.status_code != 500, f"email={bad_email!r} returned 500: {response.text[:400]}"
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "validation_error"
    details = body["error"]["details"]
    assert details, "a validation_error must carry details"
    assert details[0]["field"] == "email"
    assert details[0]["message"]


def test_b2_api_email_lookup_finds_a_member_with_attribution(client, db_session):
    created = client.post(
        API_REGISTER,
        json={
            "full_name": "Email Lookup",
            "email": "lookup-by-email@example.com",
            "utm_source": "facebook",
            "utm_medium": "cpc",
        },
    )
    assert created.status_code == 201, created.text
    member_id = created.json()["data"]["member"]["id"]

    response = client.get("/api/v1/members", params={"email": "lookup-by-email@example.com"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    payload = data["member"] if isinstance(data, dict) and "member" in data else data
    assert payload["id"] == member_id
    assert payload["email"] == "lookup-by-email@example.com"
    assert payload["status"] == "pending"
    assert payload["attribution"]["utm_source"] == "facebook"
    assert payload["attribution"]["utm_medium"] == "cpc"


def test_b2_api_email_lookup_unknown_valid_email_is_404(client):
    response = client.get("/api/v1/members", params={"email": "nobody@example.com"})

    assert response.status_code == 404, response.text
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- H1: production refuses to start
_PROBE = """
from app.config import Settings

try:
    settings = Settings(_env_file=None)
except Exception as exc:
    print(f"REJECTED {type(exc).__name__}: {exc}")
    raise SystemExit(3)
print(f"OK env={settings.app_env} api_key_required={settings.api_key_required}")
"""

PRODUCTION_ENV = {
    "APP_ENV": "production",
    "EMAIL_MODE": "smtp",
    "SMTP_HOST": "smtp.example.com",
    "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
    "PUBLIC_BASE_URL": "https://members.example.com",
}


def _settings_probe(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Load ``Settings`` in a subprocess with a fully controlled environment (no leakage)."""
    child_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        **PRODUCTION_ENV,
        **env,
    }
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _production_credentials() -> dict[str, str]:
    return {
        "SECRET_KEY": secrets.token_urlsafe(48),
        "IP_HASH_SALT": secrets.token_urlsafe(24),
    }


def test_h1_production_refuses_to_start_without_api_key(tmp_path):
    result = _settings_probe(tmp_path, **_production_credentials(), MEMBER_API_KEY="")

    assert result.returncode != 0, f"production started with no API key: {result.stdout}"
    assert "MEMBER_API_KEY" in result.stdout + result.stderr, result.stdout + result.stderr


def test_h1_production_refuses_a_placeholder_api_key(tmp_path):
    result = _settings_probe(tmp_path, **_production_credentials(), MEMBER_API_KEY="change-me-api-key")

    assert result.returncode != 0, f"production started with a placeholder API key: {result.stdout}"
    assert "MEMBER_API_KEY" in result.stdout + result.stderr, result.stdout + result.stderr


def test_h1_production_starts_with_a_real_api_key(tmp_path):
    result = _settings_probe(tmp_path, **_production_credentials(), MEMBER_API_KEY=secrets.token_urlsafe(24))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK env=production api_key_required=True" in result.stdout


# --------------------------------------------------------------------------- H2: trusted proxy headers
def test_h2_untrusted_proxy_headers_share_one_bucket_for_spoofed_xff(settings_env, db_session, mailbox):
    """Default (TRUSTED_PROXY_HEADERS=false): XFF is ignored, so spoofing cannot mint buckets."""
    settings_env(TRUSTED_PROXY_HEADERS="false", REGISTER_RATE_LIMIT="2", REGISTER_RATE_WINDOW_SECONDS="3600")

    with TestClient(create_app()) as api:
        csrf = WebActions(api, mailbox).csrf_token()
        statuses = [
            api.post(
                "/register",
                data=_form_payload(csrf, f"untrusted-xff-{index}@example.com"),
                headers={"X-Forwarded-For": spoofed},
                follow_redirects=False,
            ).status_code
            for index, spoofed in enumerate(["10.9.9.1", "10.9.9.2", "10.9.9.3"], start=1)
        ]

    assert statuses == [303, 303, 429], (
        f"with TRUSTED_PROXY_HEADERS=false spoofed XFF values must share one bucket, got {statuses!r}"
    )
    assert _count_members(db_session) == 2, "the throttled registration must not create a member"


def test_h2_trusted_proxy_rate_limit_uses_the_rightmost_hop(settings_env, db_session, mailbox):
    """One trusted proxy appends the peer address: only the rightmost hop may decide the bucket."""
    settings_env(TRUSTED_PROXY_HEADERS="true", REGISTER_RATE_LIMIT="2", REGISTER_RATE_WINDOW_SECONDS="3600")

    with TestClient(create_app()) as api:
        csrf = WebActions(api, mailbox).csrf_token()
        statuses = [
            api.post(
                "/register",
                data=_form_payload(csrf, f"prepended-xff-{index}@example.com"),
                headers={"X-Forwarded-For": f"{spoofed}, 10.9.9.9"},
                follow_redirects=False,
            ).status_code
            for index, spoofed in enumerate(["spoofed-a", "spoofed-b", "spoofed-c"], start=1)
        ]

    assert statuses == [303, 303, 429], (
        f"prepending fake XFF hops must not mint new rate-limit buckets, got {statuses!r}"
    )
    assert _count_members(db_session) == 2, "the throttled registration must not create a member"


def test_h2_trusted_proxy_uses_the_rightmost_xff_hop_for_ip_hash(settings_env, db_session):
    settings_env(TRUSTED_PROXY_HEADERS="true")
    salt = get_settings().ip_hash_salt

    with TestClient(create_app()) as api:
        response = api.post(
            "/register",
            data=_form_payload(_csrf(api), "rightmost-xff@example.com"),
            headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.7"},
            follow_redirects=False,
        )

    assert response.status_code == 303, response.text
    attribution = _attribution(db_session, _member(db_session, "rightmost-xff@example.com"))
    assert attribution is not None
    assert attribution.ip_hash == hash_ip("203.0.113.7", salt), (
        "with TRUSTED_PROXY_HEADERS the closest (rightmost) XFF hop must be hashed"
    )
    assert attribution.ip_hash != hash_ip("1.2.3.4", salt), "the attacker-controlled first hop must be ignored"


def test_h2_untrusted_proxy_headers_ignore_xff(client, web, db_session, app_settings):
    response = client.post(
        "/register",
        data=_form_payload(web.csrf_token(), "ignore-xff@example.com"),
        headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.7"},
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    attribution = _attribution(db_session, _member(db_session, "ignore-xff@example.com"))
    assert attribution is not None
    assert attribution.ip_hash == hash_ip("testclient", app_settings.ip_hash_salt), (
        "XFF must be ignored while TRUSTED_PROXY_HEADERS is false (the default)"
    )
    assert attribution.ip_hash != hash_ip("203.0.113.7", app_settings.ip_hash_salt)


def test_h2_hostile_xff_header_never_500_and_is_truncated(settings_env, db_session):
    """A huge XFF chain with a 200-char last hop must not 500 and must not be hashed raw."""
    settings_env(TRUSTED_PROXY_HEADERS="true")
    salt = get_settings().ip_hash_salt
    last_hop = "h" * 200
    hostile_chain = ", ".join(["spoofed"] * 5000 + [last_hop])

    with TestClient(create_app()) as api:
        response = api.post(
            "/register",
            data=_form_payload(_csrf(api), "hostile-xff@example.com"),
            headers={"X-Forwarded-For": hostile_chain},
            follow_redirects=False,
        )

    assert response.status_code != 500, response.text[:300]
    assert response.status_code == 303, response.text
    attribution = _attribution(db_session, _member(db_session, "hostile-xff@example.com"))
    assert attribution is not None
    assert re.fullmatch(r"[0-9a-f]{64}", str(attribution.ip_hash)), attribution.ip_hash
    assert attribution.ip_hash != hash_ip(last_hop, salt), "the 200-char hop must not be hashed raw"
    assert any(hash_ip(last_hop[:length], salt) == attribution.ip_hash for length in range(1, len(last_hop))), (
        "the rightmost hop must be truncated to a bounded prefix before hashing"
    )


# --------------------------------------------------------------------------- H3: register limit covers the API
def test_h3_html_register_rate_limit_blocks_the_third_post(settings_env, db_session, mailbox):
    settings_env(REGISTER_RATE_LIMIT="2", REGISTER_RATE_WINDOW_SECONDS="3600", API_RATE_LIMIT="2")

    with TestClient(create_app()) as api:
        actions = WebActions(api, mailbox)
        statuses = [
            actions.register(email=f"h3-html-{index}@example.com")[0].status_code for index in range(3)
        ]

    assert statuses == [303, 303, 429], statuses
    assert _count_members(db_session) == 2


def test_h3_api_register_rate_limit_blocks_the_third_post(settings_env, db_session):
    settings_env(REGISTER_RATE_LIMIT="2", REGISTER_RATE_WINDOW_SECONDS="3600", API_RATE_LIMIT="2")

    with TestClient(create_app()) as api:
        statuses = [
            api.post(
                API_REGISTER, json={"full_name": "H3 API", "email": f"h3-api-{index}@example.com"}
            ).status_code
            for index in range(3)
        ]

    assert statuses == [201, 201, 429], f"the api scope must throttle the API register, got {statuses!r}"
    assert _count_members(db_session) == 2


def test_h3_register_rate_limit_alone_covers_the_api_register(settings_env, db_session):
    """REGISTER_RATE_LIMIT must bound the API register too, not just the HTML form."""
    settings_env(REGISTER_RATE_LIMIT="2", REGISTER_RATE_WINDOW_SECONDS="3600", API_RATE_LIMIT="1000")

    with TestClient(create_app()) as api:
        statuses = [
            api.post(
                API_REGISTER, json={"full_name": "H3 Scope", "email": f"h3-scope-{index}@example.com"}
            ).status_code
            for index in range(3)
        ]

    assert statuses == [201, 201, 429], (
        f"REGISTER_RATE_LIMIT must also cover /api/v1/members/register, got {statuses!r}"
    )
    assert _count_members(db_session) == 2


# --------------------------------------------------------------------------- M1: hostile pagination
def test_m1_hostile_pagination_never_500_and_page_is_clamped(admin_client, db_session):
    _seed_members(db_session, 30)

    for page in ["99999999999999999999", "9223372036854775808", "-3", "0", "not-a-number"]:
        response = admin_client.get("/admin/members", params={"page": page, "per_page": 10})
        assert response.status_code == 200, f"page={page} returned {response.status_code}: {response.text[:300]}"

    for per_page in ["0", "-5", "999999"]:
        response = admin_client.get("/admin/members", params={"page": 1, "per_page": per_page})
        assert response.status_code == 200, (
            f"per_page={per_page} returned {response.status_code}: {response.text[:300]}"
        )

    from app.services.admin import MAX_PAGE  # the documented pagination clamp bound

    clamped = admin_client.get("/admin/members", params={"page": "99999999999999999999", "per_page": 10})
    assert clamped.status_code == 200
    assert "99999999999999999999" not in clamped.text, "the hostile page value must not be echoed"
    match = PAGE_INFO.search(clamped.text)
    assert match, "the pagination info did not render"
    shown, total_pages = int(match.group(1)), int(match.group(2))
    assert total_pages == 3
    assert 1 <= shown <= MAX_PAGE, f"page must be clamped to at most {MAX_PAGE}, rendered {shown}"


# --------------------------------------------------------------------------- M3: oversized chunked body
def test_m3_oversized_chunked_body_is_413_and_commits_nothing(settings_env, db_session):
    settings_env(MAX_REQUEST_BYTES="4096")
    email = "oversized-chunked@example.com"

    with TestClient(create_app()) as api:
        csrf = _csrf(api)
        boundary = "member-boundary"
        body = _multipart_body(_form_payload(csrf, email), boundary, padding=100_000)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}

        # httpx computes the headers we are about to send: a generator body is chunked.
        probe = httpx.Request("POST", "http://testserver/register", content=_chunks(body), headers=headers)
        assert "content-length" not in probe.headers, "this case must be sent chunked (no Content-Length)"
        assert probe.headers.get("transfer-encoding") == "chunked"

        response = api.post("/register", content=_chunks(body), headers=headers, follow_redirects=False)

    assert response.status_code != 500, response.text[:400]
    assert response.status_code == 413, response.text[:400]
    assert _count_members(db_session, email) == 0, "an oversized chunked body must never be committed"


def test_m3_oversized_content_length_body_is_413_and_commits_nothing(settings_env, db_session):
    settings_env(MAX_REQUEST_BYTES="4096")
    email = "oversized-content-length@example.com"

    with TestClient(create_app()) as api:
        response = api.post(
            "/register",
            data=_form_payload(_csrf(api), email, company="x" * 100_000),
            follow_redirects=False,
        )

    assert response.status_code != 500, response.text[:400]
    assert response.status_code == 413, response.text[:400]
    assert "413" in response.text
    assert _count_members(db_session, email) == 0, "an oversized body must never be committed"


# --------------------------------------------------------------------------- M4: resend rate limit
def test_m4_resend_verification_is_rate_limited_per_member(settings_env, db_session):
    settings_env(MEMBER_API_KEY="resend-limit-key")
    headers = {"X-API-Key": "resend-limit-key"}

    with TestClient(create_app()) as api:
        first = api.post(
            API_REGISTER, json={"full_name": "Resend A", "email": "resend-a@example.com"}, headers=headers
        )
        assert first.status_code == 201, first.text
        member_a = first.json()["data"]["member"]["id"]

        other = api.post(
            API_REGISTER, json={"full_name": "Resend B", "email": "resend-b@example.com"}, headers=headers
        )
        assert other.status_code == 201, other.text
        member_b = other.json()["data"]["member"]["id"]

        statuses = [
            api.post(f"/api/v1/members/{member_a}/resend-verification", headers=headers).status_code
            for _ in range(4)
        ]
        untouched = api.post(f"/api/v1/members/{member_b}/resend-verification", headers=headers)

    assert statuses[:3] == [200, 200, 200], f"the first three resends must succeed, got {statuses!r}"
    assert statuses[3] == 429, f"the fourth resend for the same member must be throttled, got {statuses!r}"
    assert untouched.status_code == 200, "a different member must not share the resend bucket"
    body = untouched.json()
    assert body["success"] is True
    assert body["data"]["verification_sent"] is True


# --------------------------------------------------------------------------- L4: no internal SMTP errors
def test_l4_internal_smtp_error_is_not_returned_to_api_callers(settings_env, db_session, closed_port):
    settings_env(
        EMAIL_MODE="smtp",
        SMTP_HOST="127.0.0.1",
        SMTP_PORT=str(closed_port),
        SMTP_TLS="false",
    )

    with TestClient(create_app()) as api:
        response = api.post(
            API_REGISTER, json={"full_name": "SMTP Failure", "email": "smtp-failure@example.com"}
        )

    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["verification_sent"] is False
    assert data["email_error"] in (None, "send_failed"), f"raw SMTP error leaked: {data['email_error']!r}"

    for marker in ("Errno", "Connection refused", "refused", "Traceback", "smtplib", "OSError", "socket"):
        assert marker not in response.text, f"internal SMTP detail {marker!r} leaked to the API caller"

    assert _count_members(db_session, "smtp-failure@example.com") == 1, "the member must still be registered"


# --------------------------------------------------------------------------- L5: /welcome and pending members
def test_l5_welcome_does_not_present_a_pending_member_as_a_member(client, web):
    response, _ = web.register(email="welcome-pending@example.com", full_name="Pending Person")
    assert response.status_code == 303, response.text

    page = client.get("/welcome", follow_redirects=True)

    assert page.status_code == 200, page.text
    assert "Bạn đã trở thành thành viên" not in page.text, (
        "a pending member must never be presented as an already-verified member"
    )
    assert "pill-verified" not in page.text, "the pending member must not be shown as verified"
    assert "xác minh" in page.text.lower(), "the page must tell the pending member to verify their email"


def test_l5_welcome_shows_the_verified_member(client, web):
    verified, email = web.register_and_verify(email="welcome-verified@example.com", full_name="Verified Person")
    assert verified.status_code == 200, verified.text

    page = client.get("/welcome", follow_redirects=True)

    assert page.status_code == 200, page.text
    assert "Verified Person" in page.text, "the verified member's name must be shown on /welcome"
    assert email in page.text


# --------------------------------------------------------------------------- M6 / L7: config hygiene
def test_m6_dead_admin_session_setting_is_gone():
    assert "admin_session_max_age_seconds" not in Settings.model_fields, (
        "ADMIN_SESSION_MAX_AGE_SECONDS is dead configuration and must be removed"
    )


def test_l7_webhook_timeout_default_is_five_seconds():
    assert Settings.model_fields["webhook_timeout_seconds"].default == 5
    assert Settings(_env_file=None).webhook_timeout_seconds == 5
