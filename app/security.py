"""Security primitives: password hashing, tokens, IP hashing, CSRF, HMAC signing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
from datetime import UTC, datetime

from fastapi import Request

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_PREFIX = "scrypt"

# Pre-generated scrypt hash of a random throwaway password. Login always verifies
# against *some* hash (this one when the email does not match) so an unknown email
# cannot be distinguished from a known one by response time - see app/routers/admin.py.
DUMMY_PASSWORD_HASH = (
    "scrypt$16384$8$1$vkbauDcvf95X6r0CMC8Ulw==$5qHpRVZWmKu8bv7JdZGC1j5MUM7yZ92cZhMs2BiJFl0="
)


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
#
# TRUSTED_PROXY_HEADERS=true means exactly ONE trusted reverse proxy sits in front of
# this app and APPENDS the peer address to X-Forwarded-For; never enable it on a
# directly exposed app, otherwise the client controls its own rate-limit bucket and
# its own ip_hash.
MAX_CLIENT_IP_LENGTH = 64  # a hostile header must not bloat a rate-limit key or hash
MAX_FORWARDED_HOPS = 8  # a 10 000-entry XFF header must not cost more than 8 checks

_PRIVATE_HOP_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


def _clean_ip(value: str | None) -> str | None:
    """Trim a proxy-supplied address and cap its length (never trust its size)."""
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    return candidate[:MAX_CLIENT_IP_LENGTH]


def _plausible_client_address(value: str) -> bool:
    """Whether a *lone* XFF hop could be the public peer a trusted proxy appended."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not any(
        address.version == network.version and address in network
        for network in _PRIVATE_HOP_NETWORKS
    )


def hash_ip(ip: str | None, salt: str) -> str | None:
    """Raw IPs are never stored: SHA256(ip + salt)."""
    if not ip:
        return None
    return hashlib.sha256(f"{ip}{salt}".encode()).hexdigest()


def client_ip(request: Request, trusted_proxy_headers: bool = False) -> str | None:
    """Best-effort client IP. Proxy headers are only honoured when explicitly trusted.

    Contract for ``trusted_proxy_headers=True``: **exactly one** trusted reverse proxy,
    which *appends* the peer address it saw (nginx: ``$proxy_add_x_forwarded_for``).
    Only the **rightmost** hop can be trusted; everything to its left was supplied by
    the client and is ignored. Empty/whitespace hops are skipped and at most the last
    ``MAX_FORWARDED_HOPS`` are inspected, so a 10 000-entry header is cheap.

    A chain with a **single** hop is ambiguous: it is either the address that trusted
    proxy appended (client sent no XFF) or a value the caller typed itself (proxy
    bypass). It is therefore used only when it is a plausible public peer address; a
    private/loopback/link-local value proves nothing was appended, so ``X-Real-IP`` is
    tried and the request is otherwise left unidentified (``None``). ``request.client.host``
    is deliberately *not* used in that case: ASGI servers (uvicorn's
    ``ProxyHeadersMiddleware``) rewrite ``scope["client"]`` from this very header when the
    transport peer is a trusted host, so it is not an independent source - reusing it
    after rejecting the hop would hand the attacker their own rate-limit bucket again.
    When no ``X-Forwarded-For`` is present at all, ``X-Real-IP`` and then the transport
    peer are used, as documented.

    The chosen value is always truncated to ``MAX_CLIENT_IP_LENGTH``.
    """
    if trusted_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            hops = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
            hops = hops[-MAX_FORWARDED_HOPS:]
            if hops:
                if len(hops) > 1 or _plausible_client_address(hops[-1]):
                    return hops[-1][:MAX_CLIENT_IP_LENGTH]
                return _clean_ip(request.headers.get("x-real-ip"))
        real_ip = _clean_ip(request.headers.get("x-real-ip"))
        if real_ip:
            return real_ip
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
