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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
