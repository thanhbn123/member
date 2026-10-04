# DEPLOYMENT — MEMBER service

Production notes for the standalone member service (FastAPI + SQLAlchemy 2.0 + Alembic).

> **Status of this milestone:** the project is **NOT deployed to any VPS** (no server, no
> domain, no systemd unit is installed anywhere). Everything below is the reference recipe
> to be used when a deployment is actually requested. Local development and CI use SQLite;
> PostgreSQL is the only supported production database.

---

## 1. Runtime requirements

| Item | Value |
|---|---|
| Python | 3.12 (pinned in `pyproject.toml`, `requires-python = ">=3.12"`) |
| Database | PostgreSQL 16 (SQLite is dev/CI only) |
| Driver | `psycopg` 3 — URL scheme `postgresql+psycopg://` |
| App entrypoint | `app.main:app` (ASGI) |
| Migrations | Alembic, single head `0001_initial` |
| Outbound network | SMTP relay; optional Meta Conversions API and webhook target |

Suggested layout on a host:

```
/srv/member            # git checkout (code)
/srv/member/.venv      # virtualenv
/etc/member/member.env # environment file (chmod 600, root/member only)
```

## 2. PostgreSQL setup

```bash
sudo -u postgres psql <<'SQL'
CREATE ROLE member LOGIN PASSWORD 'REPLACE_WITH_A_STRONG_PASSWORD';
CREATE DATABASE member OWNER member ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
SQL
```

* Keep the cluster in UTC (`timezone = 'UTC'`); all timestamps are stored as aware UTC.
* `pg_hba.conf`: `host  member  member  127.0.0.1/32  scram-sha-256` (local socket / private network only,
  never expose 5432 publicly).
* Connection string: `postgresql+psycopg://member:PASSWORD@127.0.0.1:5432/member`
  (URL-encode special characters in the password, e.g. `@` → `%40`).
* Pool sizing comes from `DB_POOL_SIZE` / `DB_MAX_OVERFLOW`; keep
  `workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW)` below PostgreSQL `max_connections`.

## 3. Environment variables

Start from `.env.example` (it documents every setting). In production the file must be
readable only by the service user, **never** committed.

**Required in production** — `APP_ENV=production` makes the app **refuse to start** unless *all* of
these hold (the traceback lists every failing condition):

| Variable | Requirement enforced at startup |
|---|---|
| `APP_ENV` | `production` (turns on the strict guard + secure cookies) |
| `SECRET_KEY` | non-placeholder and **≥ 32 characters**; signs sessions/CSRF. Rotating it logs everybody out |
| `IP_HASH_SALT` | non-placeholder and **≥ 16 characters**; salt for IP hashing (raw IPs are never stored) |
| `MEMBER_API_KEY` | non-empty, non-placeholder and **≥ 16 characters**; protects member PII on `/api/v1/members*` (§6) |
| `EMAIL_MODE` | exactly `smtp` — `console` is refused (it prints raw verification links) |
| `SMTP_HOST` | non-empty when `EMAIL_MODE=smtp` |
| `DATABASE_URL` | must **not** be SQLite: `postgresql+psycopg://…` (see above) |
| `PUBLIC_BASE_URL` | non-empty and starting with **`https://`** — used in verification links and cookies |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD_HASH` | optional pair, but when the admin UI is enabled (both non-empty) the hash must **not** be a placeholder; leaving either empty disables the admin UI instead of blocking startup |

**Strongly recommended:** `BRAND_*`, `SMTP_USER`/`SMTP_PASSWORD`/`SMTP_TLS`/`SMTP_PORT`/`SMTP_FROM`,
`TRUSTED_PROXY_HEADERS=true` (only behind exactly one trusted proxy — §6),
`SECURITY_HEADERS_ENABLED=true`, `CSRF_ENABLED=true`, `GA4_MEASUREMENT_ID`, `META_*`,
`MEMBER_VERIFIED_WEBHOOK_*`.

Generating values:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"                       # SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"                       # IP_HASH_SALT
python -c "import secrets; print(secrets.token_urlsafe(32))"                       # MEMBER_API_KEY
python -c "from app.security import hash_password; print(hash_password('…'))"      # ADMIN_PASSWORD_HASH
```

## 4. Release procedure

```bash
cd /srv/member
git fetch --all && git checkout <tag-or-sha>
.venv/bin/pip install --upgrade -e .            # or: pip install -r requirements.lock
sudo install -m 600 -o root -g member /dev/null /etc/member/member.env   # first time only

# 1) migrate BEFORE starting the new code (single head, forward-only in practice)
set -a; . /etc/member/member.env; set +a
.venv/bin/alembic current          # what the DB is at
.venv/bin/alembic upgrade head     # apply pending revisions
.venv/bin/alembic current          # must now print 0001_initial (head)

# 2) restart the service and verify
sudo systemctl restart member
curl -fsS http://127.0.0.1:8000/health
```

Rules of thumb:

* Migrations run **once per release**, from the release user, before the app restart.
* Never edit a revision after it has been applied in an environment; add a new one.
* `alembic downgrade base` / `downgrade -1` works for `0001_initial` and is useful on
  throwaway databases, but on production prefer a forward fix or a restore (see §9).

## 5. Process supervision (systemd)

`/etc/systemd/system/member.service`:

```ini
[Unit]
Description=MEMBER service (FastAPI)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=exec
User=member
Group=member
WorkingDirectory=/srv/member
EnvironmentFile=/etc/member/member.env
# Bind to loopback: the reverse proxy is the only entry point.
# One worker keeps the in-process rate limiter exact (see §7); raise it only if
# you accept that every worker keeps its own counters.
ExecStart=/srv/member/.venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8000 \
    --workers 1 \
    --proxy-headers --forwarded-allow-ips 127.0.0.1 \
    --no-server-header --timeout-keep-alive 5
Restart=always
RestartSec=3
# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/srv/member
[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now member
sudo systemctl status member
journalctl -u member -f
```

* **uvicorn** is the reference server. `uvicorn --workers N` is preferred; the classic
  gunicorn recipe (`gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 2 -b 127.0.0.1:8000`)
  still works but recent uvicorn versions moved `UvicornWorker` out of the main package —
  if you use gunicorn, install the `uvicorn-worker` package and use
  `-k uvicorn_worker.UvicornWorker`.
* All workers must share the same environment file: `SECRET_KEY`, `IP_HASH_SALT` and every
  other setting must be identical, otherwise signed sessions break intermittently.
* `--forwarded-allow-ips 127.0.0.1` plus the app setting `TRUSTED_PROXY_HEADERS=true` is what
  makes the real client IP visible; only do this when the app is reachable **exclusively**
  through the proxy (loopback bind + firewall — see §6). The app takes the rightmost
  `X-Forwarded-For` hop (the one the proxy appended with `$proxy_add_x_forwarded_for`), falls back
  to `X-Real-IP`, then to the socket peer.

## 6. Reverse proxy and TLS

Terminate TLS at nginx/Caddy/Traefik and proxy to `127.0.0.1:8000`. Never expose the
uvicorn port to the internet.

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name members.example.com;

    ssl_certificate     /etc/letsencrypt/live/members.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/members.example.com/privkey.pem;

    client_max_body_size 256k;          # keep in sync with MAX_REQUEST_BYTES

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        # $proxy_add_x_forwarded_for APPENDS $remote_addr to any client-supplied
        # header: the rightmost hop is therefore the only trustworthy one.
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # A verification request can block for ~28 s before it answers:
        # 3 webhook attempts x WEBHOOK_TIMEOUT_SECONDS (5 s) + backoff (1 s + 2 s)
        # + the Meta call (META_TIMEOUT_SECONDS, 10 s). Keep this >= 60 s so the
        # proxy never cuts a slow-but-successful verification with a 504.
        proxy_read_timeout 60s;
    }
}
server { listen 80; server_name members.example.com; return 301 https://$host$request_uri; }
```

* TLS is mandatory in production: the verification link, session cookie and CSRF cookie must
  never travel in clear text. `SESSION_HTTPS_ONLY` defaults to `true` in production, and the
  app sets security headers (CSP etc.) — keep `SECURITY_HEADERS_ENABLED=true`.
* `TRUSTED_PROXY_HEADERS=true` is correct **only** under this exact contract: *exactly one* trusted
  reverse proxy in front, and that proxy **appends** the peer address
  (`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`). The app then uses the
  **rightmost** `X-Forwarded-For` hop — the value this proxy appended — and ignores everything to
  its left (client-supplied); it falls back to `X-Real-IP`, then to the socket peer. If the app can
  be reached directly, leave it `false`: a client would otherwise choose its own rate-limit bucket
  and `ip_hash`, defeating IP-based rate limiting.
* Keep `client_max_body_size` in sync with `MAX_REQUEST_BYTES` (the `413` is preventive: the body is
  buffered and rejected before any handler runs, with no side effects).
* Set `PUBLIC_BASE_URL=https://members.example.com` so emails contain the public URL.

## 7. Multi-worker caveats

**Rate limiter (in-process).** `app.ratelimit` keeps counters in the memory of each worker.
With `--workers N` the effective limit becomes up to `N ×` the configured value, and every
restart resets the counters. Mitigations, in order of preference:

1. run a single worker (`--workers 1`) — the app is I/O bound and this keeps limits exact;
2. add a reverse-proxy limit (`limit_req_zone` / `limit_req` in nginx) as a coarse outer guard;
3. use a shared store (Redis) behind the same interface when the project grows.

This affects `REGISTER_RATE_LIMIT`, `API_RATE_LIMIT` and `LOGIN_RATE_LIMIT` alike — as well as the
fixed per-member resend cap (3 verification emails/hour) — the admin login limit is a brute-force
protection, so do not multiply it by the worker count.

**Console email mode** is impossible in production: the startup guard requires `EMAIL_MODE=smtp`,
precisely because the console backend **prints the raw verification link to stdout by design**
(that is how the local harness and the tests read it). `EMAIL_MODE=console` therefore only prints
`[EMAIL][console] …` blocks to the stdout of whichever worker handled the request in local
development, tests and CI: no email is delivered and with several workers the output is interleaved
in the journal. Production must use `EMAIL_MODE=smtp` with a working relay — and it refuses to boot
otherwise.

## 8. Email deliverability (SPF / DKIM / DMARC)

Verification emails are transactional; landing in spam breaks the whole registration funnel.

* Send from a subdomain you control (e.g. `mail.example.com`), not from a shared relay domain.
* **SPF** — publish the relay's include, e.g. `v=spf1 include:relay.example.net -all`
  (one SPF record per domain; keep it under 10 DNS lookups).
* **DKIM** — enable signing at the relay and publish its public key as
  `<selector>._domainkey.mail.example.com`. The relay must sign with a domain aligned to the
  `From:` header.
* **DMARC** — start with `v=DMARC1; p=none; rua=mailto:dmarc@example.com`, review the reports,
  then move to `p=quarantine` and finally `p=reject`.
* Set the envelope/`From` to the same aligned domain (`SMTP_FROM`), add a display name
  (`SMTP_FROM_NAME`), use STARTTLS (`SMTP_TLS=true`), and keep `VERIFICATION_TOKEN_TTL_HOURS`
  generous enough (48 h default) for delayed delivery.
* Warm up new sending IPs/domains gradually and monitor bounces/complaints; failed sends are
  recorded as `EMAIL_FAILED` events and surfaced to the user as a resend option.

## 9. Backups and restore

* **Database** — nightly logical backup plus retention, e.g.
  `pg_dump -Fc -U member member > /var/backups/member/member_$(date +%F).dump`, keep ≥ 30 days,
  store off-host, and enable WAL archiving / PITR when the member list becomes business
  critical.
* **Restore drill** — regularly restore into a scratch database
  (`createdb member_restore && pg_restore -d member_restore member_YYYY-MM-DD.dump`), then run
  `alembic current` against it and confirm it equals the deployed head before trusting a backup.
* **Secrets** — back up `/etc/member/member.env` through your secret manager (it is not in the
  database dump and not in git). Losing `SECRET_KEY` invalidates sessions; losing
  `IP_HASH_SALT` makes existing `ip_hash` values non-comparable.
* **SQLite** — dev only. If you must, back up with `sqlite3 member.db ".backup out.db"`, never
  by copying a file that is being written.

## 10. Logs and monitoring

* The app logs to stdout/stderr; with systemd that is journald
  (`journalctl -u member -f`, retention via `SystemMaxUse=`). Requests carry a `request_id`
  that also appears in the API envelope `meta.request_id` — use it to correlate reports with logs.
* Health probes: `/health` (plain JSON, no secrets) for liveness; `/api/v1/health`
  (envelope, adds `database`, `email_mode`, `webhook_enabled`, `meta_enabled`) for readiness.
  A `database` value other than `ok` means the app cannot serve.
* Alert on: 5xx rate, p95 latency, `429` spikes, `EMAIL_FAILED` / `WEBHOOK_FAILED` /
  `META_EVENT_FAILED` events in `member_events`, PostgreSQL connection saturation and disk usage.
* After every release check `alembic current` against the repo head and watch the first
  minutes of `journalctl -u member`; `uvicorn` startup errors (bad config, unreachable DB) are
  fatal by design, especially with `APP_ENV=production` config guards.
* Never log secrets, raw IPs or token values — the code only stores hashed IPs and hashed
  verification tokens. In production (`EMAIL_MODE=smtp`) verification tokens are never written to
  logs; the local console backend prints the raw verification link by design and production refuses
  to start with it, so a deployment can never leak tokens through stdout. Keep it that way in any
  custom logging you add.

## 11. Rollback

1. Stop the rollout, restart the previous code revision (`git checkout <previous-sha>` +
   `systemctl restart member`) — this is safe while the schema is backwards compatible.
2. Schema rollback: `alembic downgrade -1` only if the revision is genuinely reversible and
   you accept the data loss it implies (`0001_initial` drops all tables). Otherwise restore the
   pre-release dump.
3. Prefer a forward fix (new revision) over a downgrade on live data.
