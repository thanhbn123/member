# Deploying MEMBER on a VPS (Docker Compose + shared Caddy)

This is the concrete recipe used for the first deployment
(`https://member.quangkhoiwellnessretreat.com` on `160.22.170.20`). It assumes a
host that already runs a **shared Caddy** container on ports 80/443 (host network) and
Docker with the Compose plugin. Nothing is installed on the host itself.

```
/srv/member/
├── app/          # git clone of this repository
└── env/
    ├── app.env       # application environment (mode 600, never in git)
    └── stack.env     # PostgreSQL credentials for the compose stack (mode 600)
```

## 1. Layout and secrets

```bash
install -d -m 700 /srv/member/env
git clone https://github.com/thanhbn123/member.git /srv/member/app

# PostgreSQL credentials (referenced by docker-compose.yml)
cat > /srv/member/env/stack.env <<EOF
POSTGRES_USER=member
POSTGRES_PASSWORD=$(openssl rand -hex 24)
POSTGRES_DB=member
EOF

# Application environment
install -m 600 /srv/member/app/deploy/env.production.example /srv/member/env/app.env
#   then set at minimum:
#     PUBLIC_BASE_URL=https://<your-domain>
#     SECRET_KEY=$(openssl rand -hex 32)
#     IP_HASH_SALT=$(openssl rand -hex 16)
#     MEMBER_API_KEY=$(openssl rand -hex 24)
#     ADMIN_EMAIL / ADMIN_PASSWORD_HASH   (python -m app.cli hash-password)
#     DATABASE_URL=postgresql+psycopg://member:<POSTGRES_PASSWORD>@member-db:5432/member
#     EMAIL_MODE=smtp + SMTP_*            (see step 5)
chmod 600 /srv/member/env/stack.env /srv/member/env/app.env
```

The application **refuses to start** in `APP_ENV=production` if any of these is missing or
weak: `SECRET_KEY`, `IP_HASH_SALT`, `MEMBER_API_KEY`, `EMAIL_MODE=smtp` + `SMTP_HOST`,
an `https://` `PUBLIC_BASE_URL`, a non-SQLite `DATABASE_URL`, and a non-placeholder
`ADMIN_PASSWORD_HASH` when `ADMIN_EMAIL` is set. That guard is the deployment checklist.

## 2. Start the stack

```bash
cd /srv/member
docker compose --env-file env/stack.env -f app/deploy/docker-compose.yml up -d --build
docker compose --env-file env/stack.env -f app/deploy/docker-compose.yml ps
```

What runs:

| Service | Published on | Purpose |
|---|---|---|
| `member-app` | `127.0.0.1:18090` | uvicorn, runs `alembic upgrade head` on start |
| `member-db` | not published | PostgreSQL 16, volume `member-db` |
| `member-mailpit` | `127.0.0.1:8025` | SMTP sink + web UI, only until real SMTP is configured |

Loopback-only publishing is deliberate: the host firewall does not filter Docker-published
ports, so nothing except Caddy (host network) can reach them.

## 3. Reverse proxy + TLS

```bash
cp /srv/vip-staging-proxy/Caddyfile /srv/vip-staging-proxy/Caddyfile.bak-$(date +%F-%H%M)
cat /srv/member/app/deploy/Caddyfile.member >> /srv/vip-staging-proxy/Caddyfile
docker exec vip-staging-caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker exec vip-staging-caddy caddy reload   --config /etc/caddy/Caddyfile --adapter caddyfile
```

Caddy issues/renews Let's Encrypt certificates automatically and redirects `http` → `https`.
`TRUSTED_PROXY_HEADERS=true` is correct here because Caddy is the single proxy in front and
appends the peer address.

## 4. Verify

```bash
curl -fsS https://<your-domain>/health
curl -fsS -o /dev/null -w '%{http_code}\n' https://<your-domain>/register
python /srv/member/app/scripts/acceptance.py --skip-tests   # local harness, SQLite only
```

End-to-end on the live domain (this is what the first deployment was verified with):

1. register a member on `https://<your-domain>/register?utm_source=facebook`;
2. read the verification mail from the SMTP sink (`http://127.0.0.1:8025` on the VPS, or its
   API `GET /api/v1/messages`);
3. open the verification link → success page, member becomes `verified`;
4. log in at `/admin/login` and confirm the member, the attribution and the CSV export.

## 5. Switching from the SMTP sink to real Gmail SMTP

```bash
# in /srv/member/env/app.env
EMAIL_MODE=smtp
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=<gmail address>
SMTP_PASSWORD=<16-character Google App Password>   # requires 2FA on the Google account
SMTP_FROM=<gmail address>
SMTP_FROM_NAME=<brand>

cd /srv/member && docker compose --env-file env/stack.env -f app/deploy/docker-compose.yml up -d && \
docker compose --env-file env/stack.env -f app/deploy/docker-compose.yml logs --tail=20 member-app
```

Gmail requires an **App Password** (Google account → Security → 2-Step Verification →
App passwords); the normal account password will be rejected. Remember SPF/DKIM/DMARC if a
custom domain is used as the From address.

## 6. Updating, backups, rollback

```bash
# update
cd /srv/member/app && git pull --ff-only
cd /srv/member && docker compose --env-file env/stack.env -f app/deploy/docker-compose.yml up -d --build

# backup (daily cron)
docker exec member-member-db-1 pg_dump -U member -Fc member > /srv/member/backups/member-$(date +%F).dump

# rollback: pin the previous commit and rebuild
cd /srv/member/app && git checkout <previous-commit> && cd /srv/member && \
docker compose --env-file env/stack.env -f app/deploy/docker-compose.yml up -d --build
```

Migrations are forward-only; take a database backup before every release.

## 7. Operational notes

* The register/API/login rate limiter is **in-process**: with one uvicorn worker (the default
  here) it behaves as documented; scaling to N workers multiplies the limits by N and needs a
  shared store.
* `--no-access-log` is set in the container CMD so verification tokens in query strings never
  reach the container logs; Caddy writes its own access log to `/data/access-member.log`
  inside the proxy container.
* Logs: `docker compose logs -f member-app`, `docker exec vip-staging-caddy tail -f /data/access-member.log`.
* Health: `/health` (plain) and `/api/v1/health` (JSON envelope) — both key-free.
