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
    """Reject oversized requests *before* the app sees them (cheap DoS guard).

    The ``Content-Length`` header is the fast path, but a chunked upload has no such
    header: detecting the overshoot while the handler streams the body would let the
    request be processed (and committed) before the 413 is sent. The body is therefore
    buffered here up to ``max_request_bytes``; an oversized body is answered immediately
    without ever calling the app, and an accepted body is replayed downstream exactly
    once through a custom ``receive``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = get_settings().max_request_bytes
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

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return  # the client went away: nothing to answer, nothing to buffer
            if message["type"] != "http.request":
                continue
            body += message.get("body", b"")
            if len(body) > limit:
                await self._reject(scope, send, limit)
                return
            if not message.get("more_body", False):
                break

        buffered = bytes(body)
        delivered = False

        async def replay_receive() -> Message:
            """Hand the buffered body to the app once, then pass through to the server.

            After that the underlying channel only carries ``http.disconnect``, which
            ``StreamingResponse`` (the admin CSV export) listens to while it streams:
            inventing a disconnect here would cancel the response body.
            """
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": buffered, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

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


class CSRFCookieMiddleware:
    """Ensure a double-submit CSRF cookie exists and expose it to templates.

    CSRF exemption is per-endpoint, not per-prefix: the ``/api/`` routers are exempt
    because they never depend on ``app.deps.require_csrf`` (they authenticate with
    X-API-Key instead), while every browser POST route does.
    """

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
