# MEMBER — dịch vụ đăng ký thành viên độc lập

`MEMBER` is a self-contained FastAPI + SQLAlchemy 2.0 service that owns the *member registration*
domain for VIPORDER / VIP GROUP / VIP AI / Marketing Hub and any future customer site: a hosted
registration page, double opt-in email verification with one-time hashed tokens, first-touch
attribution (UTM, `fbclid`, `_fbp`, `_fbc`, salted IP hash), an append-only event audit trail, an
admin UI with search/filter/pagination/CSV export, a versioned JSON API (`/api/v1`), a signed
verified-member webhook and an opt-in Meta Conversions API bridge. The service is white-labelled
purely through environment variables — no customer name, colour, logo, domain or secret is
hard-coded anywhere in `app/` — so one deployment can serve exactly one brand and be cloned per
tenant.

**Status: MVP, deployed.** The service is **live at <https://member.quangkhoiwellnessretreat.com>**:
a Docker Compose stack in `/srv/member` on a VPS (`160.22.170.20`) behind a **shared Caddy** reverse
proxy (Let's Encrypt TLS), with PostgreSQL 16 in its own container, the app container running as a
**non-root** user and every published port bound to loopback, `EMAIL_MODE=smtp` pointed at a
temporary internal SMTP sink, `MEMBER_API_KEY` set and the admin UI enabled. The production runbook
(host layout, env files, Compose, Caddy vhost, backups, rollback) is
[`deploy/README.md`](deploy/README.md).

Local acceptance still passes: `python scripts/acceptance.py` runs the 16-step harness
(migrations → real uvicorn server → the full HTTP registration/verification/admin/dashboard flow →
`pytest` → secret scan) and reports `LOCAL ACCEPTANCE: 16/16 steps passed` on SQLite, and the same
16 steps pass with `--database-url postgresql+psycopg://…` against a real PostgreSQL 16 instance.
On the live server, `scripts/production_acceptance.sh` runs the **34-check production acceptance**
over HTTPS against the deployed domain — real registration, real delivery through the configured
SMTP backend (read back from the sink), the verification link and its single use, the database and
audit trail, admin login + dashboard + member management + CSV export, the API with the production
`MEMBER_API_KEY` (and 401 without it), and hosting hygiene (non-root container, loopback-only
ports). The pytest suite is **246 tests**, green on SQLite and PostgreSQL. **Honest caveats:**
until the Gmail credentials are configured, the SMTP sink captures member mail but **members do
not receive real email yet**; Meta is **disabled** until a pixel id + access token are provided.

---

## 1. Tính năng

* **Registration** — server-rendered Vietnamese registration form (`GET`/`POST /register`) with
  CSRF protection, rate limiting and server-side validation; the same operation is exposed as JSON
  for machine callers.
* **Landing page (customer-facing)** — `GET /` renders a configurable, mobile-first landing page
  (hero, benefits grid, “how it works”, FAQ, contact footer) instead of redirecting to `/register`.
  It embeds the **same** registration form — identical fields, POST target `/register`, CSRF token
  and hidden attribution inputs — so a conversion straight from the landing page is indistinguishable
  from one on `/register`, and `GET /register` keeps working unchanged for existing ad links.
  Every word, benefit, contact detail and link comes from `LANDING_*` / `BRAND_*` configuration
  (see §4.12); with `LANDING_SHOW_FORM=false` the hero shows a CTA button linking to `/register`
  instead of the form.
* **Email verification (double opt-in)** — every registration issues a cryptographically random
  one-time token; only `sha256(token)` is stored. Tokens expire after
  `VERIFICATION_TOKEN_TTL_HOURS` (48 h by default), issuing a new token supersedes all previous
  unused ones, and the claim is an atomic conditional `UPDATE` so two concurrent clicks can never
  both succeed.
* **Attribution** — first-touch capture of `utm_source|medium|campaign|content|term`, `fbclid`
  (turned into an `fb.1.<ms>.<fbclid>` `_fbc` value when Meta's cookie is absent), the `_fbp` /
  `_fbc` cookies, landing URL, referrer, user agent and a **salted hash of the client IP**.
  Client-side capture in `app/static/js/register.js` is a convenience only: the server re-reads the
  query string, cookies and headers, and those sources always win.
* **Events audit trail** — every lifecycle step is written to `member_events`
  (`REGISTER_STARTED`, `REGISTER_COMPLETED`, `EMAIL_SENT`, `EMAIL_FAILED`, `EMAIL_VERIFIED`,
  `MEMBER_UPDATED`, `LOGIN`, `EXPORT`, `WEBHOOK_SENT`, `WEBHOOK_FAILED`, `META_EVENT_SENT`,
  `META_EVENT_FAILED`) and surfaced on the member detail page.
* **Admin UI** — session login (`POST /admin/login`, scrypt-hashed password) and a dashboard
  (`GET /admin/dashboard`, also the post-login landing page) showing the headline counters (total,
  pending, verified, verification rate, registrations in the last 7 days, marketing consent — plus
  *đã huỷ* / *bị chặn* cards only when non-zero), a zero-filled 14-day registrations chart drawn with
  decile CSS classes (no inline styles), the top-5 UTM sources and top-5 UTM campaigns (`NULL` ⇒
  “không xác định”), the 10 newest members and quick links to the member list, the CSV export and the
  public `/register` page. The member list offers full-text search, status / `utm_source` /
  date-range filters and pagination (10/25/50/100); the member detail page shows attribution and the
  last 200 events and hosts the management form (status, notes ≤ 2000 chars, marketing consent) and a
  “resend verification” action for `pending` members. Every management change is audited as exactly
  one `MEMBER_UPDATED` event (`{"actor": …, "changed": {field: [old, new]}}`); a submission that
  changes nothing writes no event. The CSV export honours the active filters, is hardened against
  spreadsheet formula injection and emits a UTF-8 BOM so Excel opens Vietnamese text correctly.
* **Public API** — `/api/v1` JSON with a single response envelope, `X-API-Key` auth (mandatory in
  production), its own rate limit, request-id echo, and 201-vs-200 semantics that make duplicate
  registrations idempotent.
* **Webhook** — a signed `member.verified` POST fired after the verification transaction has
  committed, with HMAC-SHA256 signature, timestamp, delivery id and exponential-backoff retries.
* **Meta CAPI (preparation)** — `CompleteRegistration` events are built and sent to the
  Conversions API only when `META_PIXEL_ID` **and** `META_ACCESS_TOKEN` are both set;
  `client_ip_address` is deliberately omitted because raw IPs are never stored.
* **Branding via config** — `APP_NAME`, `BRAND_NAME`, `BRAND_LOGO_URL`, `BRAND_PRIMARY_COLOR`,
  `BRAND_SUPPORT_EMAIL`, `BRAND_TAGLINE` drive every template and the verification email.
* **Multi-tenant ready** — one deployment per customer brand; a tenancy is an `.env` file plus a
  database. Nothing in the code path branches on a customer name.

---

## 2. Kiến trúc

### 2.1 Module map (`app/`)

```
app/
├── __init__.py          __version__ ("0.1.0"), reported by /health
├── main.py              FastAPI app factory: middleware stack, router wiring, envelope error handlers, token-redacting log filter
├── config.py            pydantic-settings Settings — the source of truth for EVERY env var + prod guards
├── db.py                engine/session factory, get_db() dependency, session_scope() contextmanager
├── dbtypes.py           UTCDateTime — identical aware-UTC behaviour on SQLite and PostgreSQL
├── models.py            Member, MemberAttribution, MemberEvent, EmailVerificationToken + status/event enums
├── schemas.py           ok()/fail() envelope, APIError, RegisterRequest, MemberOut, MemberDetailOut
├── security.py          scrypt hashing, one-time tokens, IP hashing, CSRF, HMAC signing, client_ip()
├── normalize.py         server-side cleaning/validation: email, phone, UTM values, URLs, fbclid
├── ratelimit.py         in-process sliding-window limiter (per worker — see §10)
├── deps.py              FastAPI dependencies: CSRF, register/API/login rate limits, X-API-Key, admin session
├── middleware.py        request id, per-request CSP nonce, security headers, request-size cap, CSRF cookie
├── attribution.py       collects UTM / fbclid / _fbp / _fbc / referrer / user-agent / ip_hash per request
├── web.py               Jinja2 rendering, Brand context object, email masking
├── cli.py               operator CLI: hash-password, check-config, init-db
├── routers/
│   ├── public.py        HTML surface: landing page (GET /), /health, /register, /check-email, /verify-email, /welcome, /robots.txt
│   ├── api_v1.py        JSON API: register, get-by-id, lookup-by-email, resend-verification, health
│   └── admin.py         admin UI: login/logout, dashboard, member list/detail/edit, CSV export
├── services/
│   ├── members.py       registration + verification lifecycle (owns every transaction)
│   ├── admin.py         dashboard aggregates, member filters/pagination, validated edits + formula-injection-safe CSV writer
│   └── events.py        record_event() — the audit trail used by every module
├── email/
│   └── service.py       console / SMTP backends, Vietnamese verification email (text + HTML)
├── integrations/
│   ├── dispatch.py      post-commit fan-out; never raises; records WEBHOOK_*/META_* events
│   ├── webhook.py       signed member.verified delivery with retries and backoff
│   └── meta.py          Meta Conversions API CompleteRegistration (opt-in, hashed PII only)
├── templates/           Jinja2: base, landing, register, check_email, verify_result, welcome, error, admin/{login,dashboard,members,member_detail}
└── static/              css/app.css, css/landing.css, js/register.js (first-touch attribution capture)
```

Around the package: `alembic/versions/0001_initial.py` (single migration head), `tests/` (pytest
suite), `docs/` (this file's siblings), `.github/workflows/ci.yml` (lint + SQLite tests +
PostgreSQL 16 job), `pyproject.toml`, `alembic.ini`, `.env.example`.

### 2.2 Request flow — registration

```
Browser / API client            FastAPI app                        Database            Email backend
──────────────────────────────  ─────────────────────────────────  ──────────────────  ─────────────
GET  / (landing)           ───▶ render landing.html: hero + benefits + the SAME registration
                                form (identical field names, CSRF cookie, attribution inputs),
                                so this form posts to /register below without any special case
GET  /register             ───▶ render form, issue CSRF cookie
POST /register (form)      ───▶ require_csrf ─▶ register rate limit (key = sha256(ip+salt))
   or POST /api/v1/             require_api_key ─▶ api rate limit ─▶ register rate limit
        members/register   ───▶ normalize_email / normalize_phone  (NormalizationError → 422)
                                REGISTER_STARTED ────────────────▶ member_events
                                new:      insert member + attribution + token
                                          REGISTER_COMPLETED ────▶ member_events
                                existing: refresh missing attribution (first-touch kept),
                                          supersede old tokens, issue a new one
                                COMMIT ──────────────────────────▶ (durable)
                                send verification email ───────────────────────────▶ console stdout
                                          EMAIL_SENT / EMAIL_FAILED ─▶ member_events      or SMTP
303 → /check-email         ◀──  email failure NEVER fails the request
   or 201 / 200 envelope   ◀──  (email_error set, verification_sent = false)
```

### 2.3 Request flow — verification

```
Email link: GET /verify-email?token=<raw>
        │
        ▼
  sha256(raw) ──▶ SELECT email_verification_tokens WHERE token_hash = ?
        │
        ├── no row ................ → "invalid"  → 400 page
        ├── used_at IS NOT NULL ... → "used"     → 400 page
        └── expires_at <= now ..... → "expired"  → 410 page
                    │
                    ▼
        atomic UPDATE … SET used_at = now
          WHERE id = ? AND used_at IS NULL AND expires_at > now
                    │
                    ├── rowcount != 1 (lost the race) → "used" → 400 page
                    └── rowcount == 1
                          member.status = verified, email_verified_at = now
                          EMAIL_VERIFIED ──▶ member_events
                          COMMIT ─────────▶ durable
                          │
                          ▼  AFTER COMMIT, inside try/except:
                          dispatch.notify_member_verified(member)
                             ├── webhook  POST member.verified (signed)
                             └── Meta     CompleteRegistration (only if configured)
                          any failure here is recorded as *_FAILED and swallowed
                    │
                    ▼
              200 success page ("Xác minh thành công")
```

The successful claim also stores the member in the session, which is exactly what `GET /welcome`
renders: **verified member in the session → congratulations**; **no verified member (pending,
unknown or a fresh browser) → the neutral “Email chưa được xác minh” state** with the resend /
check-email links. The page never claims a membership it cannot prove.

### 2.4 The side-effect rule

> **Side effects (email, webhook, Meta) run after `COMMIT` and can never break verification.**

* `app/services/members.py` commits the member, the attribution row, the token and the
  `REGISTER_COMPLETED` / `EMAIL_VERIFIED` event *first*; only then does it call the email backend
  (`_deliver_verification`) or the integration fan-out (`_notify_verified`).
* Both call sites are wrapped in `try/except`: `send_email` / `send_member_verified` /
  `send_complete_registration` return *status objects*, never raise, and the caller records the
  outcome as an `EMAIL_SENT` / `EMAIL_FAILED` / `WEBHOOK_SENT` / `WEBHOOK_FAILED` /
  `META_EVENT_SENT` / `META_EVENT_FAILED` event.
* Consequence: an unreachable SMTP relay, a 500 from a webhook receiver or a bad Meta token
  degrades the *integration*, never the member record. A failed verification email is recoverable
  through the resend endpoint (§7).

---

## 3. Local setup

Python 3.12 is required (`requires-python = ">=3.12"`). The reference flow uses
[`uv`](https://docs.astral.sh/uv/):

```bash
# 1. virtualenv
uv venv --python 3.12
source .venv/bin/activate

# 2. install the app + dev extras (pytest, pytest-cov, ruff)
uv pip install -e ".[dev]"

# 3. configuration
cp .env.example .env
#    then edit .env — see the note below about DATABASE_URL

# 4. create the admin password hash (interactive prompt; paste the value into .env)
python -m app.cli hash-password
#    → prints e.g.  scrypt:16384:8:1:<salt_b64>:<hash_b64>
#    → set ADMIN_EMAIL and ADMIN_PASSWORD_HASH in .env to enable the admin UI
#    → keep the ":" separator exactly as printed: a "$"-separated hash is corrupted by
#      Docker Compose env_file interpolation and by shell `source` (see §4.5 and §12)

# 5. create the schema
alembic upgrade head

# 6. run it
uvicorn app.main:app --reload
```

Then open <http://localhost:8000/> for the landing page (the standalone form stays at
<http://localhost:8000/register>).

**SQLite default.** `.env.example` ships `DATABASE_URL=sqlite:///./member.db`, so a plain
`cp .env.example .env` runs on SQLite with no edits; the PostgreSQL DSN is included as a commented
line for production. The *code* default (when `DATABASE_URL` is not set at all) is the same value.

`sqlite:///./member.db` is relative to the **current working directory**, so if you run
`alembic upgrade head` and `uvicorn` from the repository root the file lands at `./member.db`
(repository root) and is already git-ignored via `*.db`. Both `alembic upgrade head` and
`alembic current` read `DATABASE_URL` through `alembic/env.py`, which always overrides the
placeholder in `alembic.ini`.

**Operator CLI.** `python -m app.cli <command>`:

| Command | What it does |
|---|---|
| `python -m app.cli hash-password` | Prompts twice, prints an `ADMIN_PASSWORD_HASH` value (scrypt). `--password '<pw>'` skips the prompt; minimum 8 characters. |
| `python -m app.cli check-config` | Prints the effective configuration with secrets masked (DB credentials, `SECRET_KEY`, `IP_HASH_SALT`, API key) plus warnings for a disabled admin UI, `EMAIL_MODE=smtp` without `SMTP_HOST`, and SQLite in production. |
| `python -m app.cli init-db` | Runs `alembic upgrade head` against the configured database and prints `database is at head`. |

`check-config` is the fastest way to confirm that `.env` is being read:

```console
$ python -m app.cli check-config
APP_NAME            : MEMBER
APP_ENV             : local
PUBLIC_BASE_URL     : http://localhost:8000
BRAND_NAME          : MEMBER
DATABASE_URL        : sqlite:///./member.db
SECRET_KEY          : ************
IP_HASH_SALT        : ************
EMAIL_MODE          : console
SMTP_HOST           : (not set)
ADMIN_EMAIL         : (not set)
ADMIN_PASSWORD_HASH : (empty)
MEMBER_API_KEY      : (empty)
WEBHOOK_ENABLED     : False
META_ENABLED        : False
CSRF_ENABLED        : True
SECURE_COOKIES      : False
```

Notes:

* `EMAIL_MODE=console` is the default, so a local registration prints the whole verification email
  — including the clickable link — to the terminal. Grep for `[EMAIL][console]`.
* Set `ENV_FILE=/path/to/other.env` in the *shell* environment to load a different env file; the
  default is `.env`.
* Interactive API docs are served at `/docs` (OpenAPI JSON at `/openapi.json`).

---

## 4. Environment

Every setting below exists in `app/config.py` (`Settings`). Grouping mirrors `.env.example`.
“Required in production” means:

* **Yes (guard)** — `APP_ENV=production` makes the app **refuse to start** if the value is left at
  its development default (validated in `Settings._production_guards`).
* **Yes** — not enforced by a guard, but the service is broken or insecure without it.
* **Recommended** — safe default; review it for your deployment.

### 4.1 Application

| Variable | Default | Required in production | Meaning |
|---|---|---|---|
| `APP_NAME` | `MEMBER` | Recommended | Service name shown in logs, `/health` and the OpenAPI title. |
| `APP_ENV` | `local` | **Yes** | `local` \| `staging` \| `production`. `production` enables the config guards and secure cookies. |
| `DEBUG` | `false` | **Yes** (keep `false`) | Verbose errors and extra logging. Never enable in production. |
| `LOG_LEVEL` | `INFO` | Recommended | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL`; applied by `logging.basicConfig` at startup. |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | **Yes** | Public origin used to build verification links (`<base>/verify-email?token=…`) and canonical URLs. Trailing slash is stripped. Must be the public `https://` origin in production. |

### 4.2 Branding (white-label)

| Variable | Default | Required in production | Meaning |
|---|---|---|---|
| `BRAND_NAME` | `MEMBER` | Recommended | Brand shown in templates and in the email subject/body. Falls back to `APP_NAME` when empty. |
| `BRAND_LOGO_URL` | *(empty)* | Recommended | Absolute URL of the logo; empty renders text only. |
| `BRAND_PRIMARY_COLOR` | `#2563eb` | Recommended | Hex colour (`#rgb` or `#rrggbb`); invalid values raise at startup. Injected as the `--brand` CSS variable and used in the email button. |
| `BRAND_SUPPORT_EMAIL` | *(empty)* | Recommended | Support address shown on pages, in the email footer and used as a last-resort envelope sender. Empty hides it. |
| `BRAND_TAGLINE` | `Đăng ký thành viên` | Recommended | Short tagline under the brand name (the env file is read as UTF-8). It is also the landing-page hero title fallback when `LANDING_HERO_TITLE` is empty. |
| `BRAND_PHONE` | *(empty)* | Optional | Phone number shown in the landing-page contact band. Rendered as text and as a `tel:` link when it holds at least 6 digits (only digits and a leading `+` go into the `href`). Empty hides the field. |
| `BRAND_ADDRESS` | *(empty)* | Optional | Postal address (single line) in the landing-page contact band. Empty hides the field. |
| `BRAND_FACEBOOK_URL` | *(empty)* | Optional | Facebook page URL in the landing-page contact band. **`http(s)` only**: any other scheme (`javascript:`, `data:`, `ftp:`, …) is ignored at load time and the link is not rendered. |
| `BRAND_ZALO_URL` | *(empty)* | Optional | Zalo URL (e.g. `https://zalo.me/<oa-id>`), same `http(s)`-only rule as above. Empty hides the field. |

### 4.3 Database

| Variable | Default | Required in production | Meaning |
|---|---|---|---|
| `DATABASE_URL` | `sqlite:///./member.db` | **Yes** | SQLAlchemy URL. SQLite for local dev; `postgresql+psycopg://user:password@host:5432/member` in production. |
| `DB_ECHO` | `false` | **Yes** (keep `false`) | Log every SQL statement — development only. |
| `DB_POOL_SIZE` | `5` | Recommended | PostgreSQL pool size (ignored for SQLite). |
| `DB_MAX_OVERFLOW` | `10` | Recommended | Extra connections above the pool size; keep `workers × (pool + overflow)` below PostgreSQL `max_connections`. |

### 4.4 Security

| Variable | Default | Required in production | Meaning |
|---|---|---|---|
| `SECRET_KEY` | `dev-insecure-secret-key-change-me` | **Yes (guard)** | Signs the session cookie (and therefore the admin session). Rotating it logs everybody out. Generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"`. |
| `IP_HASH_SALT` | `dev-insecure-ip-salt-change-me` | **Yes (guard)** | Salt for `SHA256(ip + salt)`. Raw IPs are never stored, so this value is what makes `ip_hash` non-reversible. |
| `TRUSTED_PROXY_HEADERS` | `false` | Only behind **exactly one** trusted proxy | Honor proxy headers **only** under this contract: exactly ONE trusted reverse proxy sits in front and *appends* the peer address (`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`). The app then uses the **rightmost** `X-Forwarded-For` hop — the value that proxy appended — falling back to `X-Real-IP`, then to the socket peer; everything the client sent to the left of the rightmost hop is ignored. Never enable it when the app is directly reachable by clients: the caller would then control its own rate-limit bucket and `ip_hash`. `X-Real-IP` also works (it is consulted when no `X-Forwarded-For` is present). |
| `SECURITY_HEADERS_ENABLED` | `true` | **Yes** (keep `true`) | Emit CSP, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Cross-Origin-Opener-Policy`, `Permissions-Policy` and (when cookies are secure) HSTS. |
| `MAX_REQUEST_BYTES` | `262144` (256 KiB) | Recommended | Body cap. The middleware **buffers** the body and rejects anything over the cap with `413` — including a chunked body with no `Content-Length` — **without the handler ever running**: nothing is persisted and the request has no side effects. Keep the reverse proxy’s `client_max_body_size` in sync. |
| `SESSION_COOKIE_NAME` | `member_session` | Recommended | Name of the signed session cookie (admin login, last-verified member). |
| `SESSION_MAX_AGE_SECONDS` | `28800` (8 h) | Recommended | Session cookie lifetime. |
| `SESSION_HTTPS_ONLY` | *(unset)* | Recommended | `true`/`false` to force the `Secure` flag. Unset means automatic: secure when `APP_ENV=production` **or** `PUBLIC_BASE_URL` starts with `https://`. |
| `SESSION_SAME_SITE` | `lax` | Recommended | `lax` \| `strict` \| `none`. |
| `CSRF_ENABLED` | `true` | **Yes** (keep `true`) | Require a double-submit CSRF token on every HTML `POST`. JSON `/api/*` endpoints are exempt by design. |
| `CSP_EXTRA_SCRIPT_SRC` | *(empty)* | — | Extra `script-src` sources (space or comma separated), merged into the generated CSP. Needed only if you add third-party scripts: the per-request nonce already covers the built-in inline bootstrap, and the GA4/Meta hosts are allow-listed automatically when their ids are set. |
| `CSP_EXTRA_STYLE_SRC` | *(empty)* | — | Extra `style-src` sources. |
| `CSP_EXTRA_IMG_SRC` | *(empty)* | — | Extra `img-src` sources. |
| `CSP_EXTRA_CONNECT_SRC` | *(empty)* | — | Extra `connect-src` sources (e.g. an analytics endpoint). |
| `CSP_EXTRA_FRAME_SRC` | *(empty)* | — | Extra `frame-src` sources; the default policy allows only `'self'`. |

### 4.5 Admin

| Variable | Default | Required in production | Meaning |
|---|---|---|---|
| `ADMIN_EMAIL` | *(empty)* | Recommended | Admin login address (compared case-insensitively). Empty **or** empty hash disables the whole admin UI. |
| `ADMIN_PASSWORD_HASH` | *(empty)* | **Yes (guard) when the admin UI is enabled** | scrypt hash produced by `python -m app.cli hash-password`, encoded as **`scrypt:<n>:<r>:<p>:<salt_b64>:<digest_b64>`** — colon separated, **no `$`**. A `$`-separated value is silently corrupted by Docker Compose `env_file` interpolation (`$1`, `$16384` …) and by shell `source`, which breaks admin login (this happened in production); `verify_password` still accepts legacy `$` hashes, but new hashes are always emitted with `:`. A **malformed** hash blocks startup in production whenever `ADMIN_EMAIL` is also set. Never a plain password. In production, a placeholder value is refused at startup **when `ADMIN_EMAIL` and this hash are both non-empty**; leaving either empty simply disables the admin UI instead. See §12. |

### 4.6 Email / verification

| Variable | Default | Required in production | Meaning |
|---|---|---|---|
| `VERIFICATION_TOKEN_TTL_HOURS` | `48` | Recommended | How long a verification link stays valid. |
| `EMAIL_MODE` | `console` | **Yes (guard: must be `smtp`)** | `console` prints the email — **including the raw verification link** — to stdout (dev/CI by design); `smtp` really delivers it. Production refuses to start with `console`. |
| `SMTP_HOST` | *(empty)* | **Yes (guard when `EMAIL_MODE=smtp`)** | SMTP relay hostname; empty makes email sending fail with `SMTP_HOST is not configured`. |
| `SMTP_PORT` | `587` | **Yes** | Relay port (587 = submission + STARTTLS, 465 = implicit TLS, 25/1025 = local relay). |
| `SMTP_USER` | *(empty)* | Recommended | Login user; empty skips `smtp.login()` (unauthenticated relay). |
| `SMTP_PASSWORD` | *(empty)* | Recommended | Login password. |
| `SMTP_FROM` | *(empty)* | **Yes** | Envelope `From:` address; must be SPF/DKIM-authorised for the sending domain. Falls back to `SMTP_USER`, then `BRAND_SUPPORT_EMAIL`. |
| `SMTP_FROM_NAME` | *(empty)* | Recommended | Display name in the `From:` header. |
| `SMTP_TLS` | `true` | **Yes** (keep `true`) | Issue `STARTTLS` before sending. Set `false` only for a local relay on port 25/1025. |
| `SMTP_TIMEOUT_SECONDS` | `15` | Recommended | Socket timeout; a hung relay can never block a request forever. |

### 4.7 Rate limiting

| Variable | Default | Required in production | Meaning |
|---|---|---|---|
| `REGISTER_RATE_LIMIT` | `10` | Recommended | Registrations allowed per IP hash per window: `POST /register` (HTML) **and** `POST /api/v1/members/register`, which is limited by this `register` scope **and** the `api` scope below. |
| `REGISTER_RATE_WINDOW_SECONDS` | `3600` | Recommended | Window for the above, in seconds. |
| `API_RATE_LIMIT` | `60` | Recommended | `/api/v1/members*` calls per IP hash per window (`api` scope). `POST /api/v1/members/register` also counts against `REGISTER_RATE_LIMIT`, and `resend-verification` additionally has a fixed per-member cap of **3 per hour** (`429` on the 4th), independent of this value. |
| `API_RATE_WINDOW_SECONDS` | `60` | Recommended | Window for the above, in seconds. |
| `LOGIN_RATE_LIMIT` | `10` | Recommended | Admin login attempts per IP hash per window (brute-force protection). |
| `LOGIN_RATE_WINDOW_SECONDS` | `900` | Recommended | Window for the above, in seconds. |

All counters are **in-process**: with `--workers N` the effective limit becomes up to `N ×` the
configured value (§10).

### 4.8 API

| Variable | Default | Required in production | Meaning |
|---|---|---|---|
| `MEMBER_API_KEY` | *(empty)* | **Yes (guard)** | **Mandatory in production** — the app refuses to start without a non-placeholder value of ≥ 16 characters, because `/api/v1/members*` can return member PII and trigger verification emails. When set, every `/api/v1/members*` request must send `X-API-Key` (constant-time compare) or receives `401`. `/health` and `/api/v1/health` are never protected. Empty outside production leaves the member endpoints open (local/dev only). |
| `API_DOCS_ENABLED` | `true` | Recommended `false` | When `false`, FastAPI registers neither `/docs` nor `/openapi.json` (both return `404`). Set it to `false` in production, or keep it on and protect `/docs` at the reverse proxy. |
| `CORS_ALLOW_ORIGINS` | *(empty)* | Recommended | Comma/semicolon separated browser origins allowed to call the API. Empty registers no CORS middleware (same-origin only). Credentials are never allowed. |

### 4.9 Meta (Conversions API)

| Variable | Default | Required in production | Meaning |
|---|---|---|---|
| `META_PIXEL_ID` | *(empty)* | Only for Meta | Dataset/pixel id. Meta is **disabled** unless this **and** `META_ACCESS_TOKEN` are set. |
| `META_ACCESS_TOKEN` | *(empty)* | Only for Meta | Conversions API access token. Masked in logs and error strings. |
| `META_API_VERSION` | `v21.0` | Recommended | Graph API version used in `https://graph.facebook.com/<version>/<pixel_id>/events`. |
| `META_TEST_EVENT_CODE` | *(empty)* | Leave empty in production | Events Manager test code; when set it is added to the payload as `test_event_code`. |
| `META_TIMEOUT_SECONDS` | `10` | Recommended | HTTP timeout for the Graph API call. |

### 4.10 Webhook

| Variable | Default | Required in production | Meaning |
|---|---|---|---|
| `MEMBER_VERIFIED_WEBHOOK_URL` | *(empty)* | Only for webhook | Target URL. The webhook is **disabled** unless this **and** the secret are set. |
| `MEMBER_VERIFIED_WEBHOOK_SECRET` | *(empty)* | Only for webhook | HMAC-SHA256 signing secret; also scrubbed from recorded error strings. |
| `WEBHOOK_TIMEOUT_SECONDS` | `5` | Recommended | Per-attempt HTTP timeout. Worst case for `GET /verify-email` is ≈ 3 × 5 s + backoff (1 s + 2 s) + the Meta call (10 s) ≈ 28 s, which is why the reverse proxy must allow ≥ 60 s (§10, [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) §6). |
| `WEBHOOK_MAX_ATTEMPTS` | `3` | Recommended | Total attempts before the delivery is marked `failed`. |
| `WEBHOOK_BACKOFF_SECONDS` | `1.0` | Recommended | Base delay; the wait before attempt *n+1* is `backoff × 2^(n-1)` seconds (1 s, 2 s, 4 s …). |

### 4.11 Analytics

| Variable | Default | Required in production | Meaning |
|---|---|---|---|
| `GA4_MEASUREMENT_ID` | *(empty)* | Optional | GA4 measurement id (e.g. `G-XXXXXXXXXX`). When non-empty, the GA4 tag **is** rendered in `base.html` with the per-request CSP nonce and the CSP allow-list is widened for `googletagmanager.com` / `google-analytics.com`. Empty (default) renders nothing. |

### 4.12 Landing page (`GET /`)

Every value is optional, so an unconfigured install still renders a complete page with the brand
name and a generic benefits trio — nothing here names a customer. The page is served by
`app/templates/landing.html` + `app/static/css/landing.css`; the palette is **derived in CSS from
the injected `--brand` variable** (`BRAND_PRIMARY_COLOR`), so no colour, benefit or link is
hard-coded in the template.

| Variable | Default | Required in production | Meaning |
|---|---|---|---|
| `LANDING_HERO_TITLE` | *(empty)* | Recommended | Hero `<h1>`. Empty falls back to `BRAND_TAGLINE`, then to the brand name. Capped at 160 characters. |
| `LANDING_HERO_SUBTITLE` | *(empty)* | Recommended | Hero paragraph under the title. Empty hides it. Capped at 300 characters. |
| `LANDING_HERO_IMAGE_URL` | *(empty)* | Optional | Optional hero image; `http(s)` only, anything else (e.g. `javascript:`) is ignored. Empty (default) renders the text-only hero, which is designed to look complete without an image. |
| `LANDING_BENEFITS` | *(empty)* | Recommended | Benefits grid: up to **6** items, each `icon\|title\|description`, items separated by `;;`. An item with a missing/extra `\|` or an empty field is dropped silently; text is truncated (icon 8, title 80, description 240 chars) and anything past the 6th valid item is ignored. Empty or fully malformed → the generic defaults *Đăng ký nhanh* / *Xác minh email* / *Ưu đãi thành viên*. Example in [`.env.example`](.env.example). |
| `LANDING_CTA_TEXT` | `Đăng ký ngay` | Recommended | Label of the header, hero and embedded-form call-to-action buttons. Capped at 40 characters. |
| `LANDING_SHOW_FORM` | `true` | Recommended | `true` embeds the registration form in the page (anchor `#dang-ky`). `false` renders no form at all and points the hero CTA at `/register` instead — useful when an ad funnel must stay on its own page. |

Behaviour worth knowing:

* the embedded form is the **same** form as `register.html` (`full_name`, `email`, `phone`,
  `company`, `consent_marketing`, `csrf_token` + the hidden `utm_*`/`landing_url`/`referrer`/`fbp`/
  `fbc` inputs) and posts to the same `POST /register`, which is unchanged — CSRF, rate limiting,
  normalisation, duplicate handling and the `/check-email` redirect all behave identically;
* `app/static/js/register.js` is reused as-is (first-touch attribution capture + light client-side
  validation), so there is no second copy of that logic;
* a configured value is only ever rendered escaped (Jinja autoescape, no `|safe`) and the page
  carries **no inline `<script>`, no `style="…"` attribute and no external asset** — the layout
  switches through classes only, which keeps the nonce-based CSP intact.

> `ENV_FILE` is not a `Settings` field: it is read from the **shell** environment and selects which
> env file pydantic-settings loads (default `.env`).

---

## 5. Database

| Environment | URL |
|---|---|
| Local development / tests / CI | `sqlite:///./member.db` (SQLite file in the current working directory) |
| Production | `postgresql+psycopg://user:password@host:5432/member` (PostgreSQL via `psycopg` 3) |

Both backends are supported by the same models: no dialect-specific types and no server-side
defaults. `app/dbtypes.UTCDateTime` stores aware UTC everywhere (SQLite keeps naive UTC on disk
and re-attaches `UTC` on read). SQLite additionally gets `PRAGMA foreign_keys=ON` on every
connection, because SQLite does not enforce foreign keys by default.

### 5.1 Migrations

```bash
alembic upgrade head     # apply everything (also available as: python -m app.cli init-db)
alembic current          # what the database is at — must print 0001_initial (head)
alembic downgrade base   # drop everything (throwaway databases only)
```

There is a single revision, `0001_initial` (`alembic/versions/0001_initial.py`), whose DDL matches
`app/models.py` column for column, index for index. `alembic/env.py` always replaces the placeholder
URL in `alembic.ini` with `settings.database_url`, so `alembic.ini` never holds a real credential.
Run migrations **before** restarting the application on every release.

### 5.2 What each table stores

| Table | Contents |
|---|---|
| `members` | One row per member: UUID `id`, `full_name`, unique lower-cased `email`, optional `phone` / `company`, `status` (`pending` \| `verified` \| `unsubscribed` \| `blocked`), `consent_marketing`, `email_verified_at`, `source` (`web_form`, `api`, `viporder`, `vipgroup`, `import`, `other`), free-text `notes`, `created_at`, `updated_at`. |
| `member_attribution` | Exactly one row per member (`member_id` unique, `ON DELETE CASCADE`): `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`, `landing_url`, `referrer`, `fbp`, `fbc`, `user_agent`, `ip_hash`, `created_at`. First-touch wins — a later registration of the same email only fills fields that are still empty. |
| `email_verification_tokens` | One row per issued link: `member_id`, unique `token_hash` (SHA-256 hex of the raw token), `expires_at`, `used_at` (`NULL` = still usable), `created_at`. Issuing a new token sets `used_at` on all unused tokens of that member, so only the newest link works. |
| `member_events` | Append-only audit trail: `member_id` (nullable for pre-creation and login events), `event_type`, `metadata_json`, `created_at`. Types: `REGISTER_STARTED`, `REGISTER_COMPLETED`, `EMAIL_SENT`, `EMAIL_FAILED`, `EMAIL_VERIFIED`, `MEMBER_UPDATED`, `LOGIN`, `EXPORT`, `WEBHOOK_SENT`, `WEBHOOK_FAILED`, `META_EVENT_SENT`, `META_EVENT_FAILED`. |
| `alembic_version` | Alembic’s own bookkeeping (current revision). |

### 5.3 Privacy rule — no raw IPs, ever

> **A raw client IP is never stored. `member_attribution.ip_hash` is
> `SHA256(client_ip + IP_HASH_SALT)` (hex, 64 chars).**

Consequences that the code enforces:

* `app/security.hash_ip()` is the only path from an IP to storage, and it is used for attribution
  and for rate-limit keys — never the raw value.
* The webhook payload deliberately omits `ip_hash` (see `_ATTRIBUTION_FIELDS` in
  `app/integrations/webhook.py`).
* The Meta payload deliberately omits `client_ip_address`; `user_data` carries only SHA-256 hashes
  of email/phone plus `fbp`, `fbc` and the user agent.
* Because the hash is salted, two deployments with different `IP_HASH_SALT` values cannot be
  correlated — and losing the salt makes existing `ip_hash` values non-comparable (§9 of
  `docs/DEPLOYMENT.md`).

### 5.4 Backup

* **PostgreSQL (production):** nightly `pg_dump -Fc`, retained off-host ≥ 30 days, plus WAL
  archiving/PITR once the member list is business-critical. Restore drills, secret backup (`.env`
  is *not* in the dump) and the full procedure live in
  [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) §9.
* **SQLite (dev only):** back up with `sqlite3 member.db ".backup out.db"` — never by copying a
  file that is being written.
* The `member_events` table is the audit trail: it is append-only in the application and should be
  included in every backup.

---

## 6. SMTP setup

Email is a *best-effort side effect*: it runs after the registration/verification transaction has
committed and can never fail the request.

### 6.1 `EMAIL_MODE=console` (default — local development, tests, CI)

The verification email is printed to stdout as a greppable block:

```console
$ uvicorn app.main:app --reload
[EMAIL][console] to=nguyen.van.a@example.com subject=Xác nhận đăng ký thành viên VIPORDER
Xin chào Nguyễn Văn A,

Cảm ơn bạn đã đăng ký thành viên VIPORDER.
Vui lòng xác nhận địa chỉ email của bạn bằng liên kết dưới đây:

http://localhost:8000/verify-email?token=VtcBg-84wNt8v-iGWWf23PqgsLSjyCRC5iGn-AtulCc

Liên kết có hiệu lực trong 48 giờ kể từ khi email này được gửi.
Nếu bạn không thực hiện đăng ký này, hãy bỏ qua email này.

Trân trọng,
VIPORDER
```

The raw URL sits on its own line, so
`grep -o 'http://localhost:8000/verify-email?token=[^ ]*'` gives you a clickable link immediately.
The same block is what the test fixtures parse.

This is a **local/dev/CI-only** backend by design: it prints the raw verification link (a
credential) to stdout. Production cannot run it — the startup guard requires `EMAIL_MODE=smtp`
(§12).

### 6.2 `EMAIL_MODE=smtp` (real delivery)

```dotenv
EMAIL_MODE=smtp
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER="relay-user"
SMTP_PASSWORD="relay-password"   # real value only in your .env, never in git
SMTP_FROM=no-reply@example.com
SMTP_FROM_NAME=VIPORDER
SMTP_TLS=true
SMTP_TIMEOUT_SECONDS=15
```

*(Every value above is a placeholder. The project’s own secret scan —
`scripts/acceptance.py` step 15 — rejects a literal SMTP password assignment anywhere outside
`.env.example`, which is why the two credentials here are quoted placeholders.)*

Behaviour (`app/email/service.py`):

* `smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS)`.
* `STARTTLS` is issued when `SMTP_TLS=true` (keep it true on 587); `SMTP_TLS=false` is only for a
  local relay on 25/1025.
* `smtp.login(SMTP_USER, SMTP_PASSWORD)` only when `SMTP_USER` is non-empty.
* `From:` = `SMTP_FROM_NAME <SMTP_FROM>`; the address falls back to `SMTP_USER` and then to
  `BRAND_SUPPORT_EMAIL`.
* Messages are `multipart/alternative` — plain text plus an HTML body using `BRAND_PRIMARY_COLOR`,
  `BRAND_NAME`, `BRAND_TAGLINE` and `BRAND_SUPPORT_EMAIL`.
* A missing `SMTP_HOST` short-circuits with
  `EmailResult(sent=False, error="SMTP_HOST is not configured")`.

### 6.3 Failure and retry semantics

| Situation | What happens |
|---|---|
| SMTP raises, times out, is rejected | `EmailResult(sent=False, error=…)`, an `EMAIL_FAILED` event is recorded, the API returns `verification_sent: false` with `email_error` populated, and the **registration still succeeds** (201/303). |
| A member never received the link | Re-send with `POST /api/v1/members/{member_id}/resend-verification`, or submit the registration form again with the same email (a duplicate pending registration issues a fresh token and re-sends). Issuing a new token invalidates the previous link. |
| Member already verified | `resend-verification` returns `{"verification_sent": false, "error": "already_verified"}` with HTTP 200. |

### 6.4 Deliverability (SPF / DKIM / DMARC)

Send from a subdomain you control, publish one SPF record for the relay, enable DKIM signing with a
domain aligned to `SMTP_FROM`, and roll DMARC from `p=none` to `p=reject`. The full checklist —
including warm-up advice and how to monitor `EMAIL_FAILED` events — is in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) §8.

---

## 7. API

* **Base path:** `/api/v1` (frozen — see the versioning note in [`docs/API.md`](docs/API.md)).
* **Auth:** `MEMBER_API_KEY` is **mandatory in production** (the app refuses to start without it)
  because `/api/v1/members*` can return member PII and trigger verification emails; only `/health`
  and `/api/v1/health` stay open. When it is set, every member endpoint requires the header
  `X-API-Key: <key>` (constant-time comparison, `401` otherwise). Rate limits are keyed by the
  salted client IP hash: `POST /api/v1/members/register` is limited by **both** the `register`
  scope (`REGISTER_RATE_LIMIT`, default 10/3600 s) **and** the `api` scope (`API_RATE_LIMIT`,
  default 60/60 s), every other `/api/v1/members*` call by the `api` scope, and
  `resend-verification` additionally by a fixed per-member cap of 3 per hour. Every response
  carries `X-Request-ID`.
* **Envelope:** every `/api/v1` response — success *and* error — has the same four keys:

```json
{"success": true,  "data": { }, "error": null, "meta": {"request_id": "947133a75784483c97c475c0a5a5d435"}}
{"success": false, "data": null, "error": {"code": "validation_error", "message": "…", "details": [ ]}, "meta": {"request_id": "…"}}
```

Full endpoint reference, parameter tables, every status code and the pagination/filter semantics of
the admin CSV are in **[`docs/API.md`](docs/API.md)**.

### 7.1 `POST /api/v1/members/register`

Creates a pending member and sends the verification email. **`201` for a new member, `200` for a
duplicate** — the response body has the same shape, so a retry is safe.

```bash
curl -i -X POST http://localhost:8000/api/v1/members/register \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEMBER_API_KEY" \
  -d '{
        "full_name": "Nguyễn Văn A",
        "email": "Nguyen.Van.A@Example.COM",
        "phone": "0901234567",
        "company": "Công ty TNHH ABC",
        "consent_marketing": true,
        "utm_source": "viporder",
        "utm_medium": "email",
        "utm_campaign": "launch-2025",
        "fbclid": "IwAR0abc",
        "source": "viporder"
      }'
```

```http
HTTP/1.1 201 Created
content-type: application/json
x-request-id: 947133a75784483c97c475c0a5a5d435
```

```json
{
  "success": true,
  "data": {
    "member": {
      "id": "f9b4dba4-9fa3-4f5b-99d1-e06a5e9dc36e",
      "full_name": "Nguyễn Văn A",
      "email": "nguyen.van.a@example.com",
      "phone": "0901234567",
      "company": "Công ty TNHH ABC",
      "status": "pending",
      "consent_marketing": true,
      "email_verified_at": null,
      "created_at": "2026-10-04T06:23:36.891078Z",
      "updated_at": "2026-10-04T06:23:36.891079Z"
    },
    "duplicate": false,
    "verification_sent": true,
    "email_error": null
  },
  "error": null,
  "meta": {"request_id": "947133a75784483c97c475c0a5a5d435"}
}
```

The **same request again** (member still `pending`) returns `200` with `"duplicate": true`,
`verification_sent: true` and the *same* member id — the previous link is invalidated and a fresh
one is emailed. If the member is already `verified`, the duplicate response has
`"verification_sent": false` and no email is sent.

Request body fields (extra keys are rejected with `422`):

| Field | Type | Required | Notes |
|---|---|---|---|
| `full_name` | string (1–200) | **yes** | Whitespace-collapsed, control characters stripped. |
| `email` | string (3–320) | **yes** | Normalised to lower case (IDN-aware). This is the natural unique key. |
| `phone` | string ≤ 64 | no | Kept as digits with an optional leading `+`; 8–15 digits. |
| `company` | string ≤ 200 | no | |
| `consent_marketing` | boolean | no (default `false`) | Marketing consent flag; stored on the member. |
| `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term` | string ≤ 255 | no | Attribution; missing fields are left empty. |
| `landing_url`, `referrer` | string ≤ 2048 | no | Attribution metadata; never rendered as HTML. |
| `fbp`, `fbc` | string ≤ 255 | no | Meta browser cookies. |
| `fbclid` | string ≤ 255 | no | Converted to `fbc` in Meta’s `fb.1.<ms>.<fbclid>` format when `fbc` is absent. |
| `source` | string ≤ 50 | no | One of `web_form`, `api`, `viporder`, `vipgroup`, `import`, `other`; anything else is stored as `other`; default `api`. |

**Error example — `422`** (schema violation):

```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "validation_error",
    "message": "Dữ liệu gửi lên không hợp lệ.",
    "details": [{"field": "full_name", "message": "Field required"}]
  },
  "meta": {"request_id": "41056ffefa614b3fb4c5c385d81d95db"}
}
```

The same status is returned when the body passes the schema but fails server-side
**normalisation** — there the top-level `message` *is* the reason and `details` repeats it with the
machine field name (these are also the strings the HTML form shows):

```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "validation_error",
    "message": "Email không hợp lệ",
    "details": [{"field": "email", "message": "Email không hợp lệ"}]
  },
  "meta": {"request_id": "66a22099caaf435697e70327b05ce344"}
}
```

So `error.details` is **always** a non-empty list of `{"field", "message"}` — branch on
`error.code`, read `error.details[].field` for the offending input.

### 7.2 `GET /api/v1/members/{member_id}`

```bash
curl -s http://localhost:8000/api/v1/members/f9b4dba4-9fa3-4f5b-99d1-e06a5e9dc36e \
  -H "X-API-Key: $MEMBER_API_KEY"
```

```json
{
  "success": true,
  "data": {
    "id": "f9b4dba4-9fa3-4f5b-99d1-e06a5e9dc36e",
    "full_name": "Nguyễn Văn A",
    "email": "nguyen.van.a@example.com",
    "phone": "0901234567",
    "company": "Công ty TNHH ABC",
    "status": "pending",
    "consent_marketing": true,
    "email_verified_at": null,
    "created_at": "2026-10-04T06:23:36.891078Z",
    "updated_at": "2026-10-04T06:23:36.891079Z",
    "attribution": {
      "utm_source": "viporder",
      "utm_medium": "email",
      "utm_campaign": "launch-2025",
      "utm_content": null,
      "utm_term": null,
      "landing_url": "https://viporder.vn/register?utm_source=viporder",
      "referrer": null,
      "fbp": null,
      "fbc": "fb.1.1791095013875.IwAR0abc",
      "ip_hash": "8fef7944cd53293c903136db9a89385ec11e395678efda1783b185627f424be9",
      "created_at": "2026-10-04T06:23:36.883313Z"
    }
  },
  "error": null,
  "meta": {"request_id": "b47e0e83a1eb4739ac840f26426cb4a1"}
}
```

`attribution` is `null` when nothing was captured. An unknown **or malformed** id returns
`404 not_found` (never a 500).

### 7.3 `GET /api/v1/members?email=…` (lookup)

```bash
curl -s -G http://localhost:8000/api/v1/members \
  --data-urlencode "email=Nguyen.Van.A@Example.com" \
  -H "X-API-Key: $MEMBER_API_KEY"
```

Returns exactly the same payload as §7.2 (`MemberDetailOut`) for the normalised email, or
`404 not_found` when no member matches. An **invalid** address is a client error: `422
validation_error` with `details = [{"field": "email", "message": "Email không hợp lệ"}]` (it used to
escape as a `500`). This is the idempotency helper for integrators: look the member up before
registering.

### 7.4 `POST /api/v1/members/{member_id}/resend-verification`

No request body.

```bash
curl -s -X POST \
  http://localhost:8000/api/v1/members/f9b4dba4-9fa3-4f5b-99d1-e06a5e9dc36e/resend-verification \
  -H "X-API-Key: $MEMBER_API_KEY"
```

```json
{
  "success": true,
  "data": {"member_id": "f9b4dba4-9fa3-4f5b-99d1-e06a5e9dc36e", "verification_sent": true, "error": null},
  "error": null,
  "meta": {"request_id": "e1802206ff354ae7afce7013939229b8"}
}
```

For an already-verified member: `200` with
`{"member_id": "…", "verification_sent": false, "error": "already_verified"}` — the call is not an
error, it simply has nothing to do. Unknown id → `404 not_found`. On top of the `api` scope, one
member can be re-mailed at most **3 times per hour**: the 4th call in the window is `429
rate_limited` with `Retry-After`.

### 7.5 `GET /api/v1/health`

Never requires an API key. Reports capability flags so an integrator can self-check.

```bash
curl -s http://localhost:8000/api/v1/health
```

```json
{
  "success": true,
  "data": {
    "status": "ok",
    "app": "MEMBER",
    "env": "local",
    "database": "ok",
    "email_mode": "console",
    "webhook_enabled": false,
    "meta_enabled": false,
    "api_key_required": true
  },
  "error": null,
  "meta": {}
}
```

`status`/`database` become `"degraded"`/`"error"` when `SELECT 1` fails. The plain-text liveness
probe `GET /health` returns `{"status","app","env","version","database"}` with **no** envelope and
no secrets.

### 7.6 Error codes

| HTTP | `error.code` | When |
|---|---|---|
| `401` | `unauthorized` | `X-API-Key` missing or wrong while `MEMBER_API_KEY` is set. |
| `404` | `not_found` | Unknown/malformed member id, or email lookup miss. |
| `405` | `method_not_allowed` | Wrong HTTP method on a known path. |
| `413` | `payload_too_large` | Body over `MAX_REQUEST_BYTES`; emitted by the middleware **before the handler runs** (its envelope has an empty `meta`) — the request is buffered, rejected and has **no side effects**: nothing is persisted. |
| `422` | `validation_error` | Pydantic body validation (missing/extra/oversized/blank field), `NormalizationError` (bad email/phone), or `GET /api/v1/members?email=<invalid>` (`details: [{"field": "email", "message": "Email không hợp lệ"}]`) — the latter used to be a `500`. `error.details` is **always** a non-empty list of `{"field", "message"}`. Body validation: the top-level `message` is the generic `Dữ liệu gửi lên không hợp lệ.` and each detail carries Pydantic’s text. Normalisation failure: the top-level `message` **and** the single detail entry carry the Vietnamese reason (e.g. `Email không hợp lệ`), with `field` set to the machine name (`email`, `phone`, `utm`, `url`, `fbclid`, `body`). |
| `429` | `rate_limited` | `API_RATE_LIMIT` exceeded on any `/api/v1/members*` call, `REGISTER_RATE_LIMIT` too on `register` (both scopes apply), or the per-member resend cap (3/hour) on `resend-verification`; the response carries `Retry-After` in seconds (the HTML 429 page carries it too). |
| `500` | `internal_error` | Unhandled exception; the `request_id` in `meta` correlates with the server log line. |
| *any other* | `request_failed` | Fallback for any other `HTTPException` raised by the framework. |

### 7.7 Versioning

**`/api/v1` is frozen.** New optional response fields and new endpoints may be added, but existing
paths, request fields, status codes and the envelope shape will not change. Any breaking change —
removing/renaming a field, changing a status code or the duplicate semantics — requires a new
`/api/v2` namespace served side by side; `/api/v1` then keeps working until integrators have
migrated. See [`docs/API.md`](docs/API.md) for the full versioning policy.

---

## 8. Webhook integration

When `MEMBER_VERIFIED_WEBHOOK_URL` **and** `MEMBER_VERIFIED_WEBHOOK_SECRET` are both set, a
`member.verified` event is POSTed **after** the verification transaction commits.

**Event name:** `member.verified` (also sent as the `X-Member-Event` header).

**Headers:**

```
Content-Type: application/json
X-Member-Event: member.verified
X-Member-Timestamp: 2026-10-04T06:23:43.833313+00:00
X-Member-Signature: sha256=8f3afab6a5f2e4787f3bc848db45c0d8f71ef473cc95d13566e0dbef4fd46ec1
X-Member-Delivery: 3c0fce83-81ff-4886-abef-ee2738fd9c09
User-Agent: python-httpx/0.28.1
```

**Signature:** `X-Member-Signature: sha256=<hmac(secret, "<timestamp>.<raw body>")>`, i.e.
HMAC-SHA256 over the timestamp, a literal `.`, and the **exact bytes** of the request body, using
`MEMBER_VERIFIED_WEBHOOK_SECRET` as the key. Always verify against the raw body — never against a
re-serialised JSON object.

**Body** (compact JSON, `ensure_ascii=False`, keys in this order):

```json
{"event":"member.verified","occurred_at":"2026-10-04T06:23:43.784287+00:00","member":{"id":"7d1fffb3-108c-4aa9-8465-3ef19db94a05","full_name":"Trần Thị B","email":"b@example.com","phone":null,"company":null,"status":"verified","consent_marketing":false,"email_verified_at":"2026-10-04T06:23:43.778584+00:00","created_at":"2026-10-04T06:23:43.771403+00:00"},"attribution":{"utm_source":"viporder","utm_medium":"email","utm_campaign":"launch-2025","landing_url":"https://viporder.vn/register","fbc":"fb.1.1791095013875.IwAR0abc"}}
```

* `attribution` contains only the fields that have a value (`null`s and empty strings are omitted)
  and **never** contains `ip_hash` or `user_agent`.
* **Retry policy:** up to `WEBHOOK_MAX_ATTEMPTS` attempts (default 3). Any 2xx ends the delivery; a
  non-2xx status, a connection error or a timeout is retried after
  `WEBHOOK_BACKOFF_SECONDS × 2^(attempt-1)` seconds (default 1 s, then 2 s). The `X-Member-Delivery`
  UUID is stable across attempts — use it to de-duplicate. `X-Member-Signature` is recomputed with
  a fresh `X-Member-Timestamp` on every attempt, so check freshness against that header.
* **Timeout:** `WEBHOOK_TIMEOUT_SECONDS` per attempt (default 5 s). Worst case on the verification
  request: 3 attempts × 5 s + backoff 1 s + 2 s = 18 s for the webhook, plus the Meta call
  (`META_TIMEOUT_SECONDS`, 10 s) ≈ **28 s**, so keep the reverse proxy’s `proxy_read_timeout` at
  **≥ 60 s**.
* **Never blocking:** delivery happens after commit; the result is recorded as `WEBHOOK_SENT` or
  `WEBHOOK_FAILED` (with `status`, `attempts`, `http_status`, `duration_ms`, `error`) and never
  affects the member’s verification.

**Verification snippet (copy-paste):**

```python
import hashlib
import hmac

from flask import Flask, request  # any web framework works

app = Flask(__name__)
WEBHOOK_SECRET = "whsec_..."  # MEMBER_VERIFIED_WEBHOOK_SECRET


def verify(raw_body: bytes, timestamp: str, signature: str, secret: str = WEBHOOK_SECRET) -> bool:
    """Constant-time check of X-Member-Signature over the raw request body."""
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


@app.post("/hooks/member-verified")
def member_verified():
    raw = request.get_data()  # exact bytes, before any JSON parsing
    if not verify(raw, request.headers.get("X-Member-Timestamp", ""),
                  request.headers.get("X-Member-Signature", "")):
        return {"error": "invalid signature"}, 401
    payload = request.get_json(force=True)
    if payload.get("event") != "member.verified":
        return {"error": "unexpected event"}, 400
    member = payload["member"]
    # ... upsert into VIPORDER using member["id"] / member["email"], then return 2xx
    return {"ok": True}, 200
```

Return `2xx` quickly (enqueue slow work) — a slow receiver burns the retry budget. Reject a
signature whose timestamp is much older than now if you need replay protection.

---

## 9. Meta attribution

**Captured client-side** (`app/static/js/register.js`, no framework, no inline handlers):

* `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term` and `fbclid` from the URL —
  stored as **first touch** in `localStorage["member_attribution"]` (written once, never
  overwritten) and copied into hidden form fields when the server did not already render a value.
* `_fbp` and `_fbc` cookies (readable JavaScript cookies only); when `_fbc` is missing but `fbclid`
  is present, the script derives `fb.1.<Date.now()>.<fbclid>`.
* `landing_url` (first-touch URL) and `referrer`.
* `register.js` itself makes no network call and loads no third-party code: it only fills form
  fields. Everything is re-validated server-side, and the server’s own sources (query string,
  cookies, headers) always win over the hidden fields — a tampered form cannot forge a query
  string.

### 9.1 Optional browser pixels (Meta Pixel / GA4)

`app/templates/base.html` renders the official snippets **only** when the matching id is
non-empty, and every inline script carries the per-request CSP nonce:

| Setting | What is rendered when non-empty |
|---|---|
| `META_PIXEL_ID` | The `connect.facebook.net/en_US/fbevents.js` bootstrap (`fbq('init', …)`, `fbq('track','PageView')`) plus the `<noscript>` `<img>` fallback to `facebook.com/tr`. |
| `GA4_MEASUREMENT_ID` | The `googletagmanager.com/gtag/js?id=…` loader plus the inline `gtag('config', …)` bootstrap. |

Both ids are operator-controlled configuration (never user input), and the CSP allow-list for
`googletagmanager.com`, `google-analytics.com`, `connect.facebook.net` and `facebook.com` is added
automatically in exactly the same condition. With the `.env.example` defaults both ids are empty,
so a fresh clone loads **no** third-party script and sends no pixel request — which is the
privacy-preserving default.

**Stored server-side:** the same values plus `user_agent` and `ip_hash` in `member_attribution`
(see §5.3 — the raw IP is never stored).

**Sent to the Conversions API when enabled:** on verification, `app/integrations/meta.py` builds one
`CompleteRegistration` event and POSTs it to
`https://graph.facebook.com/<META_API_VERSION>/<META_PIXEL_ID>/events` (access token as a query
parameter):

```json
{
  "data": [
    {
      "event_name": "CompleteRegistration",
      "event_time": 1791095013,
      "action_source": "website",
      "event_id": "7d1fffb3-108c-4aa9-8465-3ef19db94a05",
      "user_data": {
        "em": ["<sha256 of the lower-cased email>"],
        "ph": ["<sha256 of the digits-only phone>"],
        "fbp": "fb.1.…",
        "fbc": "fb.1.…",
        "client_user_agent": "Mozilla/5.0 …"
      },
      "event_source_url": "https://viporder.vn/register",
      "custom_data": {"utm_source": "viporder", "utm_medium": "email", "utm_campaign": "launch-2025"}
    }
  ]
}
```

`event_id` is the member UUID, so Meta can de-duplicate retries. `client_ip_address` is
**deliberately omitted** — raw IPs are never stored. `META_TEST_EVENT_CODE`, when set, is added as
`test_event_code`.

**Why it stays disabled without `META_PIXEL_ID` + `META_ACCESS_TOKEN`:** the integration is strictly
opt-in (`Settings.meta_enabled` requires *both* values). With either one missing,
`send_complete_registration()` returns
`{"status": "disabled", "reason": "META_PIXEL_ID/META_ACCESS_TOKEN not configured"}`, **no outbound
call is made and no event row is written** (the dispatcher skips `disabled` results so the audit
trail stays clean). This is also the default in `.env.example` — a fresh clone sends nothing to Meta
and therefore needs no consent-handling work to be safe.

**Where to plug future events:** everything Meta-specific lives in `app/integrations/meta.py`.
`build_event(member, attribution, event_source_url)` constructs one event dict (hashing, `fbp`/`fbc`/
user-agent selection, UTM `custom_data`) and `send_complete_registration()` wraps it in the `data`
array with the timeout and the token-redacting error handling. To add e.g. `Lead` or `Subscribe`:

1. add a `build_<event>(…)` / `send_<event>(…)` pair in `meta.py`, reusing `_sha256_hex`,
   `_hash_phone`, `_sanitize` and `_custom_data`;
2. register the runner in `app/integrations/dispatch.py::_TRACKED` with its success/failure
   `EventType` so the attempt is audited;
3. call it from the post-commit path (`_notify_verified` in `app/services/members.py` for
   verification-triggered events).

Never send raw PII: hash first, and keep `ip_hash`/raw IPs out of the payload.

---

## 10. Production deployment notes

**The service is live** at <https://member.quangkhoiwellnessretreat.com> (VPS `160.22.170.20`),
deployed as the Docker Compose stack in `/srv/member` documented in
**[`deploy/README.md`](deploy/README.md)** — that file is the operational runbook (host layout,
`env/app.env` + `env/stack.env`, Compose services, shared-Caddy vhost, updates, backups, rollback).
The stack runs PostgreSQL 16 in its own container, the app in a **non-root** container whose ports
are published on loopback only, behind a **shared Caddy** container that terminates Let's Encrypt
TLS. `EMAIL_MODE=smtp` points at a temporary internal SMTP sink (loopback-only web UI) until the
Gmail credentials are configured, `MEMBER_API_KEY` is set and the admin UI is enabled. A production
acceptance of **34 checks over HTTPS** was run against the live domain: real delivery through the
configured SMTP backend, admin login, CSV export, the API with the production key, the non-root
container and the loopback-only ports. **Honest caveats:** members do **not** receive real email
until the Gmail credentials are configured (the sink captures it), and **Meta is disabled** until a
pixel id + access token are set (§4.9).

[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) remains the host-level reference recipe (systemd unit,
nginx/TLS, PostgreSQL role creation, backup/restore, rollback, monitoring): use it for a
bare-metal/VM install or when the Compose stack is not an option. The short version, valid for either
shape:

* **Database:** PostgreSQL 16 with `DATABASE_URL=postgresql+psycopg://…`; SQLite is dev/CI only.
* **Env vars:** start from `.env.example`, keep the file `chmod 600` and out of git.
  `APP_ENV=production` turns on the strict startup guard: the app **refuses to start** unless
  `SECRET_KEY` is a non-placeholder value of ≥ 32 chars, `IP_HASH_SALT` is non-placeholder and
  ≥ 16 chars, `MEMBER_API_KEY` is non-empty, non-placeholder and ≥ 16 chars, `EMAIL_MODE=smtp`
  with a non-empty `SMTP_HOST`, `DATABASE_URL` is not SQLite, `PUBLIC_BASE_URL` is non-empty and
  starts with `https://`, and `ADMIN_PASSWORD_HASH` is not a placeholder whenever `ADMIN_EMAIL` is
  also set. Every failing condition is listed in the traceback.
* **Migrations:** `alembic upgrade head` once per release, before restarting the service; verify with
  `alembic current` (must print `0001_initial (head)`).
* **Interactive docs:** set `API_DOCS_ENABLED=false` to stop registering `/docs` and
  `/openapi.json` (both then return `404`), or keep them enabled and protect `/docs` at the reverse
  proxy. The JSON API itself is never affected.
* **Reverse proxy:** terminate TLS at nginx/Caddy/Traefik, bind uvicorn to `127.0.0.1`, and set
  `TRUSTED_PROXY_HEADERS=true` **only** when *exactly one* trusted proxy sits in front and appends
  the peer address (`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for`). The app reads
  the **rightmost** `X-Forwarded-For` hop — the one that proxy appended — falling back to
  `X-Real-IP`, then to the socket peer. Never enable it when clients can reach the app directly:
  they could spoof the header and choose their own rate-limit bucket and `ip_hash`. Keep
  `client_max_body_size` in sync with `MAX_REQUEST_BYTES`, and set `proxy_read_timeout` to **≥ 60 s**
  because a verification request can take ≈ 28 s (3 × `WEBHOOK_TIMEOUT_SECONDS` (5 s) + backoff
  1 s + 2 s + `META_TIMEOUT_SECONDS` (10 s)) before it answers.
* **Multi-worker caveat:** the rate limiter is in-process. With `--workers N` the effective limit
  becomes up to `N ×` the configured value and restarts reset it — prefer `--workers 1`, or add a
  proxy-level `limit_req` as an outer guard. This affects `REGISTER_RATE_LIMIT`, `API_RATE_LIMIT`
  and `LOGIN_RATE_LIMIT` alike.
* **HTTPS-only cookies:** `SESSION_HTTPS_ONLY` is unset by default and resolves to `true` in
  production or whenever `PUBLIC_BASE_URL` starts with `https://`; the CSRF cookie and HSTS follow
  the same switch. Do not disable it.
* **Deployed topology:** a Docker Compose stack in `/srv/member` (app + PostgreSQL 16 + SMTP sink
  containers) behind the shared Caddy container on ports 80/443; every Compose port is published on
  `127.0.0.1` only, so nothing except Caddy is reachable from the internet. See
  [`deploy/README.md`](deploy/README.md) for the exact commands.
* **Hash encoding:** keep `ADMIN_PASSWORD_HASH` in its printed colon-separated `scrypt:…` form. A
  `$`-separated value is silently corrupted by Compose `env_file` interpolation and breaks admin
  login — see §4.5 and §12.

---

## 11. Tests

```bash
pytest -q            # the whole suite (Python 3.12, throwaway SQLite per test)
pytest -q -x         # stop at the first failure
pytest --cov=app     # with coverage (pytest-cov is a dev dependency)
ruff check .         # the same lint gate CI uses

# run the exact same suite against PostgreSQL (rows are truncated between tests)
TEST_DATABASE_URL=postgresql+psycopg://member:member@localhost:5432/member_test pytest -q

# full local acceptance: migrations + uvicorn + 15 end-to-end steps + pytest + secret scan
python scripts/acceptance.py
python scripts/acceptance.py --database-url postgresql+psycopg://member:member@localhost:5432/member_test
python scripts/acceptance.py --skip-tests --port 8123      # other flags: --skip-secret-scan, --keep-db
```

`tests/conftest.py` builds the environment *before* importing the app: a fresh temporary SQLite
database per test, console email mode, rate limits raised to non-blocking values, and the fixtures
`settings_env`, `client`, `web`, `mailbox`, `admin_client`, `db_session`, `database`,
`webhook_server`, `closed_port` and `app_settings` — covering the browser flow
(`web.register_and_verify(...)`), a captured console mailbox (`mailbox.latest_token()`), an
authenticated `admin_client`, a real local HTTP server that records webhook deliveries, and a
`closed_port` for connection-error paths. Exporting **`TEST_DATABASE_URL`** makes the whole
suite run against that database instead of the per-test SQLite file — truncating the four tables
between tests — which is exactly what the CI `postgres` job does (so “the tests pass on
PostgreSQL” is reproducible on any machine, not just in CI). The suite is **246 tests** at the time
of writing, green on SQLite **and** on PostgreSQL — including `tests/test_landing.py` (37 tests) and
`tests/test_admin_dashboard.py` (41 tests).

What is covered today:

* **Landing page** (`tests/test_landing.py`) — `GET /` renders `landing.html` (200, not the old
  307 → `/register` redirect) with the brand name and the embedded form; the form on `/` really
  registers a member when posted with the CSRF token taken from `/` (member row + console email +
  attribution); `LANDING_SHOW_FORM=false` renders a CTA link to `/register` and no `<form>`;
  custom benefits render in order, while 20 items / empty fields / a 5 000-character description are
  capped and never 500; a `javascript:` contact URL is never rendered as an `href`; the hero title
  falls back to the tagline; and the page carries no `style="…"` attribute and no `<script>` without
  the CSP nonce.

* **Admin dashboard** (`tests/test_admin_dashboard.py`) — the stat cards (including the
  unsubscribed/blocked cards that only appear when non-zero), the zero-filled 14-day series and its
  decile CSS classes, top-5 attribution with the `NULL` ⇒ “không xác định” bucket, the 10 recent
  members, the `/admin` → `/admin/dashboard` redirects, the management form’s validation / audit /
  no-change behaviour (including that a manual `verified` does **not** set `email_verified_at`) and
  the allow-listed `?msg=` flash codes + `next=` target of the resend action.

* **Email** (`tests/test_email.py`) — console backend output and greppability, SMTP message
  building / `From` fallback, and the guarantee that a failing backend returns
  `EmailResult(sent=False)` instead of raising.
* **Integrations** (`tests/test_integrations.py`) — webhook payload shape and HMAC signature
  verification, retry/backoff and failure paths, Meta `CompleteRegistration` event building (hashed
  `em`/`ph`, `fbp`/`fbc`, `custom_data`), the `disabled` short-circuit, and that
  `dispatch.notify_member_verified()` never raises and records `WEBHOOK_*` / `META_*` events.
* **Registration flow** (`tests/test_register_flow.py`) — the HTML form end to end, CSRF handling,
  normalisation/validation errors, duplicate handling and the check-email redirect.
* **Verification** (`tests/test_verification.py`) — one-time tokens (reuse rejected, expiry,
  superseding), the atomic claim and the `already_verified` path; the concurrent test is a **real
  multi-thread race** (several threads released together through a `threading.Barrier`) that asserts
  the conditional `UPDATE` lets exactly one claim win.
* **Security-review regressions** (`tests/test_review_regressions.py`) — locks down the findings of
  the security review: strict production guards (no API key / placeholder key refuses to boot),
  rightmost-hop proxy trust and shared buckets when the proxy is untrusted, per-IP/per-member rate
  limits, the preventive chunked/`Content-Length` body cap with no persisted side effects, hostile
  pagination, SMTP error scrubbing, the two `/welcome` states, the removed admin session setting and
  the 5 s webhook default.
* **JSON API** (`tests/test_api.py`) — envelope shape, `X-API-Key` enforcement, 201-vs-200
  duplicate semantics, lookup, resend and the `validation_error` `details` list.
* **Admin UI** (`tests/test_admin.py`) and **CSV export** (`tests/test_export_csv.py`) — login,
  session guard, filters/search/pagination and the formula-injection-safe export.
* **Security** (`tests/test_security.py`) — rate limits (including `Retry-After`), header/CSP
  assertions, hashed-IP storage, token hashing, and the **hash-encoding regressions**: new hashes use
  the shell/Compose-safe `scrypt:…` separator (no `$`), legacy `$` hashes still verify, and a
  malformed `ADMIN_PASSWORD_HASH` never authenticates and blocks a production boot.

### 11.1 End-to-end acceptance harness

`scripts/acceptance.py` is the authoritative local acceptance run. It applies migrations with
`python -m app.cli init-db`, starts a real `uvicorn app.main:app`, and replays the checklist below
over HTTP (SQLite by default, or any DSN via `--database-url`), then runs `pytest` and a secret
scan of tracked files:

| Step | Check |
|---|---|
| 1–2 | migrations applied, app starts, `GET /health` → `ok` |
| 3–5 | `GET /register`, submit the form, member exists in the DB as `pending` |
| 6–7 | verification URL appears in the console email, clicking it verifies the member |
| 8–9 | the member is `verified` in the DB; reusing the same token is rejected |
| 10–12 | admin login, the member is visible in the admin list, CSV export succeeds |
| 13 | `POST /api/v1/members/register` over HTTP |
| 14 | `pytest -q` |
| 15 | secret scan (no real secrets in tracked files, `.env` untracked) |

It exits `0` only when every step passes and prints `LOCAL ACCEPTANCE: x/y steps passed`. The
default run leaves a throwaway `acceptance.db` in the repository root (git-ignored via `*.db`).

**Manual acceptance checklist** — the same flow by hand, useful when debugging:

1. `alembic upgrade head`, then `alembic current` → `0001_initial (head)`.
2. `python -m app.cli check-config` → no unexpected warnings; `WEBHOOK_ENABLED` / `META_ENABLED`
   match your intent.
3. `uvicorn app.main:app --reload`, open `/` (the landing page): hero, benefits, the embedded form
   and the contact band render, the header/hero CTA scrolls to `#dang-ky`, and submitting that form
   registers a member → redirected to `/check-email`; the console prints the `[EMAIL][console] …`
   block with a verification URL. `/register` still renders the standalone form.
4. Open the verification link → “Xác minh thành công”, and the member shows `verified` in the admin
   list; re-opening the same link → “Liên kết đã được sử dụng” (400).
5. `GET /health` → `{"status":"ok", …, "database":"ok"}`; `GET /api/v1/health` → envelope with
   `email_mode`, `webhook_enabled`, `meta_enabled`.
6. `POST /api/v1/members/register` → `201 duplicate:false`; repeat → `200 duplicate:true` with the
   same member id; `GET /api/v1/members?email=…` → the member.
7. With `MEMBER_API_KEY` set: the same call without `X-API-Key` → `401 unauthorized`.
8. Log into `/admin/login` → you land on `/admin/dashboard` (counters, 14-day chart, top UTM
   sources/campaigns, recent members). Filter/search on `/admin/members`, open a member, change the
   status or notes and save (`?msg=member_updated`), use “Gửi lại xác minh” on a `pending` member,
   then download `/admin/members.csv` → the export honours the filters and `X-Total-Rows`.
9. With `MEMBER_VERIFIED_WEBHOOK_URL`/`_SECRET` pointed at a local receiver: verifying a member
   delivers `member.verified` with a signature that `hmac.compare_digest` accepts.
10. `pytest -q` → green.

---

## 12. Security

Implemented controls (all verifiable in the code):

* **Server-side validation is authoritative** — `app/normalize.py` cleans every field
  (control-character stripping, whitespace collapsing, length caps, IDN-aware email validation via
  `email-validator`, digit-count phone validation). The browser check in `register.js` is UX only.
* **Normalisation before storage** — emails are stored lower-cased and unique; phones are reduced to
  digits with an optional leading `+`; UTM values and URLs are length-capped.
* **CSRF protection** — double-submit cookie (`member_csrf`, `HttpOnly`, `SameSite=Lax`, `Secure`
  when cookies are secure) validated in constant time on every HTML `POST`; JSON `/api/*` endpoints
  are exempt by design (they are not cookie-authenticated) and that exemption is documented.
* **scrypt admin password hash** — `hash_password()` uses scrypt (n=2¹⁴, r=8, p=1, dklen=32) with a
  random 16-byte salt and emits the **colon-separated** encoding
  `scrypt:<n>:<r>:<p>:<salt_b64>:<digest_b64>` (**no `$`**): a `$`-separated hash is interpolated away
  by Docker Compose `env_file` values (`$1`, `$16384` …) and expanded by shell `source`, silently
  corrupting it and locking the operator out of `/admin` — this happened in production, and
  `tests/test_security.py` now locks the encoding in. `verify_password()` still accepts legacy
  `$`-separated hashes, compares with `hmac.compare_digest` and never raises on a malformed hash;
  a **malformed** `ADMIN_PASSWORD_HASH` blocks startup in production when the admin UI is enabled.
  The plain password is never stored or logged.
* **One-time hashed verification tokens** — `secrets.token_urlsafe(32)`; only `sha256(token)` is
  persisted, so a database leak cannot be replayed. Tokens expire, are superseded when reissued, and
  are consumed by an atomic conditional `UPDATE` (lost races get `used`).
* **Hashed IPs only** — `SHA256(ip + IP_HASH_SALT)` for attribution and rate-limit keys; no raw IP
  in the database, in the webhook payload or in the Meta payload. `IP_HASH_SALT` is
  guard-required in production. Proxy headers are honoured **only** under the single-proxy contract
  of §4.4 (`TRUSTED_PROXY_HEADERS=true`): the app takes the rightmost `X-Forwarded-For` hop that the
  trusted proxy appended, then `X-Real-IP`, then the socket peer — so in a deployment where clients
  can send the header themselves, spoofing it cannot change their rate-limit bucket or `ip_hash`.
* **Rate limiting** — registration (HTML + API), `/api/v1/members*` and admin login, each keyed by
  the salted IP hash. `POST /api/v1/members/register` is checked against **both** the `register`
  scope (`REGISTER_RATE_LIMIT`) and the `api` scope (`API_RATE_LIMIT`), and the API
  `POST /api/v1/members/{id}/resend-verification` also carries a fixed per-member cap of 3 per hour
  (`resend:<member_id>` scope). The admin UI resend action (`POST
  /admin/members/{id}/resend-verification`) is **not** rate-capped: it requires an authenticated
  admin session + CSRF token and only re-sends for a `pending` member. Exceeding a limit returns `429` **on both
  branches** with a `Retry-After` header in seconds (JSON API: envelope with
  `error.code = "rate_limited"`; HTML: the Vietnamese error page). The in-process caveat for
  multi-worker deployments is documented in §10.
* **Security headers + per-request CSP** — every response carries `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy`, `Cross-Origin-Opener-Policy`, `Permissions-Policy` and
  HSTS when cookies are secure. The `Content-Security-Policy` is generated per request with a fresh
  random **nonce** (`script-src`/`style-src` include `'nonce-…'`, exposed to templates as
  `csp_nonce`) so the inline brand-colour style and the optional GA4/Meta bootstrap run without ever
  enabling `unsafe-inline`; `default-src 'self'`, `object-src 'none'` and `frame-ancestors 'none'`
  stay in force, and third-party hosts are allow-listed only when their id is configured.
* **Verification tokens and logs** — `RedactTokensFilter` in `app/main.py` rewrites `token=<value>`
  to `token=***` on the root, `uvicorn` and `uvicorn.access` loggers, so a one-time token does not
  land in journald/CI output even though it travels in the query string. In production
  (`EMAIL_MODE=smtp`) verification tokens are never written to logs; the local console backend
  **deliberately prints the raw verification link to stdout** (that is how the dev harness and the
  tests read it), and production refuses to start with it.
* **CSV formula-injection guard** — exported cells starting with `=`, `+`, `-`, `@`, TAB, CR or LF
  are prefixed with `'`; booleans/dates are normalised, URLs truncated, and the file carries a UTF-8
  BOM. The export is `Cache-Control: no-store` and records an `EXPORT` audit event.
* **Request size cap (preventive)** — `MaxBodySizeMiddleware` buffers the body and rejects anything
  over `MAX_REQUEST_BYTES` (256 KiB default) with `413` **before the handler ever runs**, using the
  fast `Content-Length` check plus the streamed byte count so a chunked body without
  `Content-Length` is caught too. A rejected request has **no side effects**: nothing is persisted.
* **API key auth** — `X-API-Key` compared with `hmac.compare_digest`. `MEMBER_API_KEY` is
  **mandatory in production** (the startup guard rejects an empty, placeholder or < 16-char value)
  because `/api/v1/members*` can return member PII and trigger verification emails; only `/health`
  and `/api/v1/health` stay open. Outside production, an empty key leaves the member endpoints open
  (documented, deliberate for local/dev).
* **No secrets in git** — `.gitignore` excludes `.env`/`.env.*` (keeping `.env.example`), `*.db`,
  caches and virtualenvs. `.env.example` contains only placeholders and documents how to generate
  real values. Logs mask secrets: the Meta access token and the webhook secret are scrubbed from
  recorded error strings; `check-config` masks `SECRET_KEY`, `IP_HASH_SALT`, `MEMBER_API_KEY` and DB
  credentials.
* **Defence in depth** — admin routes require a session and redirect anonymous browsers to
  `/admin/login`; admin pages send `noindex, nofollow` and `robots.txt` disallows `/admin` and
  `/verify-email`; unknown member ids and malformed UUIDs both return a plain `404`.

---

## 13. Related documents

| Document | Contents |
|---|---|
| [`deploy/README.md`](deploy/README.md) | **Operational runbook of the live deployment:** `/srv/member` layout, env files, Docker Compose stack, shared Caddy + TLS, updates, backups, rollback. |
| [`docs/API.md`](docs/API.md) | Complete endpoint reference: parameters, status codes, examples, error table, pagination/filter semantics, versioning policy. |
| [`docs/INTEGRATION.md`](docs/INTEGRATION.md) | How to plug MEMBER into VIPORDER / VIP GROUP / VIP AI / Marketing Hub: integration styles, sequence diagrams, idempotency, tenant setup, Python client example. |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Host-level production recipe: PostgreSQL, systemd, nginx + TLS, backups, monitoring, rollback. |
| [`docs/INTERNAL_CONTRACT.md`](docs/INTERNAL_CONTRACT.md) | Frozen internal interfaces between modules (service signatures, envelope, HTTP surface). |
| `.env.example` | Every environment variable with inline comments. |
