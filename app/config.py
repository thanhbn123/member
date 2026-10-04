"""Application configuration.

Everything that differs between customers / deployments is configuration, never code:
brand name, logo, colors, public URL, database, SMTP, Meta pixel, GA4, webhook target...

See .env.example for the full list.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET_KEY = "dev-insecure-secret-key-change-me"
DEV_IP_SALT = "dev-insecure-ip-salt-change-me"


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").replace(";", ",").split(",") if item.strip()]


class Settings(BaseSettings):
    """Environment driven settings (12-factor). All values overridable via env vars."""

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- application ----------
    app_name: str = "MEMBER"
    app_env: Literal["local", "staging", "production"] = "local"
    debug: bool = False
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"

    # ---------- branding (no customer name is ever hard-coded) ----------
    brand_name: str = "MEMBER"
    brand_logo_url: str = ""
    brand_primary_color: str = "#2563eb"
    brand_support_email: str = ""
    brand_tagline: str = "Đăng ký thành viên"

    # ---------- contact details (landing footer; each one is hidden while empty) ----------
    brand_phone: str = ""
    brand_address: str = ""
    brand_facebook_url: str = ""  # http(s) only - anything else is ignored
    brand_zalo_url: str = ""  # http(s) only - anything else is ignored

    # ---------- landing page (GET /) - every value is optional ----------
    landing_hero_title: str = ""  # empty => BRAND_TAGLINE
    landing_hero_subtitle: str = ""
    landing_hero_image_url: str = ""  # http(s) only; the text layout works without it
    # Up to 6 "icon|title|description" items joined by ";;" (see app/routers/public.py).
    landing_benefits: str = ""
    landing_cta_text: str = "Đăng ký ngay"
    landing_show_form: bool = True  # False => the hero links to /register instead

    # ---------- database ----------
    database_url: str = "sqlite:///./member.db"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ---------- security ----------
    secret_key: str = DEV_SECRET_KEY
    ip_hash_salt: str = DEV_IP_SALT
    trusted_proxy_headers: bool = False
    security_headers_enabled: bool = True
    max_request_bytes: int = 256 * 1024  # 256 KiB
    session_cookie_name: str = "member_session"
    session_max_age_seconds: int = 8 * 3600
    session_https_only: bool | None = None  # None => True in production
    session_same_site: Literal["lax", "strict", "none"] = "lax"
    csrf_enabled: bool = True

    # ---------- admin ----------
    admin_email: str = ""
    admin_password_hash: str = ""

    # ---------- email verification ----------
    verification_token_ttl_hours: int = 48
    email_mode: Literal["console", "smtp"] = "console"

    # ---------- smtp ----------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_from_name: str = ""
    smtp_tls: bool = True
    smtp_timeout_seconds: int = 15

    # ---------- rate limiting (in-process; use a shared store for multi-worker) ----------
    register_rate_limit: int = 10
    register_rate_window_seconds: int = 3600
    api_rate_limit: int = 60
    api_rate_window_seconds: int = 60
    login_rate_limit: int = 10
    login_rate_window_seconds: int = 900

    # ---------- public API ----------
    member_api_key: str = ""  # optional: when set, /api/v1 member endpoints require X-API-Key
    cors_allow_origins: str = ""
    api_docs_enabled: bool = True  # set false in production to hide /docs and /openapi.json

    # ---------- integrations: meta conversions api (disabled unless configured) ----------
    meta_pixel_id: str = ""
    meta_access_token: str = ""
    meta_api_version: str = "v21.0"
    meta_test_event_code: str = ""
    meta_timeout_seconds: int = 10

    # ---------- integrations: verified-member webhook (disabled unless configured) ----------
    member_verified_webhook_url: str = ""
    member_verified_webhook_secret: str = ""
    # 5s per attempt keeps a verification request responsive (3 attempts + backoff worst case).
    webhook_timeout_seconds: int = 5
    webhook_max_attempts: int = 3
    webhook_backoff_seconds: float = 1.0

    # ---------- client-side analytics ids (optional, empty => not rendered) ----------
    ga4_measurement_id: str = ""

    # ---------- CSP extras (space separated, merged into the default policy) ----------
    csp_extra_script_src: str = ""
    csp_extra_style_src: str = ""
    csp_extra_img_src: str = ""
    csp_extra_connect_src: str = ""
    csp_extra_frame_src: str = ""

    # ------------------------------------------------------------------
    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return (value or "").rstrip("/")

    @field_validator("brand_primary_color")
    @classmethod
    def _validate_color(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            return "#2563eb"
        if not value.startswith("#") or len(value) not in (4, 7):
            raise ValueError("BRAND_PRIMARY_COLOR must be a hex color such as #2563eb")
        return value

    @field_validator("landing_hero_image_url", "brand_facebook_url", "brand_zalo_url")
    @classmethod
    def _http_url_only(cls, value: str) -> str:
        """Keep http(s) URLs, blank everything else.

        These values end up in ``src``/``href`` attributes, so ``javascript:alert(1)``,
        ``data:...`` or a scheme-less string is dropped at load time (the router applies
        the same rule again before rendering, see ``app.routers.public.safe_http_url``).
        """
        value = (value or "").strip()
        if not value:
            return ""
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            return ""
        return value

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        """Refuse to boot a production deployment that would leak member data.

        Every message names the variable and says what to set, so an operator can fix
        the deployment from the traceback alone. Non-production environments are never
        blocked: local/staging may run sqlite, console email and an open API on purpose.
        """
        if self.app_env == "production":
            problems: list[str] = []

            def _placeholder(value: str) -> bool:
                return not value or value.lower().startswith(("change-me", "changeme", "dev-", "test-"))

            if self.secret_key in ("", DEV_SECRET_KEY) or _placeholder(self.secret_key) or len(self.secret_key) < 32:
                problems.append(
                    "SECRET_KEY must be a strong random value of at least 32 characters in production "
                    "(set SECRET_KEY, e.g. `python -c \"import secrets; print(secrets.token_urlsafe(48))\"`)"
                )
            if self.ip_hash_salt in ("", DEV_IP_SALT) or _placeholder(self.ip_hash_salt) or len(self.ip_hash_salt) < 16:
                problems.append(
                    "IP_HASH_SALT must be a random value of at least 16 characters in production "
                    "(raw IPs must never be stored) - set IP_HASH_SALT"
                )
            if _placeholder(self.member_api_key) or len(self.member_api_key) < 16:
                problems.append(
                    "MEMBER_API_KEY must be a random value of at least 16 characters in production "
                    "(it protects member PII on /api/v1) - set MEMBER_API_KEY and send it as the "
                    "X-API-Key header"
                )
            if not self.public_base_url:
                problems.append(
                    "PUBLIC_BASE_URL must be set to the public https:// origin of this deployment in "
                    "production (verification links and session cookies are built from it)"
                )
            elif not self.public_base_url.startswith("https://"):
                problems.append(
                    "PUBLIC_BASE_URL must start with https:// in production - set PUBLIC_BASE_URL to the "
                    "public https:// origin of this deployment"
                )
            if self.database_url.startswith("sqlite"):
                problems.append(
                    "DATABASE_URL must point at PostgreSQL in production - set DATABASE_URL to a "
                    "postgresql+psycopg:// DSN"
                )
            if self.email_mode != "smtp":
                problems.append(
                    'EMAIL_MODE must be "smtp" in production: the console backend prints raw verification '
                    'links (account take-over tokens) to stdout - set EMAIL_MODE=smtp'
                )
            elif not self.smtp_host:
                problems.append("SMTP_HOST is required when EMAIL_MODE=smtp - set SMTP_HOST to your mail relay")
            if self.admin_configured:
                from app.security import parse_password_hash

                if _placeholder(self.admin_password_hash):
                    problems.append(
                        "ADMIN_PASSWORD_HASH is still a placeholder - generate one with "
                        "`python -m app.cli hash-password`"
                    )
                elif parse_password_hash(self.admin_password_hash) is None:
                    problems.append(
                        "ADMIN_PASSWORD_HASH is malformed (expected "
                        "'scrypt:<n>:<r>:<p>:<salt>:<digest>') - regenerate it with "
                        "`python -m app.cli hash-password` (a bare '$' in a .env file is "
                        "usually eaten by Docker Compose or by `source`)"
                    )
            if problems:
                raise ValueError("Invalid production configuration: " + "; ".join(problems))
        return self

    # ---------- derived helpers ----------
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def secure_cookies(self) -> bool:
        if self.session_https_only is not None:
            return self.session_https_only
        return self.is_production or self.public_base_url.startswith("https://")

    @property
    def cors_origins(self) -> list[str]:
        return _split_csv(self.cors_allow_origins)

    @property
    def meta_enabled(self) -> bool:
        """Meta Conversions API is opt-in: both pixel id and access token are required."""
        return bool(self.meta_pixel_id and self.meta_access_token)

    @property
    def webhook_enabled(self) -> bool:
        """Verified-member webhook is opt-in: both url and secret are required."""
        return bool(self.member_verified_webhook_url and self.member_verified_webhook_secret)

    @property
    def api_key_required(self) -> bool:
        return bool(self.member_api_key)

    @property
    def admin_configured(self) -> bool:
        return bool(self.admin_email and self.admin_password_hash)

    @property
    def email_ready(self) -> bool:
        return self.email_mode == "console" or bool(self.smtp_host)

    def csp_directives(self) -> dict[str, list[str]]:
        script = ["'self'"]
        style = ["'self'"]
        img = ["'self'", "data:"]
        connect = ["'self'"]
        frame = ["'self'"]
        if self.ga4_measurement_id:
            script += ["https://www.googletagmanager.com"]
            connect += ["https://www.google-analytics.com", "https://region1.google-analytics.com"]
            img += ["https://www.google-analytics.com"]
        if self.meta_pixel_id:
            script += ["https://connect.facebook.net"]
            connect += ["https://connect.facebook.net", "https://www.facebook.com"]
            img += ["https://www.facebook.com", "https://connect.facebook.net"]
        script += _split_csv(self.csp_extra_script_src)
        style += _split_csv(self.csp_extra_style_src)
        img += _split_csv(self.csp_extra_img_src)
        connect += _split_csv(self.csp_extra_connect_src)
        frame += _split_csv(self.csp_extra_frame_src)
        return {"script-src": script, "style-src": style, "img-src": img, "connect-src": connect,
                "frame-src": frame}

    def csp_policy(self, nonce: str | None = None) -> str:
        """Build the Content-Security-Policy.

        ``nonce`` (per request) is what allows the tiny inline brand-colour style and
        the optional GA4 / Meta Pixel bootstrap without ever enabling ``unsafe-inline``.
        """
        directives = {
            "default-src": ["'self'"],
            "base-uri": ["'self'"],
            "form-action": ["'self'"],
            "object-src": ["'none'"],
            "frame-ancestors": ["'none'"],
            **self.csp_directives(),
        }
        if nonce:
            directives["script-src"] = [*directives["script-src"], f"'nonce-{nonce}'"]
            directives["style-src"] = [*directives["style-src"], f"'nonce-{nonce}'"]
        return "; ".join(f"{name} {' '.join(values)}" for name, values in directives.items())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that need to reload configuration from a changed environment."""
    get_settings.cache_clear()
