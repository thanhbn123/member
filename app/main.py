"""FastAPI application factory - the composition root of the member service."""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.config import get_settings
from app.middleware import (
    CSRFCookieMiddleware,
    MaxBodySizeMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.schemas import APIError, fail
from app.web import render

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _client_detail(status_code: int) -> tuple[str, str]:
    table = {
        400: ("Yêu cầu không hợp lệ", "Dữ liệu gửi lên không hợp lệ."),
        401: ("Cần đăng nhập", "Bạn cần đăng nhập để truy cập trang này."),
        403: ("Không có quyền", "Yêu cầu đã bị từ chối vì lý do bảo mật."),
        404: ("Không tìm thấy trang", "Đường dẫn bạn truy cập không tồn tại."),
        405: ("Phương thức không được phép", "Phương thức HTTP này không được hỗ trợ cho đường dẫn này."),
        410: ("Liên kết đã hết hạn", "Liên kết này không còn hiệu lực."),
        413: ("Yêu cầu quá lớn", "Dữ liệu gửi lên vượt quá giới hạn cho phép."),
        429: ("Quá nhiều yêu cầu", "Bạn thao tác quá nhanh. Vui lòng thử lại sau ít phút."),
    }
    return table.get(status_code, ("Đã xảy ra lỗi", "Vui lòng thử lại sau."))


TOKEN_PATTERN = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)


class RedactTokensFilter(logging.Filter):
    """Keep one-time verification tokens (query strings) out of access/error logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                TOKEN_PATTERN.sub(r"\1***", value) if isinstance(value, str) else value
                for value in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: (TOKEN_PATTERN.sub(r"\1***", value) if isinstance(value, str) else value)
                for key, value in record.args.items()
            }
        if isinstance(record.msg, str) and "token=" in record.msg:
            record.msg = TOKEN_PATTERN.sub(r"\1***", record.msg)
        return True


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    redaction = RedactTokensFilter()
    for name in ("", "uvicorn.access", "uvicorn.error", "uvicorn"):
        logging.getLogger(name).addFilter(redaction)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "starting %s v%s env=%s email_mode=%s webhook=%s meta=%s api_key=%s",
        settings.app_name,
        __version__,
        settings.app_env,
        settings.email_mode,
        "on" if settings.webhook_enabled else "off",
        "on" if settings.meta_enabled else "off",
        "on" if settings.api_key_required else "off",
    )
    try:
        from sqlalchemy import text

        from app.db import get_engine

        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - operator feedback only
        logger.warning("database not reachable at startup (%s). Run: alembic upgrade head", exc)
    if settings.email_mode == "smtp" and not settings.smtp_host:
        logger.warning("EMAIL_MODE=smtp but SMTP_HOST is empty - verification emails will fail")
    if settings.is_production and not settings.api_key_required:
        logger.warning(
            "MEMBER_API_KEY is not set: the public API exposes member lookups to anyone. "
            "Set MEMBER_API_KEY and send X-API-Key from your systems."
        )
    if settings.is_production and settings.api_docs_enabled:
        logger.warning("API docs are enabled in production (/docs, /openapi.json) - set API_DOCS_ENABLED=false or protect them")
    if settings.admin_configured:
        logger.info("admin UI enabled for %s", settings.admin_email)
    else:
        logger.warning("admin UI disabled: set ADMIN_EMAIL and ADMIN_PASSWORD_HASH to enable it")
    yield
    logger.info("shutting down %s", settings.app_name)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=f"{settings.app_name} API",
        description=(
            "Member registration service: registration, email verification, attribution, "
            "admin and public API. Reusable by VIPORDER / VIP GROUP / other customer sites."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.api_docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.api_docs_enabled else None,
    )

    # --- middleware (added inside-out: the last one added is the outermost) ---
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=settings.session_cookie_name,
        max_age=settings.session_max_age_seconds,
        same_site=settings.session_same_site,
        https_only=settings.secure_cookies,
    )
    app.add_middleware(CSRFCookieMiddleware)
    app.add_middleware(MaxBodySizeMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"],
            max_age=600,
        )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from app.routers import admin, api_v1, public

    app.include_router(public.router)
    app.include_router(api_v1.router)
    app.include_router(admin.router)

    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=fail(
                exc.code, exc.message, exc.details, meta={"request_id": _request_id(request)}
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ()) if part != "body"),
                "message": error.get("msg", "invalid value"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=fail(
                "validation_error",
                "Dữ liệu gửi lên không hợp lệ.",
                details,
                meta={"request_id": _request_id(request)},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code in REDIRECT_STATUSES:
            location = (exc.headers or {}).get("Location", "/admin/login")
            return RedirectResponse(location, status_code=exc.status_code)
        if _wants_json(request):
            code = {
                401: "unauthorized",
                404: "not_found",
                405: "method_not_allowed",
                413: "payload_too_large",
                429: "rate_limited",
            }.get(exc.status_code, "request_failed")
            return JSONResponse(
                status_code=exc.status_code,
                content=fail(str(code), str(exc.detail), meta={"request_id": _request_id(request)}),
                headers=exc.headers,
            )
        title, message = _client_detail(exc.status_code)
        message = str(exc.detail) if exc.detail and exc.status_code in {403, 429} else message
        return render(
            request, "error.html", status_code=exc.status_code, title=title, message=message
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        if _wants_json(request):
            return JSONResponse(
                status_code=500,
                content=fail(
                    "internal_error",
                    "Đã xảy ra lỗi hệ thống.",
                    meta={"request_id": _request_id(request)},
                ),
            )
        return render(
            request,
            "error.html",
            status_code=500,
            title="Lỗi hệ thống",
            message="Đã xảy ra lỗi. Vui lòng thử lại sau.",
        )


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


app = create_app()
