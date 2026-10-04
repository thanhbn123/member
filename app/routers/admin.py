"""Admin UI: login, dashboard, member list with filters/pagination, detail/edit, CSV export."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import (
    ADMIN_EMAIL_KEY,
    ADMIN_SESSION_KEY,
    admin_logged_in,
    login_rate_limit,
    require_admin,
    require_csrf,
)
from app.models import EventType, Member, MemberStatus, utcnow
from app.security import DUMMY_PASSWORD_HASH, client_ip, hash_ip, verify_password
from app.services.admin import (
    FLASH_MESSAGES,
    NOTE_MAX_LENGTH,
    PER_PAGE_CHOICES,
    RECENT_MEMBERS_LIMIT,
    STATUS_LABELS,
    MemberFilters,
    daily_registrations,
    dashboard_stats,
    export_filename,
    iter_csv,
    list_utm_sources,
    parse_filters,
    parse_member_edit,
    query_members,
    resolve_flash,
    top_attribution,
    update_member,
)
from app.services.events import record_event
from app.services.members import get_member, resend_verification
from app.web import render

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

DASHBOARD_PATH = "/admin/dashboard"
MEMBERS_PATH = "/admin/members"
# ``?next=`` may only ever bounce back to these two paths (exact match, no query string):
# an attacker-supplied path must never turn an admin POST into an open redirect.
RESEND_NEXT_TARGETS = (DASHBOARD_PATH, MEMBERS_PATH)
RESEND_DEFAULT_NEXT = MEMBERS_PATH


def _safe_next(value: object) -> str:
    """Allow-list the ``next`` form field; anything unknown falls back to the member list."""
    candidate = str(value or "").strip()
    return candidate if candidate in RESEND_NEXT_TARGETS else RESEND_DEFAULT_NEXT


def _flash_redirect(path: str, code: str) -> RedirectResponse:
    """303 (POST/redirect/GET) with an allow-listed flash code in the query string.

    Both halves are constrained: ``path`` is either an allow-listed constant or a path
    built from the member's own UUID, and ``code`` must exist in ``FLASH_MESSAGES`` -
    the query string is a lookup key, never rendered text.
    """
    safe_code = code if code in FLASH_MESSAGES else "error"
    return RedirectResponse(f"{path}?msg={quote(safe_code)}", status_code=303)


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def admin_index(request: Request) -> RedirectResponse:
    if not admin_logged_in(request):
        return RedirectResponse("/admin/login", status_code=303)
    return RedirectResponse(DASHBOARD_PATH, status_code=303)


@router.get("/login", include_in_schema=False)
async def login_form(request: Request) -> object:
    if admin_logged_in(request):
        return RedirectResponse(DASHBOARD_PATH, status_code=303)
    settings = get_settings()
    return render(
        request,
        "admin/login.html",
        error=None,
        admin_email_hint=_email_hint(settings.admin_email),
        admin_configured=settings.admin_configured,
    )


def _email_hint(email: str) -> str:
    if not email:
        return ""
    local, _, domain = email.partition("@")
    if not domain:
        return ""
    return f"{local[:1]}***@{domain}"


@router.post(
    "/login",
    include_in_schema=False,
    dependencies=[Depends(require_csrf), Depends(login_rate_limit)],
)
async def login_submit(request: Request, db: Session = Depends(get_db)) -> object:
    settings = get_settings()
    form = await request.form()
    email = str(form.get("email") or "").strip().lower()
    password = str(form.get("password") or "")

    email_matches = bool(settings.admin_configured) and email == settings.admin_email.strip().lower()
    # Always run scrypt - against a dummy hash when the email does not match - so the
    # response time cannot be used to tell a valid admin address from an unknown one.
    # The verification must not sit behind ``and``: that would short-circuit it away.
    stored_hash = settings.admin_password_hash if email_matches else DUMMY_PASSWORD_HASH
    password_ok = verify_password(password, stored_hash)
    ok_login = email_matches and password_ok

    if not ok_login:
        logger.warning("failed admin login attempt for %r", email)
        try:
            record_event(
                db,
                EventType.LOGIN,
                None,
                {
                    "actor": email,
                    "success": False,
                    "ip_hash": hash_ip(client_ip(request, settings.trusted_proxy_headers), settings.ip_hash_salt),
                },
            )
            db.commit()
        except Exception:  # pragma: no cover - auditing must not block the response
            logger.exception("could not record failed login")
            db.rollback()
        return render(
            request,
            "admin/login.html",
            status_code=status.HTTP_401_UNAUTHORIZED,
            error="Email hoặc mật khẩu không đúng.",
            admin_email_hint=_email_hint(settings.admin_email),
            admin_configured=settings.admin_configured,
        )

    request.session.clear()  # rotate the session on privilege change (fixation defence)
    request.session[ADMIN_SESSION_KEY] = True
    request.session[ADMIN_EMAIL_KEY] = settings.admin_email.lower()
    try:
        record_event(
            db,
            EventType.LOGIN,
            None,
            {
                "actor": settings.admin_email.lower(),
                "success": True,
                "ip_hash": hash_ip(client_ip(request, settings.trusted_proxy_headers), settings.ip_hash_salt),
            },
        )
        db.commit()
    except Exception:  # pragma: no cover
        logger.exception("could not record successful login")
        db.rollback()
    return RedirectResponse(DASHBOARD_PATH, status_code=303)


@router.post("/logout", include_in_schema=False, dependencies=[Depends(require_csrf)])
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


@router.get("/dashboard", include_in_schema=False, dependencies=[Depends(require_admin)])
async def dashboard(request: Request, db: Session = Depends(get_db)) -> object:
    """Landing page after login: counters, 14-day chart, top channels, newest members."""
    now = utcnow()
    recent, _total = query_members(db, MemberFilters(page=1, per_page=RECENT_MEMBERS_LIMIT))
    return render(
        request,
        "admin/dashboard.html",
        stats=dashboard_stats(db, now=now),
        series=daily_registrations(db, now=now),
        top_sources=top_attribution(db, "utm_source"),
        top_campaigns=top_attribution(db, "utm_campaign"),
        recent=recent,
        status_labels=STATUS_LABELS,
        flash=resolve_flash(request.query_params.get("msg")),
        admin_email=request.session.get(ADMIN_EMAIL_KEY, ""),
        generated_at=now,
    )


@router.get("/members", include_in_schema=False, dependencies=[Depends(require_admin)])
async def members_list(request: Request, db: Session = Depends(get_db)) -> object:
    filters = parse_filters(dict(request.query_params))
    members, total = query_members(db, filters)
    pages = max(1, (total + filters.per_page - 1) // filters.per_page)
    if filters.page > pages:
        # A page past the end (typo or hostile input) is shown as the last page so the
        # pager never renders "Trang 10000 / 3" and the table is not misleadingly empty.
        filters.page = pages
        members, total = query_members(db, filters)
    return render(
        request,
        "admin/members.html",
        members=members,
        total=total,
        page=filters.page,
        per_page=filters.per_page,
        per_page_choices=PER_PAGE_CHOICES,
        pages=pages,
        filters=filters.as_query(),
        statuses=[item.value for item in MemberStatus],
        status_labels=STATUS_LABELS,
        utm_sources=list_utm_sources(db),
        flash=resolve_flash(request.query_params.get("msg")),
        admin_email=request.session.get(ADMIN_EMAIL_KEY, ""),
        sort="-created_at",
    )


@router.get("/members.csv", include_in_schema=False, dependencies=[Depends(require_admin)])
async def members_export(request: Request, db: Session = Depends(get_db)) -> StreamingResponse:
    filters = parse_filters(dict(request.query_params))
    filters.per_page = 100000  # export honours filters, not pagination
    filters.page = 1
    members, total = query_members(db, filters)
    record_event(
        db,
        EventType.EXPORT,
        None,
        {
            "actor": request.session.get(ADMIN_EMAIL_KEY, ""),
            "rows": total,
            "filters": filters.as_query(),
            "format": "csv",
        },
    )
    db.commit()
    filename = export_filename()
    return StreamingResponse(
        iter_csv(members),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Total-Rows": str(total),
            "Cache-Control": "no-store",
        },
    )


@router.get("/members/{member_id}", include_in_schema=False, dependencies=[Depends(require_admin)])
async def member_detail(request: Request, member_id: str, db: Session = Depends(get_db)) -> object:
    from sqlalchemy import select

    from app.models import MemberEvent

    member: Member | None = get_member(db, member_id)
    if member is None:
        return _member_not_found(request)
    events = (
        db.execute(
            select(MemberEvent)
            .where(MemberEvent.member_id == member.id)
            .order_by(MemberEvent.created_at.desc())
            .limit(200)
        )
        .scalars()
        .all()
    )
    return render(
        request,
        "admin/member_detail.html",
        member=member,
        events=list(events),
        attribution=member.attribution,
        statuses=[item.value for item in MemberStatus],
        status_labels=STATUS_LABELS,
        notes_max_length=NOTE_MAX_LENGTH,
        flash=resolve_flash(request.query_params.get("msg")),
        admin_email=request.session.get(ADMIN_EMAIL_KEY, ""),
    )


@router.post(
    "/members/{member_id}",
    include_in_schema=False,
    dependencies=[Depends(require_admin), Depends(require_csrf)],
)
async def member_update_submit(
    request: Request, member_id: str, db: Session = Depends(get_db)
) -> object:
    """Management form: status / notes / marketing consent, audited as MEMBER_UPDATED."""
    member: Member | None = get_member(db, member_id)
    if member is None:
        return _member_not_found(request)
    # Built from the stored UUID, never from the raw path segment: the redirect target
    # cannot be influenced by the request.
    detail_path = f"/admin/members/{member.id}"

    edit, error = parse_member_edit(await request.form())
    if edit is None:
        # Invalid status or oversized notes: nothing is written, the admin gets the
        # allow-listed explanation back on the detail page (POST/redirect/GET).
        return _flash_redirect(detail_path, error or "error")

    changed = update_member(db, member, edit, actor=request.session.get(ADMIN_EMAIL_KEY, ""))
    return _flash_redirect(detail_path, "member_updated" if changed else "no_change")


@router.post(
    "/members/{member_id}/resend-verification",
    include_in_schema=False,
    dependencies=[Depends(require_admin), Depends(require_csrf)],
)
async def resend_member_verification(
    request: Request, member_id: str, db: Session = Depends(get_db)
) -> object:
    """Re-issue the verification email and bounce back to the page the admin came from."""
    form = await request.form()
    target = _safe_next(form.get("next"))

    member: Member | None = get_member(db, member_id)
    if member is None:
        return _flash_redirect(target, "member_not_found")
    if member.status == MemberStatus.VERIFIED.value:
        return _flash_redirect(target, "already_verified")
    if member.status != MemberStatus.PENDING.value:
        # Unsubscribed / blocked: a fresh verification email is precisely what those
        # members asked us to stop sending.
        return _flash_redirect(target, "not_pending")

    sent, _error = resend_verification(db, member)
    return _flash_redirect(target, "verification_sent" if sent else "error")


def _member_not_found(request: Request) -> object:
    return render(
        request,
        "error.html",
        status_code=404,
        title="Không tìm thấy member",
        message="Member này không tồn tại hoặc đã bị xoá.",
    )
