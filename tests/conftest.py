"""Shared pytest fixtures.

Environment is configured BEFORE the application is imported, so the whole app
(settings, engine, routers) is built against a throwaway SQLite database.
"""

from __future__ import annotations

import os
import re
import socket
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

os.environ.update(
    {
        "APP_ENV": "local",
        "APP_NAME": "MEMBER",
        "BRAND_NAME": "MEMBER",
        "PUBLIC_BASE_URL": "http://testserver",
        "SECRET_KEY": "test-secret-key-not-for-production",
        "IP_HASH_SALT": "test-ip-salt",
        "EMAIL_MODE": "console",
        "CSRF_ENABLED": "true",
        "SECURITY_HEADERS_ENABLED": "true",
        "ADMIN_EMAIL": "admin@example.com",
        "REGISTER_RATE_LIMIT": "1000",
        "API_RATE_LIMIT": "1000",
        "LOGIN_RATE_LIMIT": "1000",
        "MEMBER_API_KEY": "",
        "MEMBER_VERIFIED_WEBHOOK_URL": "",
        "MEMBER_VERIFIED_WEBHOOK_SECRET": "",
        "META_PIXEL_ID": "",
        "META_ACCESS_TOKEN": "",
    }
)

ADMIN_PASSWORD = "admin-pass-123"

from app.config import get_settings, reset_settings_cache  # noqa: E402
from app.db import dispose_engine, get_session_factory  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base  # noqa: E402
from app.security import hash_password  # noqa: E402

os.environ.setdefault("ADMIN_PASSWORD_HASH", hash_password(ADMIN_PASSWORD))

URL_RE = re.compile(r"https?://\S*/verify-email\?token=[A-Za-z0-9_\-%]+")
EMAIL_HEADER_RE = re.compile(r"\[EMAIL\]\[console\]\s+to=(?P<to>\S+)\s+subject=(?P<subject>.*)")
TOKEN_RE = re.compile(r"token=([A-Za-z0-9_\-%]+)")


@dataclass
class ConsoleMailbox:
    """Parses the console email backend output captured from stdout."""

    capsys: object
    messages: list[dict] = field(default_factory=list)

    def read(self) -> list[dict]:
        captured = self.capsys.readouterr()  # type: ignore[attr-defined]
        output = captured.out
        current: dict | None = None
        for line in output.splitlines():
            header = EMAIL_HEADER_RE.search(line)
            if header:
                current = {"to": header.group("to"), "subject": header.group("subject"), "body": ""}
                self.messages.append(current)
                continue
            if current is not None:
                current["body"] += line + "\n"
        for message in self.messages:
            match = URL_RE.search(message["body"])
            message["url"] = match.group(0) if match else None
            token_match = TOKEN_RE.search(message["url"] or "")
            message["token"] = token_match.group(1) if token_match else None
        return self.messages

    def latest(self) -> dict:
        messages = self.read()
        assert messages, "no console email was sent"
        return messages[-1]

    def latest_token(self) -> str:
        token = self.latest().get("token")
        assert token, "no verification token found in the console email"
        return token


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app import ratelimit

    ratelimit.reset()
    yield
    ratelimit.reset()


def _truncate_all(engine) -> None:
    """Wipe data between tests when a *shared* database is used (TEST_DATABASE_URL)."""
    from sqlalchemy import text

    tables = ["member_events", "email_verification_tokens", "member_attribution", "members"]
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
        else:
            for table in tables:
                connection.execute(text(f"DELETE FROM {table}"))


@pytest.fixture
def settings_env(monkeypatch, tmp_path):
    """Set env vars + a fresh database, and restore everything afterwards.

    Defaults to a throwaway SQLite file per test. Export ``TEST_DATABASE_URL``
    (for example a PostgreSQL DSN) to run the exact same suite against the
    production database engine - rows are truncated between tests.
    """
    shared_url = os.environ.get("TEST_DATABASE_URL", "").strip()

    def apply(**env: str):
        for key, value in env.items():
            monkeypatch.setenv(key.upper(), str(value))
        reset_settings_cache()
        dispose_engine()

    monkeypatch.setenv("DATABASE_URL", shared_url or f"sqlite:///{tmp_path / 'test.db'}")
    reset_settings_cache()
    dispose_engine()
    yield apply
    if shared_url:
        from app.db import get_engine

        _truncate_all(get_engine())
    reset_settings_cache()
    dispose_engine()


@pytest.fixture
def database(settings_env):
    """Create the schema from the models (migrations are verified separately)."""
    from app.db import get_engine

    engine = get_engine()
    Base.metadata.create_all(engine)
    if os.environ.get("TEST_DATABASE_URL", "").strip():
        _truncate_all(engine)
    yield engine


@pytest.fixture
def db_session(database):
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def app(database):
    return create_app()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


class WebActions:
    """Small helper around the browser flow (CSRF handling + form posts)."""

    def __init__(self, client, mailbox: ConsoleMailbox) -> None:
        self.client = client
        self.mailbox = mailbox

    def csrf_token(self, path: str = "/register") -> str:
        response = self.client.get(path)
        assert response.status_code == 200, response.text
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
        assert match, f"no csrf_token field rendered on {path}"
        return match.group(1)

    def register(self, email=None, **overrides):
        email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
        payload = {
            "full_name": "Nguyễn Văn A",
            "email": email,
            "phone": "0901234567",
            "company": "Công ty TNHH ABC",
            "csrf_token": self.csrf_token(),
        }
        payload.update(overrides)
        response = self.client.post("/register", data=payload, follow_redirects=False)
        return response, email

    def register_and_verify(self, email=None, **overrides):
        response, email = self.register(email=email, **overrides)
        assert response.status_code == 303, response.text
        token = self.mailbox.latest_token()
        verified = self.client.get(f"/verify-email?token={token}", follow_redirects=False)
        return verified, email


@pytest.fixture
def mailbox(capsys) -> ConsoleMailbox:
    return ConsoleMailbox(capsys)


@pytest.fixture
def web(client, mailbox) -> WebActions:
    return WebActions(client, mailbox)


@pytest.fixture
def admin_client(client, web):
    """A client that is logged into the admin UI."""
    token = web.csrf_token("/admin/login")
    response = client.post(
        "/admin/login",
        data={"email": os.environ["ADMIN_EMAIL"], "password": ADMIN_PASSWORD, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return client


# --------------------------------------------------------------------------- local HTTP server (webhook tests)
class RecordingHandler(BaseHTTPRequestHandler):
    server_version = "RecordingHTTP/1.0"

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        record = {
            "path": self.path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": body,
        }
        assert isinstance(self.server, RecordingServer)
        self.server.requests.append(record)
        status = self.server.status_code
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # silence the test output
        return


class RecordingServer(HTTPServer):
    def __init__(self, *args, status_code: int = 200, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests: list[dict] = []
        self.status_code = status_code


@pytest.fixture
def webhook_server():
    """A real local HTTP server that records webhook deliveries."""
    servers: list[RecordingServer] = []

    def start(status_code: int = 200) -> str:
        server = RecordingServer(("127.0.0.1", 0), RecordingHandler, status_code=status_code)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}/hook"

    yield start

    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def closed_port() -> int:
    """A port nothing is listening on (used to force connection errors)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def app_settings():
    return get_settings()
