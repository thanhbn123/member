"""Alembic environment for the MEMBER service.

The database URL is ALWAYS taken from application settings
(``settings.database_url`` -> ``DATABASE_URL`` env var / ``.env``); the
``sqlalchemy.url`` value in ``alembic.ini`` is a placeholder that is never used.

Works against SQLite (local dev / CI) and PostgreSQL (staging / production).
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

# NOTE: this repo contains an `alembic/` directory, so ruff's isort classifies the
# `alembic` package as first-party and groups it with `app.*`. Do not "fix" it.
from alembic import context
from app.config import get_settings
from app.dbtypes import UTCDateTime
from app.models import Base

# Alembic Config object: access to the values in alembic.ini.
config = context.config

# Python logging configuration from the [loggers]/[handlers] sections.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()

# Ignore the placeholder from alembic.ini and use the real application URL.
# ConfigParser interpolation is active, so literal '%' must be escaped as '%%'.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

# Metadata used by `alembic revision --autogenerate` / `alembic check`.
target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Render ``app.dbtypes.UTCDateTime`` as ``sa.DateTime(timezone=True)``.

    The custom aware-UTC type has no importable representation that a generated
    revision file could use, so autogenerate always spells the plain SQLAlchemy
    type it decorates. Returning ``False`` means "use the default rendering".
    """
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout, no DB connection)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (live connection from settings)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
