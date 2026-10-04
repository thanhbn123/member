"""Cross-cutting ASGI middleware: request id, security headers, request size, CSRF cookie."""

from __future__ import annotations

import logging
import secrets
import uuid

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.security import CSRF_COOKIE_NAME, generate_csrf_token

logger = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
# Endpoints that are called by machines, not browsers: CSRF does not apply (documented).
CSRF_EXEMPT_PREFIXES = ("/api/",)


class RequestContextMiddleware:
    """Attach a request id + timing to every request and echo the id back."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        nonce = secrets.token_urlsafe(16)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["csp_nonce"] = nonce
        request = Request(scope, receive)
        request.state.request_id = request_id
        request.state.csp_nonce = nonce

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
            await send(message)

        await self.app(scope, receive, send_wrapper)


class SecurityHeadersMiddleware:
    """Defence-in-depth headers. CSP is built from configuration (pixel/GA4 aware)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and settings.security_headers_enabled:
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
                nonce = (scope.get("state") or {}).get("csp_nonce")
                headers.setdefault("Content-Security-Policy", settings.csp_policy(nonce))
                if settings.secure_cookies:
                    headers.setdefault(
                        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)


class MaxBodySizeMiddleware:
    """Reject oversized requests before they reach a handler (cheap DoS guard)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        limit = settings.max_request_bytes
        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError:
                length = 0
            if length > limit:
                await self._reject(scope, send, limit)
                return

        received = 0
        too_large = False

        async def receive_wrapper() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    too_large = True
            return message

        async def send_wrapper(message: Message) -> None:
            if too_large:
                raise _BodyTooLarge
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except _BodyTooLarge:
            await self._reject(scope, send, limit)

    @staticmethod
    async def _reject(scope: Scope, send: Send, limit: int) -> None:
        import json  # local imports keep the middleware import-light

        from app.schemas import fail

        if scope.get("path", "").startswith("/api/"):
            payload = json.dumps(fail("payload_too_large", f"Request body exceeds {limit} bytes")).encode()
            content_type = b"application/json"
        else:
            payload = (
                '<!doctype html><html lang="vi"><head><meta charset="utf-8"><title>413</title></head>'
                '<body style="font-family:system-ui;padding:2rem"><h1>413 - Yêu cầu quá lớn</h1>'
                f"<p>Kích thước tối đa cho phép: {int(limit)} bytes.</p>"
                '<p><a href="/register">Quay lại</a></p></body></html>'
            ).encode()
            content_type = b"text/html; charset=utf-8"
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", content_type),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


class _BodyTooLarge(Exception):
    pass


class CSRFCookieMiddleware:
    """Ensure a double-submit CSRF cookie exists and expose it to templates."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        if not settings.csrf_enabled:
            scope.setdefault("state", {})["csrf_token"] = ""
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        token = request.cookies.get(CSRF_COOKIE_NAME)
        issued = False
        if not token:
            token = generate_csrf_token()
            issued = True
        scope.setdefault("state", {})["csrf_token"] = token

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and issued:
                MutableHeaders(scope=message).append(
                    "set-cookie",
                    (
                        f"{CSRF_COOKIE_NAME}={token}; Path=/; SameSite=Lax; HttpOnly; "
                        f"Max-Age={settings.session_max_age_seconds}"
                        + ("; Secure" if settings.secure_cookies else "")
                    ),
                )
            await send(message)

        await self.app(scope, receive, send_wrapper)
