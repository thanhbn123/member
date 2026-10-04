"""Admin UI: login, member list with filters/pagination, detail view, CSV export."""

from __future__ import annotations

import logging

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
from app.models import EventType, Member, MemberStatus
from app.security import client_ip, hash_ip, verify_password
from app.services.admin import (
    PER_PAGE_CHOICES,
    export_filename,
    iter_csv,
    list_utm_sources,
    parse_filters,
    query_members,
)
from app.services.events import record_event
from app.services.members import get_member
from app.web import render

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def admin_index(request: Request) -> RedirectResponse:
    if not admin_logged_in(request):
        return RedirectResponse("/admin/login", status_code=303)
    return RedirectResponse("/admin/members", status_code=303)


@router.get("/login", include_in_schema=False)
async def login_form(request: Request) -> object:
    if admin_logged_in(request):
        return RedirectResponse("/admin/members", status_code=303)
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

    ok_login = (
        settings.admin_configured
        and email == settings.admin_email.strip().lower()
        and verify_password(password, settings.admin_password_hash)
    )

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
    return RedirectResponse("/admin/members", status_code=303)


@router.post("/logout", include_in_schema=False, dependencies=[Depends(require_csrf)])
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


@router.get("/members", include_in_schema=False, dependencies=[Depends(require_admin)])
async def members_list(request: Request, db: Session = Depends(get_db)) -> object:
    filters = parse_filters(dict(request.query_params))
    members, total = query_members(db, filters)
    pages = max(1, (total + filters.per_page - 1) // filters.per_page)
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
        utm_sources=list_utm_sources(db),
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
        return render(
            request,
            "error.html",
            status_code=404,
            title="Không tìm thấy member",
            message="Member này không tồn tại hoặc đã bị xoá.",
        )
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
        admin_email=request.session.get(ADMIN_EMAIL_KEY, ""),
    )
