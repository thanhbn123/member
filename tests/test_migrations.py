"""Alembic migrations: ``upgrade head`` builds the schema, ``downgrade base`` removes it.

Run in a subprocess so ``alembic/env.py`` reads ``DATABASE_URL`` from a clean
environment (exactly how it runs in CI/deployment) and never touches the engine
cached by the test session.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_COLUMNS: dict[str, set[str]] = {
    "members": {
        "id",
        "full_name",
        "email",
        "phone",
        "company",
        "status",
        "consent_marketing",
        "email_verified_at",
        "source",
        "notes",
        "created_at",
        "updated_at",
    },
    "member_attribution": {
        "id",
        "member_id",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "landing_url",
        "referrer",
        "fbp",
        "fbc",
        "user_agent",
        "ip_hash",
        "created_at",
    },
    "member_events": {"id", "member_id", "event_type", "metadata_json", "created_at"},
    "email_verification_tokens": {
        "id",
        "member_id",
        "token_hash",
        "expires_at",
        "used_at",
        "created_at",
    },
}

CORE_TABLES = set(EXPECTED_COLUMNS)


def _alembic(*args: str, database_url: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "ENV_FILE": str(REPO_ROOT / "no-such-dotenv-file"),
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _has_unique_index(inspector, table: str, column: str) -> bool:
    return any(
        index.get("unique") and list(index.get("column_names") or []) == [column]
        for index in inspector.get_indexes(table)
    )


def test_alembic_upgrade_head_then_downgrade_base(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'mig.db'}"

    upgraded = _alembic("upgrade", "head", database_url=database_url)
    assert upgraded.returncode == 0, f"alembic upgrade head failed:\n{upgraded.stdout}\n{upgraded.stderr}"

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        missing = CORE_TABLES - tables
        assert not missing, f"upgrade head did not create: {sorted(missing)} (found {sorted(tables)})"

        for table, expected in EXPECTED_COLUMNS.items():
            columns = {column["name"] for column in inspector.get_columns(table)}
            assert columns == expected, (
                f"{table} columns differ from app.models: "
                f"missing={sorted(expected - columns)} unexpected={sorted(columns - expected)}"
            )

        assert _has_unique_index(inspector, "members", "email"), "members.email must be unique"
        assert _has_unique_index(
            inspector, "email_verification_tokens", "token_hash"
        ), "email_verification_tokens.token_hash must be unique"

        with engine.connect() as connection:
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == "0001_initial"
    finally:
        engine.dispose()

    downgraded = _alembic("downgrade", "base", database_url=database_url)
    assert downgraded.returncode == 0, f"alembic downgrade base failed:\n{downgraded.stdout}\n{downgraded.stderr}"

    engine = create_engine(database_url)
    try:
        remaining = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert remaining & CORE_TABLES == set(), f"downgrade base left core tables behind: {sorted(remaining)}"
