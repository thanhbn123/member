# INTEGRATION.md — plugging MEMBER into VIPORDER / VIP GROUP / VIP AI / Marketing Hub

This document describes how the MEMBER service is meant to be wired into the wider VIP ecosystem
**later** — when a real deployment is requested. Nothing here is deployed today: this milestone is
local-only (see [`DEPLOYMENT.md`](DEPLOYMENT.md)).

MEMBER owns exactly one domain: **member registration and verification**. It is not a CRM, not a
customer database of record, and not an order system. Everything below respects that boundary.

* Endpoint reference: [`API.md`](API.md)
* Service overview and environment variables: [`README.md`](../README.md)
* Production recipe: [`DEPLOYMENT.md`](DEPLOYMENT.md)

---

## 1. Integration styles at a glance

| # | Style | Direction | When to use it | Works today |
|---|---|---|---|---|
| **A** | **Public REST API**, server-to-server with `X-API-Key` | VIPORDER → MEMBER | You already collect the member’s data in your own UI/checkout and want MEMBER to own verification, attribution and the audit trail. | Yes |
| **B** | **Verified-member webhook** (`member.verified`) | MEMBER → VIPORDER | You need to react when a member actually confirms their email (grant benefits, create a VIPORDER customer, tag a CRM contact). This is the only event MEMBER pushes. | Yes |
| **C** | **Hosted registration page**, white-labelled per brand | Browser → MEMBER | The simplest funnel: send traffic to MEMBER’s own page (link, redirect, or a brand subdomain). | Yes |
| **C′** | **Cross-origin `<iframe>` embed** | Browser → MEMBER | *Not available today* — see §5.3. Blocked by design by `X-Frame-Options: DENY` + `frame-ancestors 'none'`. | **No** |

The intended production shape is **A + B**: integration in both directions, with C as the
zero-integration fallback.

```
                    ┌──────────────────────────────────────────┐
                    │              MEMBER service              │
                    │  /register (HTML)   /api/v1 (JSON)        │
                    │  admin UI           member.verified hook  │
                    └───────┬───────────────────────┬──────────┘
        A: REST + X-API-Key │                       │ B: signed webhook
                            ▼                       ▼
   VIPORDER / VIP GROUP / VIP AI / Marketing Hub  (any HTTPS endpoint you run)
                            ▲
                            │ C: hosted page (link, redirect, brand subdomain)
                        end user
```

---

## 2. Style A — public REST API (server-to-server)

**Auth.** Set `MEMBER_API_KEY` on the MEMBER deployment and send it on every member call. The key is
compared with `hmac.compare_digest`; without it (or with a wrong one) the response is
`401 unauthorized`. `/api/v1/health` and `/health` never need a key — use them as your readiness
probe.

**Do not** put this key in browser code. It is a server-side credential; the public registration
page exists precisely so the browser never needs it.

**Endpoints you will use**

| Purpose | Call |
|---|---|
| Register (or re-notify) a member | `POST /api/v1/members/register` → `201` new / `200` duplicate |
| Resolve by natural key | `GET /api/v1/members?email=<email>` → `200` / `404` |
| Resolve by id | `GET /api/v1/members/{member_id}` → `200` / `404` |
| Re-send the verification email | `POST /api/v1/members/{member_id}/resend-verification` |
| Capability check | `GET /api/v1/health` → `email_mode`, `webhook_enabled`, `meta_enabled`, `api_key_required` |

**Sequence**

```
VIPORDER                       MEMBER /api/v1                        DB
   │                                │                                 │
   │ POST /members/register         │                                 │
   │ X-API-Key: <key>               │                                 │
   │ {full_name,email,phone,        │                                 │
   │  source:"viporder", utm_*}     │                                 │
   │───────────────────────────────▶│ validate + normalise            │
   │                                │ upsert member + attribution ───▶│
   │                                │ COMMIT                          │
   │                                │ email verification link ──▶ (SMTP/console)
   │  201 {member:{id,…},            │                                 │
   │       duplicate:false,          │                                 │
   │       verification_sent:true}   │                                 │
   │◀───────────────────────────────│                                 │
   │ store member_id in your         │                                 │
   │ own customer row                │                                 │
   │                                │                                 │
   │ POST /members/register  (retry, same email)                      │
   │───────────────────────────────▶│ duplicate=true, SAME member.id   │
   │  200 {duplicate:true}          │                                 │
   │◀───────────────────────────────│                                 │
```

**Rules**

* Treat `201` and `200` as success. Only `201` means “this call created the member”; `200` means
  “the member already existed” and the body carries the **existing** `id`.
* Never retry with a *different* email casing or with a guessed id — `email` is the key; MEMBER
  lower-cases and normalises it for you.
* `verification_sent: false` with a non-null `email_error` is a **delivery** problem, not a
  registration problem: the member exists as `pending`. Retry later via the resend endpoint.
* Branch on `error.code`, never on the Vietnamese `error.message` text.

---

## 3. Style B — the verified-member webhook (push into VIPORDER)

Configure on the MEMBER deployment:

```dotenv
MEMBER_VERIFIED_WEBHOOK_URL=https://api.viporder.vn/hooks/member-verified
MEMBER_VERIFIED_WEBHOOK_SECRET=<48+ random bytes, shared with VIPORDER only>
WEBHOOK_TIMEOUT_SECONDS=10
WEBHOOK_MAX_ATTEMPTS=3
WEBHOOK_BACKOFF_SECONDS=1.0
```

The webhook fires **after** the verification transaction commits, and only for
`member.verified` — `pending`, `unsubscribed` and `blocked` are not events.

**Sequence**

```
Member's browser        MEMBER                         VIPORDER receiver
      │ GET /verify-email?token=…  │                          │
      │───────────────────────────▶│ atomic token claim       │
      │                            │ member.status=verified   │
      │                            │ COMMIT                   │
      │  200 "Xác minh thành công" │                          │
      │◀───────────────────────────│                          │
      │                            │ POST member.verified     │
      │                            │ X-Member-Signature: …    │
      │                            │ X-Member-Timestamp: …    │
      │                            │ X-Member-Delivery: uuid  │
      │                            │─────────────────────────▶│ verify HMAC over RAW body
      │                            │                          │ de-duplicate on delivery id
      │                            │                          │ upsert VIPORDER customer
      │                            │        2xx               │
      │                            │◀─────────────────────────│
      │                            │ record WEBHOOK_SENT       │
      │                            │ (2xx ends delivery;       │
      │                            │  else retry 1s,2s,…)      │
```

Payload and signature details are in [README §8](../README.md#8-webhook-integration) and
[API.md §6.1](API.md#61-memberverified-webhook). The short version:

```
X-Member-Signature: sha256=<hmac_sha256(secret, "<X-Member-Timestamp>." + <raw body bytes>)>
```

Two rules that prevent almost every real-world incident:

1. **Verify over the raw bytes**, before your framework parses JSON. Re-serialising the payload
   changes the bytes and the signature will never match.
2. **Be fast and idempotent.** Return `2xx` immediately (enqueue the work); de-duplicate on
   `X-Member-Delivery` (stable across retries) and on `member.id` (stable forever).

---

## 4. Reference implementation — a VIPORDER client (register + receive + verify)

One file, no framework beyond Flask, showing the whole A+B loop: register a member, expose the
webhook endpoint, verify the signature with `hmac.compare_digest`, and de-duplicate.

```python
# viporder_member_client.py
"""Minimal VIPORDER <-> MEMBER integration: register over REST, receive member.verified."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any

import httpx
from flask import Flask, request

log = logging.getLogger("viporder.member")
logging.basicConfig(level=logging.INFO)

MEMBER_BASE_URL = os.environ.get("MEMBER_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
MEMBER_API_KEY = os.environ["MEMBER_API_KEY"]              # server-side only
WEBHOOK_SECRET = os.environ["MEMBER_VERIFIED_WEBHOOK_SECRET"]

app = Flask(__name__)
seen_deliveries: set[str] = set()                          # use Redis/DB in production


# --------------------------------------------------------------------------- A: register
def register_member(
    *,
    full_name: str,
    email: str,
    phone: str | None = None,
    utm: dict[str, str] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Register (or re-notify) a member. Returns the envelope `data` dict.

    Idempotent: MEMBER keys on the normalised email, so calling this twice returns the
    same member id with HTTP 200 instead of creating a second member.
    """
    body: dict[str, Any] = {
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "source": "viporder",          # allowed source values: web_form|api|viporder|vipgroup|import|other
        "consent_marketing": True,
    }
    if utm:
        body.update({k: v for k, v in utm.items() if k.startswith("utm_") or k == "landing_url"})

    owns_client = client is None
    client = client or httpx.Client(base_url=MEMBER_BASE_URL, timeout=10.0)
    try:
        response = client.post(
            "/api/v1/members/register",
            json=body,
            headers={"X-API-Key": MEMBER_API_KEY},
        )
        payload = response.json()
        if response.status_code not in (200, 201) or not payload.get("success"):
            raise RuntimeError(f"MEMBER register failed [{response.status_code}]: {payload.get('error')}")
        data = payload["data"]
        log.info(
            "member %s id=%s duplicate=%s verification_sent=%s",
            data["member"]["email"], data["member"]["id"], data["duplicate"], data["verification_sent"],
        )
        if not data["verification_sent"]:
            # the member exists (pending); only the email failed - retry the send later
            log.warning("verification email not delivered: %s", data["email_error"])
        return data
    finally:
        if owns_client:
            client.close()


def find_member(email: str) -> dict[str, Any] | None:
    """Look a member up by the natural key without creating anything."""
    with httpx.Client(base_url=MEMBER_BASE_URL, timeout=10.0) as client:
        response = client.get(
            "/api/v1/members", params={"email": email}, headers={"X-API-Key": MEMBER_API_KEY}
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()["data"]


# --------------------------------------------------------------------------- B: webhook
def verify_signature(raw_body: bytes, timestamp: str, signature: str, secret: str = WEBHOOK_SECRET) -> bool:
    """Constant-time verification of X-Member-Signature over the exact request bytes."""
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


@app.post("/hooks/member-verified")
def member_verified():
    raw = request.get_data()                                    # bytes, BEFORE parsing
    if not verify_signature(
        raw,
        request.headers.get("X-Member-Timestamp", ""),
        request.headers.get("X-Member-Signature", ""),
    ):
        log.warning("rejected member.verified: bad signature")
        return {"error": "invalid signature"}, 401

    delivery_id = request.headers.get("X-Member-Delivery", "")
    if delivery_id and delivery_id in seen_deliveries:           # MEMBER retries 3x by default
        return {"ok": True, "duplicate": True}, 200
    seen_deliveries.add(delivery_id)

    payload = request.get_json(force=True)
    if payload.get("event") != "member.verified":
        return {"error": "unexpected event"}, 400

    member = payload["member"]
    attribution = payload.get("attribution", {})                 # utm_*, landing_url, referrer, fbp, fbc

    upsert_viporder_customer(                                     # your own idempotent write
        member_id=member["id"],                                   # UUID: stable, safe as a foreign key
        email=member["email"],
        full_name=member.get("full_name"),
        phone=member.get("phone"),
        consent_marketing=bool(member.get("consent_marketing")),
        source_utm=attribution.get("utm_source"),                 # "viporder" when you set it
        campaign=attribution.get("utm_campaign"),
    )
    return {"ok": True}, 200


def upsert_viporder_customer(**fields: Any) -> None:
    """Idempotent write: INSERT ... ON CONFLICT (member_id) DO UPDATE ... in your own DB."""
    log.info("VIPORDER customer upserted: %s", fields)


if __name__ == "__main__":
    # local smoke test: register a member and print the id
    register_member(
        full_name="Nguyễn Văn A",
        email="nguyen.van.a@example.com",
        phone="0901234567",
        utm={"utm_source": "viporder", "utm_medium": "email", "utm_campaign": "launch-2025"},
    )
```

Run it locally against a local MEMBER:

```bash
export MEMBER_BASE_URL=http://127.0.0.1:8000
export MEMBER_API_KEY=the-value-of-MEMBER_API_KEY
export MEMBER_VERIFIED_WEBHOOK_SECRET=the-value-of-MEMBER_VERIFIED_WEBHOOK_SECRET
python viporder_member_client.py     # registers one member and prints the id
```

…and point MEMBER at the receiver:

```dotenv
# MEMBER .env
MEMBER_VERIFIED_WEBHOOK_URL=https://<your-tunnel-or-host>/hooks/member-verified
MEMBER_VERIFIED_WEBHOOK_SECRET=<same value as above>
```

Verifying the loop locally: register → read the `[EMAIL][console] …` block from MEMBER’s stdout →
open the `verify-email` URL → the receiver logs `VIPORDER customer upserted`.

---

## 5. Style C — hosting the registration page (white-label)

### 5.1 One deployment per brand

A “tenant” is **an `.env` file plus a database**. There is no tenant table and no runtime branching
on a customer name; branding is configuration:

```dotenv
# tenant: VIPORDER
APP_NAME=VIPORDER Member
PUBLIC_BASE_URL=https://members.viporder.vn
BRAND_NAME=VIPORDER
BRAND_TAGLINE=Đăng ký thành viên VIPORDER
BRAND_PRIMARY_COLOR=#e11d48
BRAND_LOGO_URL=https://cdn.viporder.vn/logo.svg
BRAND_SUPPORT_EMAIL=support@viporder.vn

SECRET_KEY=<unique per deployment>
IP_HASH_SALT=<unique per deployment>
ADMIN_EMAIL=ops@viporder.vn
ADMIN_PASSWORD_HASH=<scrypt hash>
MEMBER_API_KEY=<unique per deployment>
MEMBER_VERIFIED_WEBHOOK_URL=https://api.viporder.vn/hooks/member-verified
MEMBER_VERIFIED_WEBHOOK_SECRET=<unique per deployment>
```

Repeat with `BRAND_*`/`PUBLIC_BASE_URL`/keys for VIP GROUP, VIP AI, Marketing Hub — each with its
**own** database, `SECRET_KEY`, `IP_HASH_SALT` and webhook secret. Never share these between tenants:
`SECRET_KEY` signs sessions and `IP_HASH_SALT` is what keeps IP hashes non-correlatable across
deployments.

`PUBLIC_BASE_URL` matters more than it looks: it is the origin used in the verification email, so a
wrong value produces links that point at the wrong host.

### 5.2 Sequence — hosted page (link, redirect or brand subdomain)

```
Visitor                Brand site              MEMBER (white-labelled)        VIPORDER
   │ click "Đăng ký"       │                          │                          │
   │───────────────────────▶│ 302/redirect to          │                          │
   │                        │ PUBLIC_BASE_URL/register │                          │
   │──────────────────────────────────────────────────▶│ GET /register            │
   │                        │                          │ (brand colours/logo,     │
   │                        │                          │  CSRF cookie)            │
   │  form POST /register   │                          │                          │
   │──────────────────────────────────────────────────▶│ member + attribution     │
   │  303 /check-email      │                          │ COMMIT → email           │
   │◀──────────────────────────────────────────────────│                          │
   │  open email link → /verify-email?token=…           │                          │
   │──────────────────────────────────────────────────▶│ COMMIT verification      │
   │  200 "Xác minh thành công"                         │──member.verified────────▶│
```

Traffic from a brand site should carry the correlation parameters (§6) — either in the link (server
side, which is the reliable way) or via the page’s own first-touch capture.

### 5.3 Cross-origin `<iframe>` embedding is **not** supported today

The service deliberately sends:

```
X-Frame-Options: DENY
Content-Security-Policy: … frame-ancestors 'none' …
```

Both are hard-coded (`app/middleware.py` + `Settings.csp_policy()`), so a branded page on
`viporder.vn` **cannot** frame `members.viporder.vn`. Do not plan around an iframe.

Note the direction of the CSP directive: `CSP_EXTRA_FRAME_SRC` / `frame-src` controls what the
*hosted page* is allowed to embed — it does **not** grant another site permission to frame MEMBER.
That would require `frame-ancestors` to be relaxed, which is a code change (a new setting plus an
allow-list of parent origins) and a deliberate security decision — the CSRF double-submit cookie and
the admin session are both safer without third-party framing.

**What to do instead**

| Option | Effort | Notes |
|---|---|---|
| Link / redirect to the hosted page (recommended) | none | Full CSP protection, works with the brand subdomain and `BRAND_*`. |
| Brand subdomain (`members.viporder.vn`) pointing at the MEMBER deployment | DNS + TLS | Looks native to the customer; the funnel is still MEMBER’s page. |
| Build your own form and call §2 from your backend | one endpoint | You own the UX; MEMBER still owns verification, attribution and the audit trail. |
| Build your own form and call the API **from the browser** | not recommended | Would require exposing `MEMBER_API_KEY` to the browser — see §8. |

---

## 6. Correlating registrations with your systems

Two independent axes, both stored on every member:

| Axis | Field | Set by | Purpose |
|---|---|---|---|
| **System of origin** | `source` | the caller | Which integration created the record. Allowed: `web_form`, `api`, `viporder`, `vipgroup`, `import`, `other`; anything else is stored as **`other`**. |
| **Campaign** | `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`, `landing_url`, `referrer` | query string / form / API body | Where the *traffic* came from; first-touch wins. |

```bash
# VIPORDER → MEMBER, both axes set
curl -X POST "$MEMBER_BASE_URL/api/v1/members/register" \
  -H "Content-Type: application/json" -H "X-API-Key: $MEMBER_API_KEY" \
  -d '{"full_name":"Nguyễn Văn A","email":"a@example.com",
       "source":"viporder","utm_source":"viporder","utm_medium":"email",
       "utm_campaign":"launch-2025"}'
```

And from a hosted-page link:

```
https://members.viporder.vn/register?utm_source=viporder&utm_medium=zalo&utm_campaign=tet-2026
```

**Important:** `source` has a fixed vocabulary. `viporder` and `vipgroup` are recognised; **VIP AI
and Marketing Hub are not** — passing `source=vipai` or `source=marketinghub` stores `other`. For
those two use `source=api` (or `other`) and put the system name in `utm_source`, e.g.
`utm_source=vipai&utm_campaign=onboarding`, until the allowed list is extended in
`app/routers/api_v1.py::ALLOWED_SOURCES` (a code change).

**Where the correlation surfaces**

* In the API: `GET /api/v1/members/{id}` (and the email lookup) returns `attribution`.
* In the webhook: the `attribution` object of the `member.verified` payload (only fields that have a
  value; never `ip_hash`).
* In the admin UI: the `utm_source` filter and the CSV export columns (`source`, `utm_*`,
  `landing_url`, `referrer`, `fbp`, `fbc`).
* In the audit trail: `member_events` records `REGISTER_STARTED` with
  `{"source": …, "utm_source": …, "duplicate": bool}` and every `EMAIL_*`/`WEBHOOK_*`/`META_*`
  outcome.

Because attribution is first-touch, the *first* system that registered the email wins the campaign
fields; later registrations only fill gaps. Agree inside the group which system calls register first,
or always send the full `utm_*` set on the first call.

---

## 7. Idempotency and identity

| Concept | Value | Rules |
|---|---|---|
| **Primary key** | `member.id` — a UUIDv4 | Stable forever, safe to store as a foreign key, never reused. Always prefer it for links between systems; never derive it yourself. |
| **Natural unique key** | `email` | Normalised server-side (lower-cased); `members.email` has a unique index. Two registrations of the same address can never create two members. |
| **Duplicate registration** | `200 OK` + `data.duplicate: true` + the **existing** member | Not an error. The member id in the response is the one you should store. |
| **Duplicate while `pending`** | `200` + `verification_sent: true` | The old link is invalidated and a new one is emailed — safe to retry a lost registration. |
| **Duplicate while `verified`** | `200` + `verification_sent: false` | Nothing is sent; the member is untouched. |
| **Status lifecycle** | `pending` → `verified` → (`unsubscribed` \| `blocked`) | Only verification moves a member to `verified`. MEMBER does not currently expose endpoints that set the other two (admin/ops only). |
| **Webhook delivery** | `X-Member-Delivery` UUID | Stable across the retries of one event; de-duplicate on it. `member.id` is the durable key for your own upsert. |

Retry policy for a caller:

```
POST /api/v1/members/register
  ├─ 201/200 + success:true  → store data.member.id, done
  ├─ 422 validation_error    → fix the input, do NOT retry unchanged
  ├─ 401 unauthorized        → fix the key (configuration bug)
  ├─ 429 rate_limited        → sleep Retry-After seconds, then retry (exponential backoff)
  ├─ 5xx / timeout           → retry with backoff: a duplicate is harmless (200, same id)
  └─ network error           → retry; idempotent by design
```

---

## 8. Do not do this

* **Do not write to MEMBER’s database from another system.** No shared tables, no foreign keys into
  `members`, no direct SQL. MEMBER owns its schema and its migrations; use the API and the webhook,
  which are versioned and audited. A shared database makes every schema change a group-wide outage.
* **Do not store secrets in client-side code.** `MEMBER_API_KEY`, `MEMBER_VERIFIED_WEBHOOK_SECRET`,
  `SECRET_KEY`, `IP_HASH_SALT`, `ADMIN_PASSWORD_HASH` and SMTP credentials live in the server-side
  `.env` (chmod 600, never committed). No browser bundle, mobile app, spreadsheet, ticket or chat
  message.
* **Do not enable Meta (CAPI or Pixel) without consent handling.** `META_PIXEL_ID`,
  `META_ACCESS_TOKEN` and `GA4_MEASUREMENT_ID` are off by default precisely so that a fresh
  deployment sends nothing to third parties. Before turning them on, make sure you have a lawful
  basis, a consent banner that covers marketing/analytics cookies (`_fbp`/`_fbc` are only captured
  when the browser already has them), a privacy notice that names Meta/Google, and a process for
  withdrawal. Enabling the pixel also widens the CSP and loads third-party scripts — a deliberate
  decision, not a default.
* **Do not store raw IP addresses anywhere.** MEMBER stores only `SHA256(ip + IP_HASH_SALT)` and
  omits `client_ip_address` from the Meta payload and `ip_hash` from the webhook. If VIPORDER wants
  IP-level analytics, keep it in your own system, under your own legal basis — do not ask MEMBER to
  start persisting raw IPs, and do not try to reverse the hash.
* **Do not log or export verification tokens or full email addresses where they are not needed.**
  MEMBER redacts `token=…` from its logs and masks the address in the `check-email` redirect
  (`a***@example.com`); keep the same discipline in your own logs and support tooling.
* **Do not treat the webhook as a guaranteed, ordered stream.** It is fire-and-forget with up to
  `WEBHOOK_MAX_ATTEMPTS` attempts and no ordering guarantee between members. Reconcile periodically
  with `GET /api/v1/members/{id}` (or the email lookup) instead of assuming you received every
  event.
* **Do not put `MEMBER_API_KEY` in a mobile app, SPA or public CI job**, and do not share one key
  across tenants — per-tenant keys make revocation a one-line change instead of a coordinated
  rollout.
* **Do not rely on `error.message` text or on internal ids** (`token_id`, event ids, `ip_hash`).
  Branch on `error.code` and on `member.id` / `email`; everything else is implementation detail and
  may change without a version bump.
* **Do not assume the HTML page can be framed** (§5.3) or that MEMBER sends email to arbitrary
  addresses on your behalf — it only sends the verification email for a member that exists.

---

## 9. Rollout checklist (when the group decides to deploy)

1. Decide the tenant list and their hostnames (`members.<brand>`), then create the deployments per
   [`DEPLOYMENT.md`](DEPLOYMENT.md) — one database, one `.env`, one `SECRET_KEY`/`IP_HASH_SALT` each.
2. Fill `BRAND_*`, `PUBLIC_BASE_URL`, `ADMIN_*`, `EMAIL_MODE=smtp` + `SMTP_*` per tenant; run
   `alembic upgrade head`; verify `python -m app.cli check-config` and `GET /api/v1/health`.
3. Generate per-tenant `MEMBER_API_KEY` and `MEMBER_VERIFIED_WEBHOOK_SECRET`; store them in the
   secret manager of both sides.
4. Deploy the VIPORDER receiver (§4), point `MEMBER_VERIFIED_WEBHOOK_URL` at it, and prove the loop
   end to end with one real member before opening traffic.
5. Wire the funnel links with `utm_source`/`utm_medium`/`utm_campaign` and `source=viporder`
   (§6) — remember `vipai`/`marketinghub` need `source=api` + `utm_source` until the allow-list is
   extended.
6. Reconcile once a day at first: compare `GET /api/v1/members?email=…` against your own records for
   a sample of new members, and watch `WEBHOOK_FAILED` / `EMAIL_FAILED` events in the admin detail
   page.
7. Keep `API_DOCS_ENABLED=false`, `TRUSTED_PROXY_HEADERS=true` (proxy only), HTTPS everywhere, and
   revisit the in-process rate limiter (README §10) if you scale beyond one worker.
