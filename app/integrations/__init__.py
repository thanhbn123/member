"""Integrations package: post-verification side effects.

Webhook and Meta are both opt-in (see ``settings.webhook_enabled`` /
``settings.meta_enabled``) and both are fire-and-forget: they report status dicts
and never raise. Use :func:`notify_member_verified` to fire both.
"""

from app.integrations.dispatch import notify_member_verified
from app.integrations.meta import is_enabled as meta_is_enabled
from app.integrations.meta import send_complete_registration
from app.integrations.webhook import is_enabled as webhook_is_enabled
from app.integrations.webhook import send_member_verified

__all__ = [
    "meta_is_enabled",
    "notify_member_verified",
    "send_complete_registration",
    "send_member_verified",
    "webhook_is_enabled",
]
