"""Admin CSV export: shape, filters, formula-injection hardening, audit event, auth."""

from __future__ import annotations

import csv
import io

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Member, MemberEvent
from app.services.admin import CSV_HEADERS

FORMULA_NAME = "=cmd|' /C calc'!A0"
FORMULA_COMPANY = "+Công ty TNHH ABC"


# --------------------------------------------------------------------------- helpers
def _register(client, web, email: str, query: str = "", **overrides):
    payload = {"full_name": "CSV Member", "email": email, "csrf_token": web.csrf_token()}
    payload.update(overrides)
    response = client.post(f"/register{query}", data=payload, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response


def _rows(raw: str) -> list[list[str]]:
    """Parse the export body (the first chunk carries a UTF-8 BOM for Excel)."""
    return [row for row in csv.reader(io.StringIO(raw.removeprefix("\ufeff"), newline="")) if row]


def _column(rows: list[list[str]], name: str) -> list[str]:
    index = rows[0].index(name)
    return [row[index] for row in rows[1:]]


def _member(db: Session, email: str) -> Member | None:
    db.expire_all()
    return db.execute(select(Member).where(Member.email == email)).scalar_one_or_none()


# --------------------------------------------------------------------------- shape
def test_csv_export_shape_headers_and_rows(admin_client, client, web):
    _register(client, web, "csv-one@example.com", "?utm_source=facebook")
    _register(client, web, "csv-two@example.com", "?utm_source=google")

    response = admin_client.get("/admin/members.csv")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert ".csv" in disposition
    assert response.headers["x-total-rows"] == "2"

    rows = _rows(response.text)
    assert rows[0] == CSV_HEADERS, "the header row must match app.services.admin.CSV_HEADERS"
    assert len(rows) == 3, "one data row per member"
    assert set(_column(rows, "email")) == {"csv-one@example.com", "csv-two@example.com"}
    exported = dict(zip(_column(rows, "email"), _column(rows, "utm_source"), strict=True))
    assert exported == {
        "csv-one@example.com": "facebook",
        "csv-two@example.com": "google",
    }


# --------------------------------------------------------------------------- filters
def test_csv_export_respects_filters(admin_client, client, web, mailbox):
    _register(client, web, "fb-pending@example.com", "?utm_source=facebook")
    _register(client, web, "gg-pending@example.com", "?utm_source=google")
    _register(client, web, "fb-verified@example.com", "?utm_source=facebook")
    token = mailbox.latest_token()
    assert client.get(f"/verify-email?token={token}").status_code == 200

    by_utm = admin_client.get("/admin/members.csv", params={"utm_source": "facebook"})
    assert by_utm.status_code == 200
    assert set(_column(_rows(by_utm.text), "email")) == {
        "fb-pending@example.com",
        "fb-verified@example.com",
    }

    filtered = admin_client.get(
        "/admin/members.csv", params={"status": "pending", "utm_source": "facebook"}
    )
    assert filtered.status_code == 200
    assert set(_column(_rows(filtered.text), "email")) == {"fb-pending@example.com"}
    assert filtered.headers["x-total-rows"] == "1"


# --------------------------------------------------------------------------- formula injection
def test_csv_export_neutralises_formula_injection(admin_client, client, web, db_session):
    _register(
        client,
        web,
        "formula@example.com",
        full_name=FORMULA_NAME,
        company=FORMULA_COMPANY,
    )

    member = _member(db_session, "formula@example.com")
    assert member is not None
    assert member.full_name == FORMULA_NAME, "the raw value must still be stored in the database"
    assert member.company == FORMULA_COMPANY

    response = admin_client.get("/admin/members.csv")
    assert response.status_code == 200
    raw = response.text
    rows = _rows(raw)
    assert len(rows) == 2

    name_cell = _column(rows, "full_name")[0]
    company_cell = _column(rows, "company")[0]

    assert name_cell == "'" + FORMULA_NAME
    assert company_cell == "'" + FORMULA_COMPANY
    for cell in (name_cell, company_cell):
        assert cell.startswith("'"), f"cell {cell!r} is not apostrophe-prefixed"
        assert not cell.startswith(("=", "+", "-", "@", "\t", "\r"))

    # ... and the same holds in the raw bytes of the response
    assert "'" + FORMULA_NAME in raw
    assert "'" + FORMULA_COMPANY in raw
    assert ",=cmd" not in raw
    assert "\r\n+" not in raw
    assert "\n+" not in raw


# --------------------------------------------------------------------------- audit trail
def test_csv_export_records_an_export_event(admin_client, client, web, db_session):
    _register(client, web, "csv-audit-one@example.com")
    _register(client, web, "csv-audit-two@example.com")

    response = admin_client.get("/admin/members.csv")
    assert response.status_code == 200
    assert response.text

    db_session.expire_all()
    events = list(
        db_session.execute(
            select(MemberEvent).where(MemberEvent.event_type == "EXPORT")
        ).scalars()
    )
    assert len(events) == 1, "the export must write exactly one EXPORT event"
    metadata = events[0].metadata_json
    assert metadata["rows"] == 2
    assert metadata["format"] == "csv"
    assert metadata["actor"] == "admin@example.com"


# --------------------------------------------------------------------------- auth
def test_csv_export_requires_admin(client, web):
    _register(client, web, "csv-guard@example.com")

    response = client.get("/admin/members.csv", follow_redirects=False)

    assert response.status_code in (303, 307), response.text
    assert "/admin/login" in response.headers["location"]
    assert "text/csv" not in response.headers.get("content-type", "")
    assert "csv-guard@example.com" not in response.text
