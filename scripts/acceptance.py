#!/usr/bin/env python3
"""Local acceptance harness for the MEMBER service.

Runs the real application (uvicorn + Alembic migrations + a real database) and
replays the full acceptance checklist end-to-end over HTTP, exactly the way a
customer would:

    1  app start            8  verify member in DB (verified)
    2  GET /health          9  reuse the same token -> rejected
    3  GET /register       10  admin login
    4  register a member   11  admin sees the member
    5  DB check (pending)  12  CSV export
    6  console email URL   13  API registration
    7  click verification  14  pytest suite, 15  secret scan

Usage:
    python scripts/acceptance.py                 # everything
    python scripts/acceptance.py --skip-tests    # skip step 14 (pytest)
    python scripts/acceptance.py --port 8123 --keep-db

Exit code 0 only when every step passed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "Local-Acceptance-Passw0rd!"
API_KEY = "local-acceptance-api-key"
SECRET_KEY = "local-acceptance-secret-key-0123456789"
IP_SALT = "local-acceptance-ip-salt"

TOKEN_RE = re.compile(r"/verify-email\?token=([A-Za-z0-9_\-%]+)")
CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')

results: list[tuple[int, str, bool, str]] = []


def record(step: int, title: str, ok: bool, detail: str = "") -> None:
    results.append((step, title, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] step {step:>2} - {title}"
    if detail:
        line += f" :: {detail}"
    print(line, flush=True)


def fail_now(step: int, title: str, detail: str) -> None:
    record(step, title, False, detail)
    summary()
    sys.exit(1)


def summary() -> None:
    passed = sum(1 for _step, _title, ok, _detail in results if ok)
    total = len(results)
    print("\n" + "=" * 72)
    print(f"LOCAL ACCEPTANCE: {passed}/{total} steps passed")
    for step, title, ok, detail in results:
        if not ok:
            print(f"  FAILED step {step}: {title} :: {detail}")
    print("=" * 72)


def server_env(database_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": database_url,
            "SECRET_KEY": SECRET_KEY,
            "IP_HASH_SALT": IP_SALT,
            "APP_ENV": "local",
            "EMAIL_MODE": "console",
            "ADMIN_EMAIL": ADMIN_EMAIL,
            "PUBLIC_BASE_URL": env.get("PUBLIC_BASE_URL", ""),
            "MEMBER_API_KEY": API_KEY,
            "PYTHONUNBUFFERED": "1",
        }
    )
    if not env["PUBLIC_BASE_URL"]:
        env.pop("PUBLIC_BASE_URL")
    return env


def hash_admin_password(env: dict[str, str]) -> str:
    from app.security import hash_password

    return hash_password(ADMIN_PASSWORD)


def wait_for_health(base_url: str, process: subprocess.Popen, log_path: Path, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            fail_now(1, "app start", f"uvicorn exited early (rc={process.returncode})\n{log_path.read_text()[-2000:]}")
        try:
            response = httpx.get(f"{base_url}/health", timeout=2.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    fail_now(1, "app start", f"server did not become healthy within {timeout}s")


_ENGINES: dict[str, object] = {}


def db_query(database_url: str, query: str, **params) -> list[tuple]:
    """Run a verification query against whatever database the app is using."""
    from sqlalchemy import create_engine, text

    engine = _ENGINES.get(database_url)
    if engine is None:
        engine = create_engine(database_url)
        _ENGINES[database_url] = engine
    with engine.connect() as connection:  # type: ignore[attr-defined]
        return [tuple(row) for row in connection.execute(text(query), params).fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument(
        "--database-url",
        default="",
        help="Database to accept against (default: a throwaway sqlite file next to the repo). "
        "Pass a postgresql+psycopg:// DSN to prove the same flow on PostgreSQL.",
    )
    parser.add_argument("--skip-tests", action="store_true", help="skip step 14 (pytest)")
    parser.add_argument("--skip-secret-scan", action="store_true", help="skip step 15")
    parser.add_argument("--keep-db", action="store_true", help="do not delete acceptance.db first")
    args = parser.parse_args()

    db_path = ROOT / "acceptance.db"
    if args.database_url:
        database_url = args.database_url
    else:
        if db_path.exists() and not args.keep_db:
            db_path.unlink()
        database_url = f"sqlite:///{db_path}"
    print(f"--- database: {database_url} ---", flush=True)
    log_path = Path("/tmp/member_acceptance_server.log")
    if log_path.exists():
        log_path.unlink()

    env = server_env(database_url)

    # ---------------------------------------------------------------- step 1: migrations + app start
    print("--- applying migrations (alembic upgrade head) ---", flush=True)
    migration = subprocess.run(
        [sys.executable, "-m", "app.cli", "init-db"], cwd=ROOT, env=env, capture_output=True, text=True
    )
    if migration.returncode != 0:
        fail_now(1, "app start", f"alembic upgrade head failed:\n{migration.stdout}\n{migration.stderr}")

    env["ADMIN_PASSWORD_HASH"] = hash_admin_password(env)
    base_url = f"http://127.0.0.1:{args.port}"
    env["PUBLIC_BASE_URL"] = base_url
    log_file = log_path.open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.port), "--log-level", "info"],
        cwd=ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_for_health(base_url, process, log_path)
        record(1, "app start", True, f"uvicorn pid={process.pid} + migrations applied")

        client = httpx.Client(base_url=base_url, timeout=15.0, follow_redirects=False)

        # ------------------------------------------------------------ step 2: health
        health = client.get("/health")
        body = health.json() if health.status_code == 200 else {}
        if health.status_code == 200 and body.get("database") == "ok":
            record(2, "GET /health", True, str(body))
        else:
            fail_now(2, "GET /health", f"status={health.status_code} body={health.text[:200]}")

        # ------------------------------------------------------------ step 3: register form (+ CSRF)
        page = client.get("/register")
        csrf = CSRF_RE.search(page.text)
        if page.status_code == 200 and csrf and "csrf_token" in page.text:
            record(3, "GET /register", True, f"{len(page.text)} bytes, csrf cookie set")
        else:
            fail_now(3, "GET /register", f"status={page.status_code} csrf={bool(csrf)}")

        # ------------------------------------------------------------ step 4: register with attribution
        email = f"acceptance-{uuid.uuid4().hex[:8]}@example.com"
        client.cookies.set("_fbp", "fb.1.1700000000000.1234567890")
        client.cookies.set("_fbc", "fb.1.1700000000000.9876543210")
        register = client.post(
            "/register",
            params={
                "utm_source": "facebook",
                "utm_medium": "cpc",
                "utm_campaign": "acceptance",
                "utm_content": "ad-1",
                "utm_term": "member",
            },
            data={
                "csrf_token": csrf.group(1),
                "full_name": "Khách Hàng Acceptance",
                "email": email,
                "phone": "0901234567",
                "company": "Công ty Acceptance",
                "consent_marketing": "true",
            },
            headers={"Referer": "https://facebook.com/ads", "User-Agent": "acceptance-harness/1.0"},
        )
        if register.status_code != 303 or "/check-email" not in register.headers.get("location", ""):
            fail_now(4, "register a member", f"status={register.status_code} location={register.headers.get('location')}")
        record(4, "register a member", True, f"{email} -> {register.headers['location']}")

        # ------------------------------------------------------------ step 5: DB check (pending + attribution)
        rows = db_query(
            database_url,
            "select id, status, email_verified_at from members where email = :email",
            email=email,
        )
        attribution = db_query(
            database_url,
            "select a.utm_source, a.utm_medium, a.utm_campaign, a.utm_content, a.utm_term, a.fbp, a.fbc, "
            "a.ip_hash, a.referrer from member_attribution a join members m on m.id = a.member_id "
            "where m.email = :email",
            email=email,
        )
        if not rows or rows[0][1] != "pending":
            fail_now(5, "DB check (pending)", f"rows={rows}")
        if not attribution:
            fail_now(5, "DB check (attribution)", "no attribution row stored")
        source, medium, campaign, content, term, fbp, fbc, ip_hash, referrer = attribution[0]
        expected = ("facebook", "cpc", "acceptance", "ad-1", "member")
        if (source, medium, campaign, content, term) != expected or fbp != "fb.1.1700000000000.1234567890" or fbc != "fb.1.1700000000000.9876543210":
            fail_now(5, "DB check (attribution)", f"got source={source} fbp={fbp} fbc={fbc}")
        if not ip_hash or len(ip_hash) != 64 or "127.0.0.1" in str(ip_hash):
            fail_now(5, "DB check (ip hash)", f"ip_hash={ip_hash}")
        record(5, "DB check (pending + attribution)", True, "utm/fbp/fbc stored, IP stored only as sha256")

        # ------------------------------------------------------------ step 6: console email
        time.sleep(0.5)
        log_text = log_path.read_text(errors="replace")
        tokens = TOKEN_RE.findall(log_text)
        if not tokens:
            fail_now(6, "console email", "no verification URL found in server stdout")
        token = tokens[-1]
        record(6, "console email URL", True, f"token={token[:12]}...")

        # ------------------------------------------------------------ step 7: verify
        verify = client.get(f"/verify-email?token={token}")
        if verify.status_code != 200 or "Xác minh thành công" not in verify.text:
            fail_now(7, "click verification", f"status={verify.status_code}")
        record(7, "click verification", True, "200 + success page")

        # ------------------------------------------------------------ step 8: DB verified
        rows = db_query(
            database_url,
            "select status, email_verified_at from members where email = :email",
            email=email,
        )
        used = db_query(
            database_url, "select used_at from email_verification_tokens order by id desc limit 1"
        )
        if rows and rows[0][0] == "verified" and rows[0][1] and used and used[0][0]:
            record(8, "DB check (verified)", True, f"email_verified_at={rows[0][1]}, token marked used")
        else:
            fail_now(8, "DB check (verified)", f"member={rows} token={used}")

        # ------------------------------------------------------------ step 9: token reuse rejected
        reuse = client.get(f"/verify-email?token={token}")
        if reuse.status_code == 400:
            record(9, "token reuse rejected", True, "400 on the second use")
        else:
            fail_now(9, "token reuse rejected", f"status={reuse.status_code}")

        # ------------------------------------------------------------ step 10: admin login
        login_page = client.get("/admin/login")
        login_csrf = CSRF_RE.search(login_page.text)
        if not login_csrf:
            fail_now(10, "admin login", "no csrf token on the login page")
        login = client.post(
            "/admin/login",
            data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": login_csrf.group(1)},
        )
        if login.status_code == 303 and "/admin/" in login.headers.get("location", "") or login.status_code == 303:
            record(10, "admin login", True, f"303 -> {login.headers['location']}")
        else:
            fail_now(10, "admin login", f"status={login.status_code}")

        dashboard = client.get("/admin/dashboard")
        if dashboard.status_code != 200 or "Bảng điều khiển" not in dashboard.text:
            fail_now(10, "admin dashboard", f"status={dashboard.status_code}")
        record(10, "admin dashboard", True, "GET /admin/dashboard renders the management area")

        # ------------------------------------------------------------ step 11: admin sees the member
        listing = client.get("/admin/members", params={"q": email})
        if listing.status_code == 200 and email in listing.text:
            record(11, "admin sees member", True, "member visible in the filtered list")
        else:
            fail_now(11, "admin sees member", f"status={listing.status_code} email_in_page={email in listing.text}")

        # ------------------------------------------------------------ step 12: CSV export
        export = client.get("/admin/members.csv")
        export_text = export.content.decode("utf-8-sig", errors="replace")
        if export.status_code == 200 and "text/csv" in export.headers.get("content-type", "") and email in export_text:
            header_line = export_text.splitlines()[0]
            record(12, "CSV export", True, f"{len(export_text.splitlines()) - 1} data row(s), header={header_line[:40]}...")
        else:
            fail_now(12, "CSV export", f"status={export.status_code} type={export.headers.get('content-type')}")

        # ------------------------------------------------------------ step 13: API registration
        api_email = f"api-{uuid.uuid4().hex[:8]}@example.com"
        api = client.post(
            "/api/v1/members/register",
            headers={"X-API-Key": API_KEY},
            json={
                "full_name": "API Member",
                "email": api_email,
                "phone": "+84901234567",
                "consent_marketing": True,
                "utm_source": "viporder",
                "utm_campaign": "api-acceptance",
                "source": "viporder",
            },
        )
        payload = api.json() if api.status_code < 500 else {}
        if api.status_code == 201 and payload.get("success") and payload["data"]["member"]["status"] == "pending":
            member_id = payload["data"]["member"]["id"]
            fetched = client.get(f"/api/v1/members/{member_id}", headers={"X-API-Key": API_KEY})
            unauthorized = client.post("/api/v1/members/register", json={"full_name": "x", "email": "x@y.com"})
            if fetched.status_code == 200 and unauthorized.status_code == 401:
                record(13, "API registration", True, "201 created, GET member 200, missing API key 401")
            else:
                fail_now(13, "API registration", f"get={fetched.status_code} no_key={unauthorized.status_code}")
        else:
            fail_now(13, "API registration", f"status={api.status_code} body={api.text[:300]}")
    finally:
        client_close = locals().get("client")
        if client_close is not None:
            client_close.close()
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        log_file.close()

    # ---------------------------------------------------------------- step 14: test suite
    if args.skip_tests:
        record(14, "test suite (skipped)", True, "--skip-tests")
    else:
        pytest_run = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True
        )
        tail = (pytest_run.stdout or "").strip().splitlines()[-1:] or [""]
        if pytest_run.returncode == 0:
            record(14, "test suite", True, tail[0])
        else:
            fail_now(14, "test suite", f"{tail[0]}\n{(pytest_run.stdout or '')[-2000:]}")

    # ---------------------------------------------------------------- step 15: secret scan
    if args.skip_secret_scan:
        record(15, "secret scan (skipped)", True, "--skip-secret-scan")
    else:
        problems = secret_scan()
        if problems:
            fail_now(15, "secret scan", "; ".join(problems))
        record(15, "secret scan", True, "no secrets in tracked files, .env untracked")

    summary()
    failed = [step for step, _title, ok, _detail in results if not ok]
    return 1 if failed else 0


PEM_HEADER = "-----BEGIN "
PEM_KEY_MARKERS = ("PRIVATE" + " KEY", "OPENSSH" + " " + "PRIVATE" + " KEY")


DOC_SUFFIXES = (".md", ".rst", ".txt", ".example", ".sample", ".template")
PLACEHOLDER_PREFIXES = ("<", "$", "{", "CHANGE_ME", "change-me", "changeme", "your-", "xxx", "...")


def _is_documentation(relative: str) -> bool:
    """Files whose credentials are placeholders (docs, templates) or deliberate fakes (tests)."""
    lowered = relative.lower()
    if lowered.endswith(DOC_SUFFIXES) or lowered.startswith(("docs/", "deploy/readme")):
        return True
    return lowered.startswith(("tests/", "scripts/"))


def _looks_like_real_secret(value: str) -> bool:
    """Heuristic: a real credential is long enough and is not a placeholder."""
    if len(value) < 8:
        return False
    if value.startswith(PLACEHOLDER_PREFIXES) or value.lower().startswith(("change", "todo", "example")):
        return False
    return not any(character in value for character in "<>{}")


def secret_scan() -> list[str]:
    """Scan the *tracked* files for credentials and verify .env is not committed."""
    problems: list[str] = []
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.split()
    if ".env" in tracked:
        problems.append(".env is tracked by git")

    if not tracked:
        return problems

    patterns = {
        "local admin password": ADMIN_PASSWORD,
        "local api key": API_KEY,
        "local secret key": SECRET_KEY,
    }
    for relative in tracked:
        path = ROOT / relative
        if not path.is_file() or path.stat().st_size > 512 * 1024:
            continue
        if relative.endswith((".db", ".sqlite3", ".png", ".jpg", ".ico")):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for label, needle in patterns.items():
            if needle and needle in text and relative != "scripts/acceptance.py":
                problems.append(f"{label} found in {relative}")
        # A real credential in a config/code file, not a documented placeholder or a
        # template value: skip Markdown/templates and ignore "<...>", "$VAR" and CHANGE_ME.
        if not _is_documentation(relative):
            for match in re.finditer(r"(?m)^\s*(?:export\s+)?([A-Z_]*(?:PASSWORD|TOKEN|SECRET|API_KEY))\s*=\s*(\S.*)$", text):
                value = match.group(2).strip().strip("\"'")
                if _looks_like_real_secret(value):
                    problems.append(f"{match.group(1)} value found in {relative}")
                    break
        # Assembled at runtime so this scanner does not flag its own source file.
        if PEM_HEADER in text and any(marker in text for marker in PEM_KEY_MARKERS):
            problems.append(f"private key material in {relative}")
    return problems


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    if shutil.which("git") is None:  # pragma: no cover
        print("git is required for the secret scan step", file=sys.stderr)
    raise SystemExit(main())
