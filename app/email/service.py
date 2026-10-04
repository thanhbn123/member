"""Outbound email delivery.

Two backends, selected by ``EMAIL_MODE``:

* ``console`` - print a greppable block to stdout (local development, tests).
* ``smtp``    - real delivery through :mod:`smtplib`.

Sending email must never break a registration or a verification, so every entry
point in this module returns an :class:`EmailResult` instead of raising.
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from typing import TYPE_CHECKING

from app.config import Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models import Member

logger = logging.getLogger(__name__)

CONSOLE_BACKEND = "console"
SMTP_BACKEND = "smtp"
SMTP_NOT_CONFIGURED = "SMTP_HOST is not configured"


@dataclass(slots=True)
class EmailResult:
    """Outcome of a send attempt (never an exception)."""

    sent: bool
    backend: str
    error: str | None = None


# --------------------------------------------------------------------------- public API
def send_email(to: str, subject: str, text_body: str, html_body: str | None = None) -> EmailResult:
    """Send a plain text (optionally HTML) email. Never raises."""
    settings = get_settings()

    if settings.email_mode == "console":
        _print_console(to, subject, text_body)
        return EmailResult(True, CONSOLE_BACKEND)

    if not settings.smtp_host:
        logger.warning("EMAIL_MODE=smtp but SMTP_HOST is empty - email to %s was not sent", to)
        return EmailResult(False, SMTP_BACKEND, error=SMTP_NOT_CONFIGURED)

    return _send_smtp(settings, to, subject, text_body, html_body)


def send_verification_email(member: Member, url: str) -> EmailResult:
    """Send the double opt-in verification link for ``member``."""
    settings = get_settings()
    subject, text_body, html_body = _verification_content(settings, member, url)
    return send_email(member.email, subject, text_body, html_body)


# --------------------------------------------------------------------------- rendering
def _verification_content(settings: Settings, member: Member, url: str) -> tuple[str, str, str]:
    brand = settings.brand_name or settings.app_name
    ttl_hours = settings.verification_token_ttl_hours
    recipient = (member.full_name or "").strip() or member.email
    greeting = f"Xin chào {recipient},"

    subject = f"Xác nhận đăng ký thành viên {brand}"

    lines = [
        greeting,
        "",
        f"Cảm ơn bạn đã đăng ký thành viên {brand}.",
        "Vui lòng xác nhận địa chỉ email của bạn bằng liên kết dưới đây:",
        "",
        url,  # raw URL on its own line - greppable in console mode
        "",
        f"Liên kết có hiệu lực trong {ttl_hours} giờ kể từ khi email này được gửi.",
        "Nếu bạn không thực hiện đăng ký này, hãy bỏ qua email này.",
    ]
    if settings.brand_support_email:
        lines += ["", f"Cần hỗ trợ? Liên hệ {settings.brand_support_email}."]
    lines += ["", "Trân trọng,", brand]
    text_body = "\n".join(lines) + "\n"

    html_body = _verification_html(
        brand=brand,
        primary_color=settings.brand_primary_color,
        greeting=greeting,
        url=url,
        ttl_hours=ttl_hours,
        support_email=settings.brand_support_email,
        tagline=settings.brand_tagline,
    )
    return subject, text_body, html_body


def _verification_html(
    *,
    brand: str,
    primary_color: str,
    greeting: str,
    url: str,
    ttl_hours: int,
    support_email: str,
    tagline: str,
) -> str:
    safe_url = escape(url, quote=True)
    safe_brand = escape(brand)
    safe_greeting = escape(greeting)
    safe_tagline = escape(tagline)
    support_line = (
        f'<p style="margin:16px 0 0;font-size:13px;color:#6b7280;">Cần hỗ trợ? '
        f'Liên hệ <a href="mailto:{escape(support_email, quote=True)}">{escape(support_email)}</a>.</p>'
        if support_email
        else ""
    )
    return f"""<!doctype html>
<html lang="vi">
  <body style="margin:0;padding:24px;background:#f3f4f6;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#111827;">
    <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:12px;padding:32px;">
      <p style="margin:0 0 4px;font-size:18px;font-weight:700;color:{escape(primary_color)};">{safe_brand}</p>
      <p style="margin:0 0 20px;font-size:13px;color:#6b7280;">{safe_tagline}</p>
      <p style="margin:0 0 12px;font-size:15px;">{safe_greeting}</p>
      <p style="margin:0 0 12px;font-size:15px;">Cảm ơn bạn đã đăng ký thành viên {safe_brand}.
        Vui lòng xác nhận địa chỉ email của bạn:</p>
      <p style="margin:24px 0;">
        <a href="{safe_url}" style="display:inline-block;background:{escape(primary_color)};color:#ffffff;
           text-decoration:none;padding:12px 20px;border-radius:8px;font-size:15px;">Xác nhận email</a>
      </p>
      <p style="margin:0 0 12px;font-size:13px;color:#6b7280;">Hoặc mở liên kết này:<br>
        <a href="{safe_url}" style="color:{escape(primary_color)};word-break:break-all;">{escape(url)}</a></p>
      <p style="margin:0;font-size:13px;color:#6b7280;">Liên kết có hiệu lực trong {ttl_hours} giờ kể từ khi
        email này được gửi. Nếu bạn không thực hiện đăng ký này, hãy bỏ qua email này.</p>
      {support_line}
    </div>
  </body>
</html>
"""


# --------------------------------------------------------------------------- backends
def _print_console(to: str, subject: str, text_body: str) -> None:
    """Greppable block: ``[EMAIL][console] to=<to> subject=<subject>`` then the body."""
    print(f"[EMAIL][console] to={to} subject={subject}")
    print(text_body if text_body.endswith("\n") else f"{text_body}\n", end="")
    print()


def _from_address(settings: Settings) -> str:
    return settings.smtp_from or settings.smtp_user or settings.brand_support_email


def _build_message(
    settings: Settings, to: str, subject: str, text_body: str, html_body: str | None
) -> EmailMessage:
    message = EmailMessage()
    sender = _from_address(settings)
    if sender:
        message["From"] = formataddr((settings.smtp_from_name, sender)) if settings.smtp_from_name else sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    return message


def _send_smtp(
    settings: Settings, to: str, subject: str, text_body: str, html_body: str | None
) -> EmailResult:
    try:
        message = _build_message(settings, to, subject, text_body, html_body)
        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
        ) as smtp:
            if settings.smtp_tls:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
    except Exception as exc:  # email must never break the caller
        logger.warning("SMTP delivery to %s failed: %s", to, exc)
        return EmailResult(False, SMTP_BACKEND, error=str(exc) or exc.__class__.__name__)

    logger.info("SMTP delivery to %s succeeded (subject=%s)", to, subject)
    return EmailResult(True, SMTP_BACKEND)
