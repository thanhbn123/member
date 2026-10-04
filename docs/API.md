# API.md — MEMBER service endpoint reference

Complete reference for every HTTP endpoint served by the MEMBER service, including the JSON API
(`/api/v1`), the browser surface and the admin UI.

* **Framework:** FastAPI (`app/main.py::create_app`), interactive docs at `/docs`, OpenAPI JSON at
  `/openapi.json`.
* **Version:** `0.1.0` (`app/__init__.py::__version__`, also reported by `GET /health`).
* **Verification status:** every request, response, status code and header shown below was captured
  from a running instance (SQLite, `EMAIL_MODE=console`) while writing this document. Where a value
  is environment specific (`request_id`, ids, timestamps, host names) it is real-shaped but
  illustrative.
* **Base URL:** `PUBLIC_BASE_URL` (default `http://localhost:8000`). All paths below are absolute.

---

## 0. Conventions

### 0.1 The `/api/v1` envelope

Every `/api/v1/*` response — success **and** failure — is a JSON object with exactly four
top-level keys:

```json
{
  "success": true,
  "data": { },
  "error": null,
  "meta": {"request_id": "947133a75784483c97c475c0a5a5d435"}
}
```

```json
{
  "success": false,
  "data": null,
  "error": {"code": "validation_error", "message": "…", "details": [ ]},
  "meta": {"request_id": "41056ffefa614b3fb4c5c385d81d95db"}
}
```

| Key | Type | Notes |
|---|---|---|
| `success` | boolean | `true` for 2xx responses, `false` otherwise. |
| `data` | object \| null | Payload on success, `null` on error. |
| `error` | object \| null | `null` on success; otherwise `{"code", "message", "details"}`. |
| `meta` | object | Normally `{"request_id": "<uuid4-hex>"}`. Empty (`{}`) for `/api/v1/health` and for the middleware-generated `413`. |

The same id is echoed in the `X-Request-ID` response header and appears in the server log line for
the request, so a support report can be correlated with logs.

### 0.2 Authentication

| Surface | Auth |
|---|---|
| `/api/v1/members*` | Optional shared key. **When `MEMBER_API_KEY` is set**, the header `X-API-Key: <key>` is mandatory; comparison is constant-time (`hmac.compare_digest`). Missing or wrong → `401 unauthorized`. When `MEMBER_API_KEY` is empty the endpoints are open (documented, deliberate for local/dev). |
| `/api/v1/health`, `/health` | Never authenticated. |
| `/api/v1/*` CSRF | Not applicable — JSON API endpoints are exempt from CSRF by design (`CSRF_EXEMPT_PREFIXES = ("/api/",)`); they are not cookie-authenticated. |
| `/admin/*` | Session cookie set by `POST /admin/login`; every other admin route redirects anonymous browsers (HTTP `303`) to `<PUBLIC_BASE_URL>/admin/login`. Requires `ADMIN_EMAIL` **and** `ADMIN_PASSWORD_HASH`. |
| HTML `POST` (`/register`, `/admin/login`, `/admin/logout`) | Double-submit CSRF token: form field `csrf_token` must match the `member_csrf` cookie. Missing/mismatched → `403`. |

### 0.3 CORS

CORS middleware is registered **only** when `CORS_ALLOW_ORIGINS` is non-empty (comma or semicolon
separated):

* `allow_origins` = the configured list, `allow_credentials=False`,
* `allow_methods=["GET", "POST", "OPTIONS"]`,
* `allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"]`,
* `max_age=600`.

### 0.4 Request body size

Bodies larger than `MAX_REQUEST_BYTES` (default `262144` = 256 KiB) are rejected with
`413 payload_too_large` by `MaxBodySizeMiddleware` **before** routing. The check uses both the
`Content-Length` header and the actual streamed byte count. Note that the `413` envelope is
produced by the middleware and therefore has `"meta": {}` (no `request_id`).

### 0.5 Rate limiting

Counters are in-process (per worker). Keys are `<scope>:sha256(client_ip + IP_HASH_SALT)`.

| Scope | Setting | Default | Applies to |
|---|---|---|---|
| `register` | `REGISTER_RATE_LIMIT` / `REGISTER_RATE_WINDOW_SECONDS` | 10 / 3600 s | `POST /register` (HTML) **and** `POST /api/v1/members/register` |
| `api` | `API_RATE_LIMIT` / `API_RATE_WINDOW_SECONDS` | 60 / 60 s | `GET/POST /api/v1/members*` |
| `login` | `LOGIN_RATE_LIMIT` / `LOGIN_RATE_WINDOW_SECONDS` | 10 / 900 s | `POST /admin/login` |

On `429`, **both branches** carry a `Retry-After` header (seconds until the window frees up): the
JSON API returns the envelope with `error.code = "rate_limited"`, the HTML surface returns the
Vietnamese error page. Verified: `POST /register` over the limit → `429` + `retry-after: 3600`;
`GET /api/v1/members` over the limit → `429` + `retry-after: 60`.

`X-Forwarded-For` / `X-Real-IP` are honoured **only** when `TRUSTED_PROXY_HEADERS=true`.

### 0.6 Idempotency summary

| Situation | Result |
|---|---|
| `POST /api/v1/members/register` with a **new** email | `201`, `data.duplicate = false`, `data.verification_sent = true` |
| … with an existing **pending** member | `200`, `data.duplicate = true`, same `member.id`, previous token invalidated, a fresh verification email sent (`verification_sent = true`) |
| … with an existing **verified** member | `200`, `data.duplicate = true`, `verification_sent = false`, no email, no token |
| `POST /api/v1/members/{id}/resend-verification` for a **verified** member | `200`, `verification_sent = false`, `error = "already_verified"` |
| Any `/api/v1/members*` call without `X-API-Key` while `MEMBER_API_KEY` is set | `401 unauthorized` |

`email` (lower-cased) is the natural unique key — `members.email` carries a unique index.

---

## 1. Endpoint index

| Method | Path | Surface | Auth | Success |
|---|---|---|---|---|
| `GET` | `/` | HTML | — | `307` → `/register` |
| `GET` | `/health` | probe | — | `200` plain JSON |
| `GET` | `/register` | HTML | — | `200` form |
| `POST` | `/register` | HTML | CSRF | `303` → `/check-email` |
| `GET` | `/check-email` | HTML | — | `200` |
| `GET` | `/verify-email` | HTML | — | `200` success, `400` invalid/used, `410` expired |
| `GET` | `/welcome` | HTML | — | `200` |
| `GET` | `/robots.txt` | text | — | `200` |
| `GET` | `/docs`, `/openapi.json` | docs | — | `200` (or `404` when `API_DOCS_ENABLED=false`) |
| `/static/*` | mounted `StaticFiles` | static | — | `200` |
| `GET` | `/api/v1/health` | JSON API | — | `200` |
| `POST` | `/api/v1/members/register` | JSON API | `X-API-Key`* | `201` new / `200` duplicate |
| `GET` | `/api/v1/members/{member_id}` | JSON API | `X-API-Key`* | `200` |
| `GET` | `/api/v1/members?email=…` | JSON API | `X-API-Key`* | `200` |
| `POST` | `/api/v1/members/{member_id}/resend-verification` | JSON API | `X-API-Key`* | `200` |
| `GET` | `/admin` | admin | — | `303` → `/admin/members` (or `/admin/login`) |
| `GET` | `/admin/` | admin | — | `303` → `/admin/members` (or `/admin/login`) |
| `GET` | `/admin/login` | admin | — | `200` (or `303` when already logged in) |
| `POST` | `/admin/login` | admin | CSRF | `303` → `/admin/members` |
| `POST` | `/admin/logout` | admin | CSRF | `303` → `/admin/login` |
| `GET` | `/admin/members` | admin | session | `200` |
| `GET` | `/admin/members.csv` | admin | session | `200` `text/csv` |
| `GET` | `/admin/members/{member_id}` | admin | session | `200` (or `404` page) |

\* required only when `MEMBER_API_KEY` is set.

---

## 2. Probes

### 2.1 `GET /health`

Plain JSON, no envelope, no secrets. Always unauthenticated. Not part of the OpenAPI schema.

```bash
curl -s http://localhost:8000/health
```

**`200 OK`**

```json
{
  "status": "ok",
  "app": "MEMBER",
  "env": "local",
  "version": "0.1.0",
  "database": "ok"
}
```

| Field | Meaning |
|---|---|
| `status` | `"ok"` when `SELECT 1` succeeds, otherwise `"degraded"`. |
| `app` | `APP_NAME`. |
| `env` | `APP_ENV` (`local` \| `staging` \| `production`). |
| `version` | `app.__version__`. |
| `database` | `"ok"` \| `"error"`. |

### 2.2 `GET /api/v1/health`

Envelope, capability flags, never authenticated.

```bash
curl -s http://localhost:8000/api/v1/health
```

**`200 OK`**

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

| Field | Meaning |
|---|---|
| `status` / `database` | As §2.1 (`degraded` / `error` on a broken database). |
| `email_mode` | `console` \| `smtp` — whether real emails are being delivered. |
| `webhook_enabled` | `true` only when `MEMBER_VERIFIED_WEBHOOK_URL` **and** `MEMBER_VERIFIED_WEBHOOK_SECRET` are set. |
| `meta_enabled` | `true` only when `META_PIXEL_ID` **and** `META_ACCESS_TOKEN` are set. |
| `api_key_required` | `true` when `MEMBER_API_KEY` is set (i.e. callers must send `X-API-Key`). |

> `api_key_required` lets an integrator detect a misconfigured deployment: a `false` value on a
> public host means the member endpoints are unauthenticated.

---

## 3. JSON API — members

All four member endpoints share the same rate limit (`API_RATE_LIMIT`) and the same optional
`X-API-Key` requirement.

### 3.1 `POST /api/v1/members/register`

Registers a member (creating a `pending` record) and sends the verification email. Also subject to
`REGISTER_RATE_LIMIT`.

**Status codes**

| Code | Meaning |
|---|---|
| `201 Created` | A new member was created (`data.duplicate = false`). |
| `200 OK` | The email already existed (`data.duplicate = true`); the member was re-notified or left untouched if already verified. |
| `401 Unauthorized` | `MEMBER_API_KEY` set and `X-API-Key` missing/wrong. |
| `413 Payload Too Large` | Body larger than `MAX_REQUEST_BYTES`. |
| `422 Unprocessable Entity` | Body validation failed (missing/extra/oversized/blank field) or server-side normalisation failed (invalid email/phone). `error.details` is always a non-empty list. |
| `429 Too Many Requests` | `REGISTER_RATE_LIMIT` (or `API_RATE_LIMIT`) exceeded; `Retry-After` header set. |
| `500 Internal Server Error` | Unhandled error; `internal_error`. |

**Body parameters** (`application/json`, `extra="forbid"`, strings are stripped of surrounding
whitespace):

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `full_name` | string | **yes** | 1–200 chars | Control characters stripped, whitespace collapsed server-side. |
| `email` | string | **yes** | 3–320 chars | Normalised to lower case; IDN-aware validation. The natural unique key. |
| `phone` | string \| null | no | ≤ 64 chars | Stored as digits with an optional leading `+`; must contain 8–15 digits. |
| `company` | string \| null | no | ≤ 200 chars | |
| `consent_marketing` | boolean | no | default `false` | Marketing consent. |
| `utm_source` | string \| null | no | ≤ 255 chars | Attribution. |
| `utm_medium` | string \| null | no | ≤ 255 chars | Attribution. |
| `utm_campaign` | string \| null | no | ≤ 255 chars | Attribution. |
| `utm_content` | string \| null | no | ≤ 255 chars | Attribution. |
| `utm_term` | string \| null | no | ≤ 255 chars | Attribution. |
| `landing_url` | string \| null | no | ≤ 2048 chars | Attribution metadata; never rendered as HTML. Defaults to the request URL when absent. |
| `referrer` | string \| null | no | ≤ 2048 chars | Attribution metadata; falls back to the `Referer` header. |
| `fbp` | string \| null | no | ≤ 255 chars | Meta browser cookie; falls back to the `_fbp` cookie. |
| `fbc` | string \| null | no | ≤ 255 chars | Meta click cookie; falls back to `_fbc`, then to `fb.1.<ms>.<fbclid>`. |
| `fbclid` | string \| null | no | ≤ 255 chars | Meta click id; used to derive `fbc` only when `fbc`/`_fbc` are absent. |
| `source` | string \| null | no | ≤ 50 chars | `web_form`, `api`, `viporder`, `vipgroup`, `import`, `other`. Unrecognised values are stored as `other`; default `api`. |

Every other key is rejected (`422`, `"Extra inputs are not permitted"`).

**Example — new member**

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

**Example — same request again, member still pending** → `200 OK`

```json
{
  "success": true,
  "data": {
    "member": { "id": "f9b4dba4-9fa3-4f5b-99d1-e06a5e9dc36e", "status": "pending", "…": "…" },
    "duplicate": true,
    "verification_sent": true,
    "email_error": null
  },
  "error": null,
  "meta": {"request_id": "bf1b4033c7fa455e921dce82fbdaa118"}
}
```

**Example — same request after the member verified** → `200 OK`

```json
{
  "success": true,
  "data": {
    "member": {
      "id": "7d1fffb3-108c-4aa9-8465-3ef19db94a05",
      "full_name": "Trần Thị B",
      "email": "b@example.com",
      "phone": null,
      "company": null,
      "status": "verified",
      "consent_marketing": false,
      "email_verified_at": "2026-10-04T06:23:43.778584Z",
      "created_at": "2026-10-04T06:23:43.771403Z",
      "updated_at": "2026-10-04T06:23:43.780844Z"
    },
    "duplicate": true,
    "verification_sent": false,
    "email_error": null
  },
  "error": null,
  "meta": {"request_id": "fbe63d7b06f34990bdd788fe0bd99b43"}
}
```

**Response fields**

| Field | Meaning |
|---|---|
| `data.member` | `MemberOut` (no `attribution` — use §3.2/§3.3 for that). |
| `data.duplicate` | `true` when the normalised email already existed. |
| `data.verification_sent` | `true` when an email was actually handed to the backend during this call. |
| `data.email_error` | Backend error string when `verification_sent` is `false` for a delivery reason (`null` otherwise). A member that is already verified reports `verification_sent: false` with `email_error: null`. |

`data.member.status` is one of `pending`, `verified`, `unsubscribed`, `blocked`.

**Error example — `422`, schema violation** (Pydantic body validation)

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

**Error example — `422`, normalisation failure** (body passed the schema, value rejected server-side)

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

`error.details` is **always** a non-empty list of `{"field", "message"}`:

* schema violations → one entry per offending field, `field` is the dotted path without the `body`
  prefix (e.g. `full_name`, `utm_source`), `message` is Pydantic’s English text, and the top-level
  `message` is the generic `Dữ liệu gửi lên không hợp lệ.`;
* normalisation failures → a single entry whose `field` is the machine field name (`email`,
  `phone`, `utm`, `url`, `fbclid`, or `body` when it cannot be attributed) and both the top-level
  `message` and the detail carry the user-facing Vietnamese reason — the same strings the HTML form
  renders.

Observed normalisation messages: `Email không hợp lệ`, `Email là bắt buộc`,
`Số điện thoại không hợp lệ`, `Số điện thoại phải có từ 8 đến 15 chữ số`.

**Error example — `401`** (only when `MEMBER_API_KEY` is set)

```json
{
  "success": false,
  "data": null,
  "error": {
    "code": "unauthorized",
    "message": "API key không hợp lệ hoặc thiếu header X-API-Key.",
    "details": null
  },
  "meta": {"request_id": "435086e343da4215a4b703907fffcc22"}
}
```

### 3.2 `GET /api/v1/members/{member_id}`

Fetch one member by UUID.

| Parameter | In | Type | Required | Notes |
|---|---|---|---|---|
| `member_id` | path | UUID string | **yes** | A malformed value returns `404`, not `422`. |

**Status codes:** `200`, `401`, `404` (unknown or malformed id), `429`, `500`.

```bash
curl -s http://localhost:8000/api/v1/members/f9b4dba4-9fa3-4f5b-99d1-e06a5e9dc36e \
  -H "X-API-Key: $MEMBER_API_KEY"
```

**`200 OK`** — `data` is a `MemberDetailOut`:

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

| Field | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key; stable across duplicate registrations. |
| `full_name`, `email`, `phone`, `company` | string \| null | Normalised values (`email` lower-cased). |
| `status` | string | `pending` \| `verified` \| `unsubscribed` \| `blocked`. |
| `consent_marketing` | boolean | |
| `email_verified_at` | ISO-8601 \| null | Set when the token was consumed. |
| `created_at`, `updated_at` | ISO-8601 UTC | `…Z`-suffixed, aware UTC. |
| `attribution` | object \| null | `null` when nothing was captured. |
| `attribution.ip_hash` | string \| null | `sha256(client_ip + IP_HASH_SALT)` — a **hash**, never a raw IP (see README §5.3). `user_agent` is stored but intentionally not exposed here. |

**`404 Not Found`**

```json
{
  "success": false,
  "data": null,
  "error": {"code": "not_found", "message": "Member không tồn tại.", "details": null},
  "meta": {"request_id": "07f2443db4a34c0e9ccc2edb4eaafe1a"}
}
```

### 3.3 `GET /api/v1/members?email=…` (lookup by email)

The idempotency helper: resolve a member from the natural unique key instead of the UUID.

| Parameter | In | Type | Required | Notes |
|---|---|---|---|---|
| `email` | query | string | **yes** | Normalised (lower-cased) before the lookup, so any casing works. |

**Status codes:** `200`, `401`, `404` (no match), `422` (missing `email` query parameter), `429`,
`500`.

```bash
curl -s -G http://localhost:8000/api/v1/members \
  --data-urlencode "email=Nguyen.Van.A@Example.com" \
  -H "X-API-Key: $MEMBER_API_KEY"
```

**`200 OK`** — identical body to §3.2 (`MemberDetailOut`, including `attribution`).

**`404 Not Found`** — same envelope as §3.2 with `not_found`.

> `GET /api/v1/members` without the `email` parameter is a `422 validation_error`, not a list
> endpoint. There is no public paginated member list — use the admin CSV (§5.3) for bulk exports.

### 3.4 `POST /api/v1/members/{member_id}/resend-verification`

Issues a new one-time token (invalidating any previous unused one) and re-sends the verification
email. No request body.

| Parameter | In | Type | Required | Notes |
|---|---|---|---|---|
| `member_id` | path | UUID string | **yes** | Unknown or malformed → `404`. |

**Status codes:** `200`, `401`, `404`, `429`, `500`. `200` is returned even when nothing was sent —
inspect `data.verification_sent` / `data.error`.

```bash
curl -s -X POST \
  http://localhost:8000/api/v1/members/f9b4dba4-9fa3-4f5b-99d1-e06a5e9dc36e/resend-verification \
  -H "X-API-Key: $MEMBER_API_KEY"
```

**`200 OK` — pending member (email sent)**

```json
{
  "success": true,
  "data": {
    "member_id": "f9b4dba4-9fa3-4f5b-99d1-e06a5e9dc36e",
    "verification_sent": true,
    "error": null
  },
  "error": null,
  "meta": {"request_id": "e1802206ff354ae7afce7013939229b8"}
}
```

**`200 OK` — already verified**

```json
{
  "success": true,
  "data": {
    "member_id": "7d1fffb3-108c-4aa9-8465-3ef19db94a05",
    "verification_sent": false,
    "error": "already_verified"
  },
  "error": null,
  "meta": {"request_id": "…"}
}
```

**`data.error` is not an envelope error code.** It is a *data* field describing why the send did
not happen:

| `data.error` | Meaning |
|---|---|
| `null` | Email handed to the backend successfully. |
| `"already_verified"` | The member is already verified; nothing to resend. |
| `"SMTP_HOST is not configured"` | `EMAIL_MODE=smtp` without `SMTP_HOST`. |
| any other string | Verbatim `smtplib`/socket error (e.g. authentication failure, timeout). |

### 3.5 Error-code table (all `/api/v1` endpoints)

| HTTP | `error.code` | `error.message` (default) | Triggered by | `Retry-After` |
|---|---|---|---|---|
| `401` | `unauthorized` | `API key không hợp lệ hoặc thiếu header X-API-Key.` | `require_api_key` when `MEMBER_API_KEY` is set and the header is missing/wrong. | — |
| `404` | `not_found` | `Member không tồn tại.` | Unknown/malformed UUID, or email lookup miss. | — |
| `405` | `method_not_allowed` | `Method Not Allowed` | Wrong method on a known path. | — |
| `413` | `payload_too_large` | `Request body exceeds <MAX_REQUEST_BYTES> bytes` | Body over the cap (middleware; `meta` is `{}`). | — |
| `422` | `validation_error` | `Dữ liệu gửi lên không hợp lệ.` (schema) / the Vietnamese reason (normalisation) | Pydantic body validation or `NormalizationError`. `details` is **always** a non-empty `[{"field", "message"}]` list. | — |
| `429` | `rate_limited` | `Bạn đã gửi quá nhiều yêu cầu. Vui lòng thử lại sau.` | Rate limit exceeded (`REGISTER_RATE_LIMIT`, `API_RATE_LIMIT`). | yes |
| `500` | `internal_error` | `Đã xảy ra lỗi hệ thống.` | Unhandled exception. | — |
| *other* | `request_failed` | *(framework detail)* | Any other `HTTPException` (e.g. `403` raised outside the HTML path). | — |

Codes listed in `docs/INTERNAL_CONTRACT.md` (`email_already_verified`, `registration_failed`) are
**not** emitted by the current implementation: “already verified” is reported as
`success: true` with `data.error = "already_verified"` (§3.4), and a failed registration is a
`422 validation_error` or a `201`/`200` with `data.verification_sent = false`.

### 3.6 Response schema reference

| Schema | Fields |
|---|---|
| `MemberOut` | `id`, `full_name`, `email`, `phone`, `company`, `status`, `consent_marketing`, `email_verified_at`, `created_at`, `updated_at` |
| `MemberDetailOut` | `MemberOut` + `attribution` |
| `AttributionOut` | `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`, `landing_url`, `referrer`, `fbp`, `fbc`, `ip_hash`, `created_at` |
| Register `data` | `member` (`MemberOut`), `duplicate`, `verification_sent`, `email_error` |
| Resend `data` | `member_id`, `verification_sent`, `error` |

---

## 4. Browser surface (HTML)

All HTML routes render Jinja2 templates (`app/templates/`), are driven by `BRAND_*` configuration
and are excluded from the OpenAPI schema.

### 4.1 `GET /` → `307 Temporary Redirect` to `/register`

### 4.2 `GET /register` → `200`

Renders the registration form. Side effects: issues the `member_csrf` cookie when absent
(`HttpOnly`, `SameSite=Lax`, `Secure` when cookies are secure) and prefills the hidden attribution
inputs from the query string, cookies and headers (so attribution works without JavaScript).

**Query parameters** (all optional, all standard UTM/click-id names): `utm_source`, `utm_medium`,
`utm_campaign`, `utm_content`, `utm_term`, `fbclid`. They are captured as attribution, not rendered
as visible content.

### 4.3 `POST /register` → `303` or `422`

`application/x-www-form-urlencoded`. Requires the CSRF token and is subject to
`REGISTER_RATE_LIMIT`.

| Form field | Required | Notes |
|---|---|---|
| `csrf_token` | **yes** | Must equal the `member_csrf` cookie; otherwise `403`. |
| `full_name` | **yes** | ≤ 200 chars. |
| `email` | **yes** | ≤ 320 chars. |
| `phone` | no | ≤ 64 chars. |
| `company` | no | ≤ 200 chars. |
| `consent_marketing` | no | Checkbox; `1`/`true`/`on`/`yes` count as consent. |
| `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`, `landing_url`, `referrer`, `fbp`, `fbc` | no | Hidden fields; **query string / cookie / header values take precedence** over submitted ones. |

| Response | Meaning |
|---|---|
| `303 See Other` → `/check-email?email=<masked>` | Registration accepted. `&sent=0` is appended when the verification email could not be delivered. |
| `422 Unprocessable Entity` | Re-renders the form with the submitted values and a Vietnamese error list. |
| `403 Forbidden` | CSRF token missing/mismatched. |
| `429 Too Many Requests` | Register rate limit exceeded (error page + `Retry-After`). |

The redirect only carries a **masked** address (`c***@example.com`), never the full email.

### 4.4 `GET /check-email` → `200`

“Check your inbox” page.

| Query parameter | Default | Meaning |
|---|---|---|
| `email` | `""` | Masked address to display. |
| `sent` | `1` | `0` renders the “we could not send the email” variant. |

### 4.5 `GET /verify-email?token=…`

Consumes the one-time token (see README §2.3 for the full state machine).

| `token` | Outcome | Status |
|---|---|---|
| valid | member set to `verified`, `EMAIL_VERIFIED` recorded, post-commit webhook/Meta fan-out | `200` “Xác minh thành công” |
| missing / unknown / tampered | “Liên kết không hợp lệ” | `400` |
| already consumed | “Liên kết đã được sử dụng” | `400` |
| expired (`expires_at <= now`) | “Liên kết đã hết hạn” | `410 Gone` |

Tokens are single-use and issuing a new one invalidates the previous link. Verification can never be
rolled back or blocked by email/webhook/Meta failures.

### 4.6 `GET /welcome` → `200`

Welcome page for the member verified in this browser session (`session["last_member_id"]`); renders
without a member when the session has none.

### 4.7 `GET /robots.txt` → `200` `text/plain`

```
User-agent: *
Disallow: /admin
Disallow: /verify-email
```

### 4.8 `GET /docs`, `GET /openapi.json`

FastAPI’s interactive documentation and the OpenAPI 3 schema. `/docs` is enabled by default,
`/redoc` is not. HTML-only routes use `include_in_schema=False`, so the schema contains the
`/api/v1` endpoints.

**`API_DOCS_ENABLED`** (default `true`) controls both: when it is set to `false`, the app registers
neither `docs_url` nor `openapi_url`, so `GET /docs` and `GET /openapi.json` return `404` while every
`/api/v1` endpoint keeps working unchanged. Recommended in production (`API_DOCS_ENABLED=false`), or
keep it enabled and restrict `/docs` at the reverse proxy — the schema reveals the API surface even
though it never contains secrets.

### 4.9 `/static/*`

Static assets: `css/app.css`, `js/register.js` (plus the `.pixel-noscript` helper class used by the
optional Meta Pixel `<noscript>` image).

---

## 5. Admin UI

Requires `ADMIN_EMAIL` + `ADMIN_PASSWORD_HASH`. Anonymous requests to any `/admin/*` page receive
`303` with `Location: <PUBLIC_BASE_URL>/admin/login`. Admin pages send `noindex, nofollow`.

### 5.0 `GET /admin`, `GET /admin/` → `303`

Convenience entry points with no body of their own: logged-in sessions are redirected to
`/admin/members`, anonymous visitors to `/admin/login` (both `303 See Other`).

### 5.1 `GET /admin/login` → `200` (or `303` when already logged in)

Renders the login form (CSRF-protected). Shows a masked hint of the configured admin address
(`a***@example.com`) and hides the form when the admin UI is not configured.

### 5.2 `POST /admin/login` → `303` or `401`

`application/x-www-form-urlencoded`, CSRF required, subject to `LOGIN_RATE_LIMIT`.

| Form field | Required | Notes |
|---|---|---|
| `csrf_token` | **yes** | Must equal the `member_csrf` cookie; otherwise `403`. |
| `email` | **yes** | Compared case-insensitively with `ADMIN_EMAIL`. |
| `password` | **yes** | Verified against `ADMIN_PASSWORD_HASH` with scrypt + `hmac.compare_digest`. |

| Response | Meaning |
|---|---|
| `303 See Other` → `/admin/members` | Login succeeded; the session is marked authenticated and a `LOGIN` event (`success: true`, `ip_hash`) is recorded. |
| `401 Unauthorized` | Wrong credentials; the form is re-rendered and a `LOGIN` event (`success: false`, `actor`, `ip_hash`) is recorded. |
| `403 Forbidden` | CSRF failure. |
| `429 Too Many Requests` | Login rate limit exceeded. |

### 5.3 `GET /admin/members` → `200`

Paginated, filterable member list. Every filter is also passed through to the CSV export.

| Query parameter | Type | Default | Semantics |
|---|---|---|---|
| `q` | string | *(none)* | Case-insensitive substring match (`LIKE %q%`) across `email`, `full_name`, `phone`, `company`. Truncated to 100 chars. |
| `status` | enum | *(none)* | Exact match on `pending` \| `verified` \| `unsubscribed` \| `blocked`. Any other value is ignored (treated as “all”). |
| `utm_source` | string | *(none)* | Exact match on the attribution `utm_source`; members without attribution are excluded. Truncated to 255 chars. |
| `date_from` | `YYYY-MM-DD` | *(none)* | `members.created_at >= 00:00:00 UTC` of that day. Invalid dates are ignored. |
| `date_to` | `YYYY-MM-DD` | *(none)* | `members.created_at <= 23:59:59.999999 UTC` of that day. Invalid dates are ignored. |
| `page` | integer ≥ 1 | `1` | 1-based page number; invalid input falls back to `1`. |
| `per_page` | `10` \| `25` \| `50` \| `100` | `25` | Any other value falls back to `25`. Accepted by the query parser; the admin UI dropdown offers `25/50/100`. |

* **Sort order** is fixed: `created_at DESC, id DESC` (newest first). There is no `sort` parameter.
* **Pagination metadata** is rendered into the page: `total`, `page`, `per_page`, `pages`
  (`ceil(total / per_page)`, minimum 1).
* The filter dropdown for `utm_source` is populated from the distinct non-null values present in
  `member_attribution` (top 100 by member count).

### 5.4 `GET /admin/members.csv` → `200` `text/csv; charset=utf-8`

Streams **all** members matching the current filters, ignoring pagination: the handler forces
`per_page = 100000` and `page = 1`, so `page` and `per_page` in the query string are accepted but
have no effect on the exported rows.

| Response header | Value |
|---|---|
| `Content-Type` | `text/csv; charset=utf-8` |
| `Content-Disposition` | `attachment; filename="members-YYYYMMDD-HHMMSS.csv"` (UTC) |
| `X-Total-Rows` | number of exported rows |
| `Cache-Control` | `no-store` |

**Filters:** identical to §5.3 (`q`, `status`, `utm_source`, `date_from`, `date_to`). The admin UI’s
“Xuất CSV” link carries the active filters only — never `page`/`per_page`.

**Columns (in order):**

```
id, full_name, email, phone, company, status, consent_marketing, email_verified_at, created_at,
source, utm_source, utm_medium, utm_campaign, utm_content, utm_term, landing_url, referrer, fbp, fbc
```

**Formatting and safety rules:**

* A UTF-8 BOM (`\ufeff`) is written first so Excel detects UTF-8 and renders Vietnamese text.
* Line terminator is CRLF; fields are minimally quoted per RFC 4180.
* Booleans render as `true`/`false`; datetimes render as `YYYY-MM-DD HH:MM:SS` in UTC.
* `landing_url` and `referrer` are truncated to 500 characters (with a trailing `…`).
* **Formula-injection guard:** any cell whose trimmed value starts with `=`, `+`, `-`, `@`, TAB, CR
  or LF **and** is not a plain number is prefixed with an apostrophe (`'`) so spreadsheets treat it
  as text. Columns are never HTML-escaped — the file is data, not markup.
* Each export writes an `EXPORT` event (`actor`, `rows`, `filters`, `format: "csv"`).

**Example**

```bash
curl -s -b cookies.txt -o members.csv \
  "http://localhost:8000/admin/members.csv?status=pending&utm_source=viporder&date_from=2026-01-01"
```

```http
HTTP/1.1 200 OK
content-type: text/csv; charset=utf-8
content-disposition: attachment; filename="members-20261004-062350.csv"
x-total-rows: 1
cache-control: no-store
```

```csv
id,full_name,email,phone,company,status,consent_marketing,email_verified_at,created_at,source,utm_source,utm_medium,utm_campaign,utm_content,utm_term,landing_url,referrer,fbp,fbc
531ba8da-b22d-41c0-8e0f-46c264d6fe27,Lê Văn C,c@example.com,0912345678,,pending,true,,2026-10-04 06:23:50,web_form,,,,,,http://testserver/register,,,
```

### 5.5 `GET /admin/members/{member_id}` → `200` or `404` page

Member detail: identity, status, consent, attribution (`fbp`, `fbc`, UTM, landing URL, referrer,
`ip_hash`) and the **200 most recent** events for that member, newest first. An unknown member
renders the error template with `404` (HTML, not the JSON envelope).

### 5.6 `POST /admin/logout` → `303` to `/admin/login`

Clears the session. CSRF-protected.

---

## 6. Outbound calls (for receivers)

These are not endpoints of this service but are part of its contract with other systems.

### 6.1 `member.verified` webhook

`POST <MEMBER_VERIFIED_WEBHOOK_URL>`, fired after the verification commit.

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `X-Member-Event` | `member.verified` |
| `X-Member-Timestamp` | ISO-8601 UTC, recomputed per attempt |
| `X-Member-Signature` | `sha256=<hmac(secret, "<timestamp>.<raw body>")>` |
| `X-Member-Delivery` | UUID, stable across retries |

Body and verification snippet: README §8 and `docs/INTEGRATION.md` §4. Retries:
`WEBHOOK_MAX_ATTEMPTS` (3) with backoff `WEBHOOK_BACKOFF_SECONDS × 2^(attempt-1)`, timeout
`WEBHOOK_TIMEOUT_SECONDS` per attempt. A receiver must return 2xx quickly.

### 6.2 Meta Conversions API

`POST https://graph.facebook.com/<META_API_VERSION>/<META_PIXEL_ID>/events?access_token=…` with
`{"data": [<CompleteRegistration event>]}`, plus `test_event_code` when
`META_TEST_EVENT_CODE` is set. Only sent when `META_PIXEL_ID` **and** `META_ACCESS_TOKEN` are both
configured. `user_data` carries SHA-256 hashes of email/phone plus `fbp`/`fbc`/`client_user_agent`;
raw IPs are never included. Details: README §9.

---

## 7. Changelog and versioning

### 7.1 Policy

**`/api/v1` is frozen.** The following are guaranteed for the lifetime of the version:

* existing paths, methods and the four-key envelope (`success`, `data`, `error`, `meta`);
* the meaning of every documented status code, including the **201-vs-200 duplicate semantics** of
  `POST /api/v1/members/register`;
* the semantics of `data.duplicate`, `data.verification_sent`, `data.email_error` and
  `data.error = "already_verified"`;
* field names and types of the existing `data` objects; `email` and `id` remain the natural and
  primary keys.

Allowed **without** a version bump (backwards compatible):

* adding new **optional** request fields;
* adding new fields to `data` objects (clients must ignore unknown fields);
* adding new endpoints under `/api/v1`;
* adding new values to `error.code` for genuinely new failure modes;
* improving messages (never rely on `error.message` text — branch on `error.code`).

**Breaking changes require `/api/v2`**, served side by side with `/api/v1`:

* removing or renaming a field, or changing its type/format;
* changing a status code (e.g. always returning `201` on register) or the duplicate semantics;
* changing authentication (`X-API-Key`) or rate-limit behaviour in a way that breaks callers;
* changing the envelope shape.

When `/api/v2` is introduced, `/api/v1` keeps working until every integrator has migrated; both
versions share the same database and services, and the deprecation is announced in this section plus
a `Deprecation`/`Sunset` response header on the old paths.

### 7.2 Changelog

| Version | Date | Changes |
|---|---|---|
| `0.1.0` / API `v1` | 2026-10-04 | Initial frozen surface: `/api/v1/health`, `POST /api/v1/members/register` (201/200), `GET /api/v1/members/{member_id}`, `GET /api/v1/members?email=`, `POST /api/v1/members/{member_id}/resend-verification`; envelope + error-code table; optional `X-API-Key`; per-IP rate limits; signed `member.verified` webhook; opt-in Meta Conversions API; admin UI with CSV export; single migration `0001_initial`. |

---

## 8. Curl quick reference

```bash
BASE=http://localhost:8000
KEY=$MEMBER_API_KEY          # omit the header if MEMBER_API_KEY is empty

curl -s $BASE/health
curl -s $BASE/api/v1/health

curl -s -X POST $BASE/api/v1/members/register \
  -H "Content-Type: application/json" -H "X-API-Key: $KEY" \
  -d '{"full_name":"Nguyễn Văn A","email":"a@example.com","source":"viporder"}'

curl -s "$BASE/api/v1/members?email=a@example.com" -H "X-API-Key: $KEY"
curl -s $BASE/api/v1/members/<member-uuid> -H "X-API-Key: $KEY"
curl -s -X POST $BASE/api/v1/members/<member-uuid>/resend-verification -H "X-API-Key: $KEY"

# browser flow with a cookie jar (CSRF handled by the form)
curl -s -c cookies.txt $BASE/register -o register.html
curl -s -b cookies.txt -c cookies.txt -X POST $BASE/register \
  --data-urlencode "csrf_token=$(grep -o 'name=\"csrf_token\" value=\"[^\"]*' register.html | cut -d'\"' -f4)" \
  --data-urlencode "full_name=Lê Văn C" \
  --data-urlencode "email=c@example.com"
```
