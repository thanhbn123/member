"""Operator CLI: `python -m app.cli <command>`.

Commands:
    hash-password    Generate an ADMIN_PASSWORD_HASH value (scrypt).
    check-config     Print the effective configuration with secrets masked.
    init-db          Run `alembic upgrade head` against DATABASE_URL.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path


def _mask(value: str, keep: int = 4) -> str:
    if not value:
        return "(empty)"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * min(12, len(value) - keep)}"


def cmd_hash_password(args: argparse.Namespace) -> int:
    from app.security import hash_password

    password = args.password or getpass.getpass("Admin password: ")
    confirm = args.password or getpass.getpass("Confirm password: ")
    if password != confirm:
        print("passwords do not match", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        return 1
    print(hash_password(password))
    print("# Put this in .env as ADMIN_PASSWORD_HASH=...", file=sys.stderr)
    return 0


def cmd_check_config(_: argparse.Namespace) -> int:
    import re

    from app.config import get_settings

    settings = get_settings()

    def _mask_dsn(dsn: str) -> str:
        """Hide the password of a database URL without mangling the rest of it."""
        return re.sub(r"(://[^:/@]+):[^@]*@", r"\1:***@", dsn)

    safe_url = _mask_dsn(settings.database_url)

    rows = [
        ("APP_NAME", settings.app_name),
        ("APP_ENV", settings.app_env),
        ("PUBLIC_BASE_URL", settings.public_base_url),
        ("BRAND_NAME", settings.brand_name),
        ("DATABASE_URL", safe_url or "(default)"),
        ("SECRET_KEY", _mask(settings.secret_key, 0)),
        ("IP_HASH_SALT", _mask(settings.ip_hash_salt, 0)),
        ("EMAIL_MODE", settings.email_mode),
        ("SMTP_HOST", settings.smtp_host or "(not set)"),
        ("ADMIN_EMAIL", settings.admin_email or "(not set)"),
        ("ADMIN_PASSWORD_HASH", _mask(settings.admin_password_hash, 7)),
        ("MEMBER_API_KEY", _mask(settings.member_api_key, 3)),
        ("WEBHOOK_ENABLED", str(settings.webhook_enabled)),
        ("META_ENABLED", str(settings.meta_enabled)),
        ("CSRF_ENABLED", str(settings.csrf_enabled)),
        ("SECURE_COOKIES", str(settings.secure_cookies)),
    ]
    width = max(len(name) for name, _ in rows)
    for name, value in rows:
        print(f"{name.ljust(width)} : {value}")
    warnings: list[str] = []
    if not settings.admin_configured:
        warnings.append("admin UI is disabled (set ADMIN_EMAIL + ADMIN_PASSWORD_HASH)")
    if settings.email_mode == "smtp" and not settings.smtp_host:
        warnings.append("EMAIL_MODE=smtp without SMTP_HOST - emails will fail")
    if settings.is_production and settings.database_url.startswith("sqlite"):
        warnings.append("production with SQLite is not recommended - use PostgreSQL")
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0


def _mask_email(address: str) -> str:
    local, _, domain = (address or "").partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def cmd_check_smtp(args: argparse.Namespace) -> int:
    """Prove the SMTP path stage by stage: CONNECT -> STARTTLS -> AUTH -> SEND.

    The password is never printed, and neither is the raw recipient (masked). This is the
    evidence command for the real-SMTP acceptance.
    """
    import smtplib
    from email.message import EmailMessage

    from app.config import get_settings

    settings = get_settings()
    host, port = settings.smtp_host, settings.smtp_port
    sender = settings.smtp_from or settings.smtp_user
    recipient = args.to or sender

    print(f"SMTP_HOST        : {host or '(empty)'}")
    print(f"SMTP_PORT        : {port}")
    print(f"STARTTLS/SSL     : {'implicit TLS' if port == 465 else ('starttls' if settings.smtp_tls else 'none')}")
    print(f"SMTP_USER        : {_mask_email(settings.smtp_user) if settings.smtp_user else '(none)'}")
    print(f"SMTP_PASSWORD    : {'set (' + str(len(settings.smtp_password)) + ' chars)' if settings.smtp_password else '(empty)'}")
    print(f"SMTP_FROM        : {_mask_email(sender) if sender else '(empty)'}")
    print(f"recipient        : {_mask_email(recipient) if recipient else '(empty)'}")

    if not host or not sender or not recipient:
        print("RESULT: configuration incomplete (need SMTP_HOST, SMTP_FROM/SMTP_USER, recipient)")
        return 2

    results: dict[str, str] = {}
    client = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    try:
        with client(host, port, timeout=settings.smtp_timeout_seconds) as smtp:
            results["SMTP CONNECT"] = "PASS"
            print(f"banner           : {smtp.ehlo()[1][:120]!r}")
            if settings.smtp_tls and port != 465:
                code, response = smtp.starttls()
                smtp.ehlo()
                results["STARTTLS"] = "PASS" if code == 220 else "FAIL"
                print(f"starttls         : {code} {response[:80]!r}")
            else:
                results["STARTTLS"] = "n/a"
            if settings.smtp_user:
                try:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                    results["SMTP AUTH"] = "PASS"
                except smtplib.SMTPAuthenticationError as exc:
                    results["SMTP AUTH"] = "FAIL"
                    print(f"auth error       : {exc.smtp_code} {exc.smtp_error!r}")
            else:
                results["SMTP AUTH"] = "n/a (no user configured)"

            message = EmailMessage()
            message["From"] = sender
            message["To"] = recipient
            message["Subject"] = args.subject or "[MEMBER] SMTP acceptance test"
            message.set_content(
                "MEMBER service SMTP acceptance test.\n"
                "No action required. This message proves the SMTP path end to end.\n"
            )
            smtp.send_message(message)
            results["GMAIL MESSAGE ACCEPTED"] = "PASS"
    except Exception as exc:  # noqa: BLE001 - the operator needs the reason
        failed_stage = next(
            (stage for stage in ("SMTP CONNECT", "STARTTLS", "SMTP AUTH", "GMAIL MESSAGE ACCEPTED")
             if stage not in results),
            "SEND",
        )
        results.setdefault(failed_stage, "FAIL")
        print(f"error            : {type(exc).__name__}: {exc}")

    print()
    for stage in ("SMTP CONNECT", "STARTTLS", "SMTP AUTH", "GMAIL MESSAGE ACCEPTED"):
        print(f"{stage:24s}: {results.get(stage, 'NOT REACHED')}")
    failed = [stage for stage, value in results.items() if value == "FAIL"]
    if failed:
        print(f"RESULT: FAIL ({', '.join(failed)})")
        return 1
    print("RESULT: PASS - the SMTP server accepted the message (inbox delivery still needs the recipient)")
    return 0


def cmd_member_status(args: argparse.Namespace) -> int:
    """Read-only view of one member's verification state (owner acceptance helper)."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import EmailVerificationToken, Member

    email = args.email.strip().lower()
    with session_scope() as db:
        member = db.execute(select(Member).where(Member.email == email)).scalar_one_or_none()
        if member is None:
            print(f"member {_mask_email(email)}: NOT FOUND")
            return 1
        tokens = list(
            db.execute(
                select(EmailVerificationToken)
                .where(EmailVerificationToken.member_id == member.id)
                .order_by(EmailVerificationToken.id.desc())
            ).scalars()
        )
        print(f"member           : {_mask_email(member.email)}")
        print(f"status           : {member.status}")
        print(f"email_verified_at: {member.email_verified_at or '(not verified)'}")
        print(f"created_at       : {member.created_at}")
        print(f"tokens           : {len(tokens)}")
        for token in tokens[:5]:
            print(
                f"  - id={token.id} expires={token.expires_at} "
                f"{'used at ' + str(token.used_at) if token.used_at else 'unused'}"
            )
        verified = member.status == "verified" and member.email_verified_at is not None
        print(f"VERIFIED         : {'YES' if verified else 'NO'}")
    return 0


def cmd_init_db(_: argparse.Namespace) -> int:
    from alembic.config import Config

    from alembic import command
    from app.config import get_settings

    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", get_settings().database_url)
    command.upgrade(config, "head")
    print("database is at head")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="member", description="MEMBER service operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_hash = sub.add_parser("hash-password", help="generate ADMIN_PASSWORD_HASH")
    p_hash.add_argument("--password", help="password (omit for an interactive prompt)")
    p_hash.set_defaults(func=cmd_hash_password)

    p_check = sub.add_parser("check-config", help="print effective configuration (masked)")
    p_check.set_defaults(func=cmd_check_config)

    p_init = sub.add_parser("init-db", help="apply migrations (alembic upgrade head)")
    p_init.set_defaults(func=cmd_init_db)

    p_smtp = sub.add_parser("check-smtp", help="prove the SMTP path (CONNECT/STARTTLS/AUTH/SEND)")
    p_smtp.add_argument("--to", help="recipient (default: SMTP_FROM / SMTP_USER)")
    p_smtp.add_argument("--subject", help="subject of the test message")
    p_smtp.set_defaults(func=cmd_check_smtp)

    p_status = sub.add_parser("member-status", help="verification state of one member")
    p_status.add_argument("--email", required=True)
    p_status.set_defaults(func=cmd_member_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
