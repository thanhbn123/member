"""Security primitives: password hashing, tokens, IP hashing, CSRF, HMAC signing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from fastapi import Request

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_PREFIX = "scrypt"


# --------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    """Hash an admin password with scrypt (stdlib, no external dependency)."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN
    )
    return "$".join(
        [
            SCRYPT_PREFIX,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time scrypt verification. Never raises on malformed hashes."""
    if not password or not stored_hash:
        return False
    try:
        prefix, n, r, p, salt_b64, digest_b64 = stored_hash.split("$")
        if prefix != SCRYPT_PREFIX:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


# --------------------------------------------------------------------------- tokens
def generate_token(nbytes: int = 32) -> str:
    """Cryptographically secure, URL safe, one-time token."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Only the hash is persisted - a database leak cannot be replayed."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- IP handling
def hash_ip(ip: str | None, salt: str) -> str | None:
    """Raw IPs are never stored: SHA256(ip + salt)."""
    if not ip:
        return None
    return hashlib.sha256(f"{ip}{salt}".encode()).hexdigest()


def client_ip(request: Request, trusted_proxy_headers: bool = False) -> str | None:
    """Best-effort client IP. Proxy headers are only honoured when explicitly trusted."""
    if trusted_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    return request.client.host if request.client else None


# --------------------------------------------------------------------------- CSRF
CSRF_COOKIE_NAME = "member_csrf"
CSRF_FORM_FIELD = "csrf_token"


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_tokens_match(cookie_token: str | None, form_token: str | None) -> bool:
    """Double-submit cookie check (constant time)."""
    if not cookie_token or not form_token:
        return False
    return hmac.compare_digest(cookie_token, form_token)


# --------------------------------------------------------------------------- webhook signing
def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over '<timestamp>.<body>' so a captured payload cannot be replayed later."""
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign_payload(secret, timestamp, body), signature)


def iso_timestamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(UTC)).isoformat()
