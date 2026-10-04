#!/usr/bin/env bash
# Switch the MEMBER deployment from the internal SMTP sink to real Gmail SMTP.
#
# Run this ON THE SERVER (root@160.22.170.20) — or from your laptop with:
#   ssh -t root@160.22.170.20 'bash /srv/member/app/deploy/set-smtp-credentials.sh'
#
# The Gmail App Password is read from the terminal with echo DISABLED: it is never passed
# as a command-line argument, never exported into a visible environment, never written to
# the shell history and never printed. Only the env file (mode 600) receives it.
#
# Google requires an App Password (16 characters, 2-Step Verification must be on):
#   https://myaccount.google.com/apppasswords   ->  app name: VIPORDER Member
#
# The script backs up the current SMTP settings, applies the new ones, restarts the app,
# runs the staged SMTP self-test (CONNECT / STARTTLS / AUTH / SEND) inside the container
# and automatically ROLLS BACK if the new credentials do not work.

set -euo pipefail

ENV_FILE="${ENV_FILE:-/srv/member/env/app.env}"
STACK_DIR="${STACK_DIR:-/srv/member}"
COMPOSE=(docker compose --env-file env/stack.env -f app/deploy/docker-compose.yml)
CONTAINER="${CONTAINER:-member-member-app-1}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:18090/health}"
SMTP_HOST_VALUE="${SMTP_HOST_VALUE:-smtp.gmail.com}"
SMTP_PORT_VALUE="${SMTP_PORT_VALUE:-587}"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ -f "$ENV_FILE" ] || die "env file not found: $ENV_FILE"
[ -d "$STACK_DIR" ] || die "stack directory not found: $STACK_DIR"

say "=== MEMBER - switch to real Gmail SMTP ==="
say "env file : $ENV_FILE"
say "stack    : $STACK_DIR"
say

read -r -p "Gmail address (the account that owns the App Password): " GMAIL
[ -n "$GMAIL" ] || die "a Gmail address is required"
case "$GMAIL" in *@*.*) ;; *) die "'$GMAIL' does not look like an email address" ;; esac

read -r -p "From name [VIPORDER Member]: " FROM_NAME
FROM_NAME="${FROM_NAME:-VIPORDER Member}"

printf 'Gmail App Password (16 characters, input hidden): '
read -r -s APP_PASSWORD
printf '\n'
[ -n "$APP_PASSWORD" ] || die "an App Password is required"

# Google shows it as "abcd efgh ijkl mnop"; spaces are cosmetic.
NORMALIZED="$(printf '%s' "$APP_PASSWORD" | tr -d ' ')"
if ! printf '%s' "$NORMALIZED" | grep -Eq '^[A-Za-z0-9]{16}$'; then
  die "the App Password must be 16 letters/digits (got ${#NORMALIZED} characters)"
fi
unset APP_PASSWORD

BACKUP="$ENV_FILE.bak-smtp-$(date +%Y%m%d-%H%M%S)"
# Every SMTP key is captured even when it is absent today, so a rollback can never leave the
# newly entered App Password behind in the env file.
{
  for key in EMAIL_MODE SMTP_HOST SMTP_PORT SMTP_TLS SMTP_USER SMTP_PASSWORD SMTP_FROM SMTP_FROM_NAME; do
    grep -E "^${key}=" "$ENV_FILE" || echo "${key}="
  done
} > "$BACKUP"
chmod 600 "$BACKUP"
say
say "previous SMTP settings backed up to $BACKUP"

# Write the new values. The secret travels over stdin (never argv, never the environment).
printf '%s\n' "$NORMALIZED" | python3 - "$ENV_FILE" "$GMAIL" "$FROM_NAME" "$SMTP_HOST_VALUE" "$SMTP_PORT_VALUE" <<'PY'
import pathlib
import sys

env_path, gmail, from_name, host, port = sys.argv[1:6]
password = sys.stdin.readline().rstrip("\n")

updates = {
    "EMAIL_MODE": "smtp",
    "SMTP_HOST": host,
    "SMTP_PORT": port,
    "SMTP_TLS": "true",
    "SMTP_USER": gmail,
    "SMTP_PASSWORD": password,
    "SMTP_FROM": gmail,
    "SMTP_FROM_NAME": from_name,
}

path = pathlib.Path(env_path)
lines = path.read_text().splitlines()
out, seen = [], set()
for line in lines:
    key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else ""
    if key in updates:
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n")
print("env file updated:", ", ".join(sorted(updates)))
PY
unset NORMALIZED
chmod 600 "$ENV_FILE"

say
say "restarting the stack so the new settings are loaded"
( cd "$STACK_DIR" && "${COMPOSE[@]}" up -d >/dev/null )

say "waiting for the health check"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
    say "health: OK"
    break
  fi
  sleep 2
done
curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1 || {
  say "the app did not become healthy; rolling back"
  python3 - "$ENV_FILE" "$BACKUP" <<'PY'
import pathlib
import sys

env_path, backup = sys.argv[1:3]
backup_lines = pathlib.Path(backup).read_text().splitlines()
keys = {line.split("=", 1)[0] for line in backup_lines if "=" in line}
lines = pathlib.Path(env_path).read_text().splitlines()
kept = [line for line in lines if line.split("=", 1)[0] not in keys]
pathlib.Path(env_path).write_text("\n".join(kept + backup_lines) + "\n")
print("restored", len(backup_lines), "settings")
PY
  ( cd "$STACK_DIR" && "${COMPOSE[@]}" up -d >/dev/null )
  die "app unhealthy after the switch - previous settings restored"
}

say
say "=== SMTP self-test (one real message is sent to the same Gmail account) ==="
if docker exec "$CONTAINER" python -m app.cli check-smtp --to "$GMAIL"; then
  say
  say "SUCCESS: Gmail accepted the message. Check the inbox (and Spam) of $GMAIL,"
  say "click the verification link of the test registration and then run:"
  say "  docker exec $CONTAINER python -m app.cli member-status --email <address>"
  exit 0
fi

say
say "SMTP self-test FAILED - rolling back to the previous settings"
python3 - "$ENV_FILE" "$BACKUP" <<'PY'
import pathlib
import sys

env_path, backup = sys.argv[1:3]
backup_lines = pathlib.Path(backup).read_text().splitlines()
keys = {line.split("=", 1)[0] for line in backup_lines if "=" in line}
lines = pathlib.Path(env_path).read_text().splitlines()
kept = [line for line in lines if line.split("=", 1)[0] not in keys]
pathlib.Path(env_path).write_text("\n".join(kept + backup_lines) + "\n")
print("restored", len(backup_lines), "settings")
PY
( cd "$STACK_DIR" && "${COMPOSE[@]}" up -d >/dev/null )
die "Gmail rejected the credentials. Common causes: not an App Password (a normal account
password never works), 2-Step Verification disabled, or the App Password was revoked.
Fix it at https://myaccount.google.com/apppasswords and run this script again."
