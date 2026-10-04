"""Tests for :mod:`app.email.service`.

Self-contained on purpose: this module builds its own SQLite database and settings
environment (``monkeypatch.setenv`` + ``reset_settings_cache`` + ``dispose_engine``)
and does not rely on any shared fixture from ``tests/conftest.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app import db as app_db
from app.config import get_settings, reset_settings_cache
from app.email import service as email_service
from app.email.service import EmailResult, send_email, send_verification_email
from app.models import Base, Member

BRAND = "Acme Test Brand"
TTL_HOURS = 12
VERIFY_URL = "https://member.acme.test/verify-email?token=raw-token-abc123"


# --------------------------------------------------------------------------- fixtures
@pytest.fixture()
def settings_env(monkeypatch, tmp_path):
    """Isolated settings + SQLite database for a single test."""
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("BRAND_NAME", BRAND)
    monkeypatch.setenv("BRAND_SUPPORT_EMAIL", "support@acme.test")
    monkeypatch.setenv("BRAND_TAGLINE", "Tham gia cộng đồng")
    monkeypatch.setenv("EMAIL_MODE", "console")
    monkeypatch.setenv("VERIFICATION_TOKEN_TTL_HOURS", str(TTL_HOURS))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://member.acme.test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'email-tests.db'}")
    reset_settings_cache()
    app_db.dispose_engine()
    try:
        yield get_settings()
    finally:
        reset_settings_cache()
        app_db.dispose_engine()


@pytest.fixture()
def member(settings_env):
    """A persisted member - email sending touches a real ORM row."""
    engine = app_db.get_engine()
    Base.metadata.create_all(engine)
    session = app_db.get_session_factory()()
    try:
        row = Member(
            id=uuid.uuid4(),
            full_name="Nguyễn Văn A",
            email="a@example.com",
            phone="+84912345678",
            company="Acme",
            status="pending",
            created_at=datetime.now(UTC),
        )
        session.add(row)
        session.commit()
        yield row
    finally:
        session.close()


class FakeSMTP:
    """Minimal stand-in for :class:`smtplib.SMTP` (context manager protocol included)."""

    instances: list[FakeSMTP] = []
    fail_with: Exception | None = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_calls = 0
        self.login_args: tuple[str, str] | None = None
        self.message = None
        self.closed = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False

    def starttls(self):
        self.starttls_calls += 1

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, message, *args, **kwargs):
        if FakeSMTP.fail_with is not None:
            raise FakeSMTP.fail_with
        self.message = message


@pytest.fixture()
def fake_smtp(monkeypatch):
    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.instances = []
    FakeSMTP.fail_with = None
    return FakeSMTP


def _configure_smtp(monkeypatch, *, tls: bool = True) -> None:
    monkeypatch.setenv("EMAIL_MODE", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.acme.test")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_USER", "mailer")
    monkeypatch.setenv("SMTP_PASSWORD", "s3cr3t")
    monkeypatch.setenv("SMTP_FROM", "noreply@acme.test")
    monkeypatch.setenv("SMTP_FROM_NAME", "Acme Mailer")
    monkeypatch.setenv("SMTP_TLS", "true" if tls else "false")
    monkeypatch.setenv("SMTP_TIMEOUT_SECONDS", "7")
    reset_settings_cache()


# --------------------------------------------------------------------------- console mode
def test_verification_email_console_block_contains_url_and_brand(capsys, member):
    result = send_verification_email(member, VERIFY_URL)

    assert result == EmailResult(True, "console")
    out = capsys.readouterr().out
    lines = out.splitlines()
    header = next(line for line in lines if line.startswith("[EMAIL][console]"))
    assert header.startswith(f"[EMAIL][console] to={member.email} subject=")
    assert BRAND in header
    # the raw URL is greppable on its own line
    assert VERIFY_URL in lines
    assert out.count(VERIFY_URL) == 1
    assert f"{TTL_HOURS} giờ" in out
    assert "support@acme.test" in out


def test_console_backend_prints_body_and_returns_success(capsys, settings_env):
    result = send_email("b@example.com", "Hello subject", "first line\nsecond line")

    assert result == EmailResult(True, "console")
    out = capsys.readouterr().out
    assert "[EMAIL][console] to=b@example.com subject=Hello subject" in out
    assert "first line\nsecond line" in out


def test_verification_email_uses_settings_not_hard_coded_brand(monkeypatch, capsys, member):
    monkeypatch.setenv("BRAND_NAME", "Another Brand")
    monkeypatch.setenv("VERIFICATION_TOKEN_TTL_HOURS", "3")
    reset_settings_cache()

    send_verification_email(member, VERIFY_URL)

    out = capsys.readouterr().out
    assert "Another Brand" in out
    assert BRAND not in out
    assert "3 giờ" in out


# --------------------------------------------------------------------------- smtp mode
def test_smtp_without_host_returns_error_result(monkeypatch, capsys, member):
    monkeypatch.setenv("EMAIL_MODE", "smtp")
    monkeypatch.setenv("SMTP_HOST", "")
    reset_settings_cache()

    result = send_verification_email(member, VERIFY_URL)

    assert result == EmailResult(False, "smtp", error="SMTP_HOST is not configured")
    assert capsys.readouterr().out == ""


def test_smtp_success_builds_message_with_html_alternative(monkeypatch, fake_smtp, member):
    _configure_smtp(monkeypatch)

    result = send_verification_email(member, VERIFY_URL)

    assert result == EmailResult(True, "smtp")
    assert len(fake_smtp.instances) == 1
    smtp = fake_smtp.instances[0]
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.acme.test", 2525, 7)
    assert smtp.starttls_calls == 1
    assert smtp.login_args == ("mailer", "s3cr3t")
    assert smtp.closed is True
    assert smtp.message is not None

    message = smtp.message
    assert message["To"] == member.email
    assert "Acme Mailer" in message["From"]
    assert "noreply@acme.test" in message["From"]
    assert BRAND in message["Subject"]
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert VERIFY_URL in plain
    assert VERIFY_URL in html
    assert VERIFY_URL in plain.splitlines()


def test_smtp_without_tls_skips_starttls(monkeypatch, fake_smtp, member):
    _configure_smtp(monkeypatch, tls=False)

    result = send_verification_email(member, VERIFY_URL)

    assert result == EmailResult(True, "smtp")
    assert fake_smtp.instances[0].starttls_calls == 0


def test_smtp_failure_is_returned_not_raised(monkeypatch, fake_smtp, member):
    _configure_smtp(monkeypatch)
    fake_smtp.fail_with = RuntimeError("smtp boom")

    result = send_verification_email(member, VERIFY_URL)

    assert result.sent is False
    assert result.backend == "smtp"
    assert result.error is not None
    assert "smtp boom" in result.error
    assert fake_smtp.instances[0].closed is True


def test_known_settings_still_load(settings_env):
    settings = get_settings()
    assert settings.email_mode == "console"
    assert settings.brand_name == BRAND
    assert settings.verification_token_ttl_hours == TTL_HOURS


def test_email_result_carries_the_provider_acceptance_line(monkeypatch):
    """The audit trail must be able to quote Gmail's own 250 ... queue id for a delivery."""
    from app.email import service as email_service
    from app.email.service import EmailResult

    assert EmailResult(True, "smtp").detail is None  # field is optional / backwards compatible

    sent: list[dict] = []

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            return (250, b"ok")

        def starttls(self):
            return (220, b"go ahead")

        def login(self, user, password):
            return (235, b"ok")

        def send_message(self, message):
            # Mirrors smtplib: send_message -> sendmail -> data(). A fully accepted message
            # returns {} and the provider's acceptance line only exists on the DATA reply.
            sent.append({"to": message["To"], "subject": message["Subject"]})
            self.data(message.as_bytes())
            return {}

        def data(self, msg):
            return 250, "2.0.0 OK  1730000000 abc123-gsmtp - gsmtp"

        def quit(self):
            return 221, b"bye"

    for key, value in {
        "EMAIL_MODE": "smtp",
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": "587",
        "SMTP_TLS": "true",
        "SMTP_USER": "owner@example.com",
        "SMTP_PASSWORD": "app-password-value",
        "SMTP_FROM": "owner@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    from app.config import reset_settings_cache

    reset_settings_cache()
    original = email_service.smtplib.SMTP
    email_service.smtplib.SMTP = FakeSMTP
    try:
        result = email_service.send_email("recipient@example.com", "Subject", "body")
    finally:
        email_service.smtplib.SMTP = original

    assert result.sent is True
    assert result.detail and "250 2.0.0 OK" in result.detail and "gsmtp" in result.detail
    assert sent and sent[0]["to"] == "recipient@example.com"
