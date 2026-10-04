"""Email package: console/SMTP delivery behind a single small API."""

from app.email.service import EmailResult, send_email, send_verification_email

__all__ = ["EmailResult", "send_email", "send_verification_email"]
