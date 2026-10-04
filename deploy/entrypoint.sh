#!/usr/bin/env bash
# MEMBER service container entrypoint: wait for PostgreSQL, migrate, then run the CMD.
set -euo pipefail

DB_WAIT_SECONDS="${DB_WAIT_SECONDS:-60}"

echo "[entrypoint] waiting for the database (max ${DB_WAIT_SECONDS}s)"
python - "$DB_WAIT_SECONDS" <<'PY'
import os
import sys
import time

from sqlalchemy import create_engine, text

url = os.environ.get("DATABASE_URL")
if not url:
    print("[entrypoint] DATABASE_URL is not set", file=sys.stderr)
    raise SystemExit(1)

deadline = time.time() + float(sys.argv[1])
engine = create_engine(url, pool_pre_ping=True)
while True:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        print("[entrypoint] database is reachable")
        break
    except Exception as exc:  # noqa: BLE001 - we only need the message
        if time.time() >= deadline:
            print(f"[entrypoint] database not reachable: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        time.sleep(1.5)
PY

echo "[entrypoint] applying migrations (alembic upgrade head)"
alembic upgrade head

echo "[entrypoint] starting: $*"
exec "$@"
