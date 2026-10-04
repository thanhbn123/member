"""Landing page (``GET /``) - rendering, configuration and the reused registration form.

The landing page is the public entry point, so these tests cover three contracts:

* the template contract with ``base.html`` (CSP: no inline style attribute, a nonce on
  every ``<script>``, autoescaping of configuration values),
* that every piece of copy, benefit, link and contact detail comes from configuration
  and that hostile configuration (``javascript:`` URLs, 5 000-char descriptions,
  20 benefits) can never break the page,
* that the embedded form behaves exactly like ``/register``: same POST target, same
  CSRF token taken from ``/``, same hidden attribution inputs, and a real member at the
  end of it.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.models import Member

CSRF_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
SCRIPT_RE = re.compile(r"<script\b[^>]*>")

# Deterministic landing configuration: every value a landing test does not override is
# pinned here, so a developer's local `.env` (or a future default) cannot change the
# outcome of this module.
BASE_LANDING_ENV: dict[str, str] = {
    "BRAND_TAGLINE": "Đăng ký thành viên",
    "BRAND_LOGO_URL": "",
    "BRAND_SUPPORT_EMAIL": "",
    "BRAND_PHONE": "",
    "BRAND_ADDRESS": "",
    "BRAND_FACEBOOK_URL": "",
    "BRAND_ZALO_URL": "",
    "LANDING_HERO_TITLE": "",
    "LANDING_HERO_SUBTITLE": "",
    "LANDING_HERO_IMAGE_URL": "",
    "LANDING_BENEFITS": "",
    "LANDING_CTA_TEXT": "Đăng ký ngay",
    "LANDING_SHOW_FORM": "true",
    "GA4_MEASUREMENT_ID": "",
    "META_PIXEL_ID": "",
}


@pytest.fixture
def landing_env(settings_env):
    """Pin the landing configuration, then let a test override any single value."""

    def apply(**overrides: str):
        settings_env(**{**BASE_LANDING_ENV, **overrides})

    apply()
    return apply


def csrf_token(html: str) -> str:
    match = CSRF_RE.search(html)
    assert match, "no csrf_token field rendered"
    return match.group(1)


# --------------------------------------------------------------------------- page


def test_root_renders_the_landing_page(client, landing_env):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "MEMBER" in response.text  # brand name from configuration
    assert 'href="/static/css/landing.css"' in response.text
    assert 'id="dang-ky"' in response.text
    assert 'action="/register"' in response.text
    assert csrf_token(response.text)


def test_root_is_no_longer_a_redirect(client, landing_env):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert response.status_code != 307
    assert "location" not in response.headers
    assert "landing.css" in response.text


def test_register_page_keeps_working_for_existing_links(client, landing_env):
    page = client.get("/register")
    assert page.status_code == 200
    assert 'action="/register"' in page.text
    assert "landing.css" not in page.text  # /register is untouched by the landing work
    assert client.get("/health").json()["status"] == "ok"


# --------------------------------------------------------------------------- form


def test_embedded_form_registers_a_member(client, db_session, mailbox, landing_env):
    page = client.get("/")
    email = "landing-flow@example.com"
    response = client.post(
        "/register",
        data={
            "full_name": "Nguyễn Văn A",
            "email": email,
            "phone": "0901234567",
            "company": "Công ty TNHH ABC",
            "consent_marketing": "true",
            "csrf_token": csrf_token(page.text),
            "utm_source": "landing",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert "/check-email" in response.headers["location"]

    member = db_session.execute(select(Member).where(Member.email == email)).scalar_one()
    assert member.status == "pending"
    assert member.full_name == "Nguyễn Văn A"
    assert mailbox.latest()["to"] == email
    assert member.attribution is not None
    assert member.attribution.utm_source == "landing"


@pytest.mark.parametrize(
    "field",
    [
        "csrf_token",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "landing_url",
        "referrer",
        "fbp",
        "fbc",
        "full_name",
        "email",
        "phone",
        "company",
        "consent_marketing",
    ],
)
def test_landing_form_has_the_same_fields_as_register(client, landing_env, field):
    page = client.get("/")
    assert f'name="{field}"' in page.text


def test_landing_form_prefills_attribution_from_the_query_string(client, landing_env):
    page = client.get("/?utm_source=facebook&utm_campaign=khai-truong")
    assert 'name="utm_source" value="facebook"' in page.text
    assert 'name="utm_campaign" value="khai-truong"' in page.text


def test_show_form_false_renders_a_cta_link_instead_of_the_form(client, landing_env):
    landing_env(LANDING_SHOW_FORM="false")
    page = client.get("/")
    assert page.status_code == 200
    assert "<form" not in page.text
    assert 'action="/register"' not in page.text
    assert 'href="/register"' in page.text
    assert "Đăng ký ngay" in page.text  # the CTA label still comes from configuration


def test_errors_and_submitted_values_come_back_on_a_422_render(landing_env):
    """The landing template honours the register context contract (``form``/``errors``).

    ``POST /register`` re-renders ``register.html`` on a 422, so this pins the landing
    template's side of the shared contract directly (rendering it with the same context
    the router builds) instead of changing the POST route.
    """
    html = _render_landing(
        form={
            "full_name": "Nguyễn Văn A",
            "email": "khong-hop-le",
            "phone": "0901234567",
            "company": "Công ty TNHH ABC",
            "consent_marketing": True,
        },
        errors=["Email không hợp lệ"],
        consent_required=True,
    )
    assert "Email không hợp lệ" in html
    assert 'aria-live="polite"' in html
    assert 'value="Nguyễn Văn A"' in html
    assert 'value="khong-hop-le"' in html
    assert 'value="0901234567"' in html
    assert 'value="Công ty TNHH ABC"' in html
    assert "checked" in html  # the consent checkbox survives a failed submit


# ---------------------------------------------------------------------- benefits


def test_default_benefits_render_when_unset(client, landing_env):
    page = client.get("/")
    assert page.text.count('class="benefit-card"') == 3
    for title in ("Đăng ký nhanh", "Xác minh email", "Ưu đãi thành viên"):
        assert title in page.text


def test_custom_benefits_render_in_order(client, landing_env):
    landing_env(LANDING_BENEFITS="🌿|Thư giãn|Không gian yên tĩnh;;🧘|Thiền định|Lớp thiền mỗi sáng")
    page = client.get("/")
    assert page.text.count('class="benefit-card"') == 2
    assert '<h3 class="benefit-title">Thư giãn</h3>' in page.text
    assert '<p class="benefit-text">Không gian yên tĩnh</p>' in page.text
    assert page.text.index("Thư giãn") < page.text.index("Thiền định")


def test_malformed_benefits_are_dropped_silently(client, landing_env):
    landing_env(LANDING_BENEFITS="chỉ-có-một-phần;;icon|thiếu-mô-tả;;a|b|c|d;;|| ;;🌿|Hợp lệ|Mô tả hợp lệ")
    page = client.get("/")
    assert page.status_code == 200
    assert page.text.count('class="benefit-card"') == 1
    assert "Hợp lệ" in page.text
    assert "Mô tả hợp lệ" in page.text
    # the malformed items are dropped, not rendered as text
    assert "chỉ-có-một-phần" not in page.text
    assert "thiếu-mô-tả" not in page.text
    assert "a|b|c|d" not in page.text


def test_benefits_are_capped_and_long_text_is_truncated(client, landing_env):
    items = [f"i{n}|Tiêu đề {n}|Mô tả {n}" for n in range(5)]
    items.append(f"⭐|Mô tả dài|{'x' * 5000}")  # 6th card: description must be truncated
    items += [f"i{n}|Tiêu đề {n}|Mô tả {n}" for n in range(5, 9)]  # over the cap
    landing_env(LANDING_BENEFITS=";;".join(items))

    page = client.get("/")
    assert page.status_code == 200
    assert page.text.count('class="benefit-card"') == 6
    assert "Tiêu đề 4" in page.text
    assert "Tiêu đề 5" not in page.text
    assert "x" * 240 in page.text
    assert "x" * 241 not in page.text


def test_benefit_text_is_escaped(client, landing_env):
    landing_env(LANDING_BENEFITS="<b>|Thẻ HTML|<script>alert(1)</script>")
    page = client.get("/")
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


# ------------------------------------------------------------------------- hero


def test_hero_title_falls_back_to_the_tagline(client, landing_env):
    landing_env(LANDING_HERO_TITLE="", BRAND_TAGLINE="Khỏe hơn mỗi ngày")
    page = client.get("/")
    assert '<h1 class="hero-title" id="hero-title">Khỏe hơn mỗi ngày</h1>' in page.text


def test_configured_hero_title_and_subtitle_win(client, landing_env):
    landing_env(LANDING_HERO_TITLE="Nghỉ dưỡng phục hồi", LANDING_HERO_SUBTITLE="Thân - tâm - trí")
    page = client.get("/")
    assert '<h1 class="hero-title" id="hero-title">Nghỉ dưỡng phục hồi</h1>' in page.text
    assert "Thân - tâm - trí" in page.text


def test_hero_image_renders_only_for_an_http_url(client, landing_env):
    assert "hero-image" not in client.get("/").text

    landing_env(LANDING_HERO_IMAGE_URL="https://cdn.example.com/hero.jpg")
    page = client.get("/")
    assert 'src="https://cdn.example.com/hero.jpg"' in page.text
    assert "hero-with-media" in page.text

    landing_env(LANDING_HERO_IMAGE_URL="javascript:alert(1)")
    page = client.get("/")
    assert "hero-image" not in page.text
    assert "javascript:" not in page.text


def test_cta_text_comes_from_configuration(client, landing_env):
    landing_env(LANDING_CTA_TEXT="Nhận ưu đãi")
    page = client.get("/")
    assert page.text.count("Nhận ưu đãi") >= 3  # header, hero and the submit button
    assert "Đăng ký ngay" not in page.text


# ---------------------------------------------------------------------- contact


def test_settings_reject_non_http_urls(settings_env):
    from app.config import get_settings

    settings_env(
        LANDING_HERO_IMAGE_URL="javascript:alert(1)",
        BRAND_FACEBOOK_URL="ftp://example.com/page",
        BRAND_ZALO_URL="https://zalo.me/123456789",
    )
    settings = get_settings()
    assert settings.landing_hero_image_url == ""
    assert settings.brand_facebook_url == ""
    assert settings.brand_zalo_url == "https://zalo.me/123456789"


def test_configured_contact_details_render(client, landing_env):
    landing_env(
        BRAND_PHONE="0901 234 567",
        BRAND_ADDRESS="123 Trần Phú, Nha Trang",
        BRAND_FACEBOOK_URL="https://facebook.com/quangkhoiwellness",
        BRAND_ZALO_URL="https://zalo.me/123456789",
    )
    page = client.get("/")
    assert 'id="lien-he"' in page.text
    assert 'href="tel:0901234567"' in page.text
    assert "123 Trần Phú, Nha Trang" in page.text
    assert 'href="https://facebook.com/quangkhoiwellness" rel="noopener noreferrer" target="_blank"' in page.text
    assert 'href="https://zalo.me/123456789"' in page.text


def test_hostile_contact_urls_are_not_rendered(client, landing_env):
    landing_env(
        BRAND_FACEBOOK_URL="javascript:alert(1)",
        BRAND_ZALO_URL="data:text/html,<script>alert(1)</script>",
    )
    page = client.get("/")
    assert page.status_code == 200
    assert "javascript:" not in page.text
    assert "data:text/html" not in page.text
    assert "Theo dõi trên Facebook" not in page.text
    assert "Liên hệ qua Zalo" not in page.text


def test_contact_section_is_hidden_when_nothing_is_configured(client, landing_env):
    page = client.get("/")
    assert 'id="lien-he"' not in page.text
    assert "contact-list" not in page.text


# -------------------------------------------------------------------------- CSP


def test_page_has_no_inline_style_attribute(client, landing_env):
    page = client.get("/")
    assert 'style="' not in page.text


def test_every_script_carries_the_csp_nonce(client, landing_env):
    page = client.get("/")
    scripts = SCRIPT_RE.findall(page.text)
    assert scripts, "the landing page must load the shared registration helper"
    for tag in scripts:
        assert "nonce=" in tag, tag
    assert any('src="/static/js/register.js"' in tag for tag in scripts)


# ---------------------------------------------------------------------- helpers


def _render_landing(**overrides) -> str:
    """Render ``landing.html`` with the exact context ``GET /`` builds."""
    from starlette.requests import Request as StarletteRequest

    from app.routers.public import landing_context
    from app.web import base_context, templates

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 1),
        "state": {},
    }
    context = base_context(StarletteRequest(scope))
    context.update(landing_context())
    context.update(
        {
            "form": {},
            "errors": [],
            "attribution": {},
            "success_message": None,
            "error_message": None,
            "consent_required": True,
        }
    )
    context.update(overrides)
    return templates.env.get_template("landing.html").render(**context)
