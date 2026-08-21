"""
TrackFlow email service — Resend integration.

Provides a single function to send password-reset emails.
The caller is responsible for providing a valid reset URL.
"""

from __future__ import annotations

import logging

import resend

from services.api.auth_settings import FRONTEND_URL, RESEND_API_KEY

_LOGGER = logging.getLogger(__name__)


def send_password_reset_email(
    to_email: str,
    reset_token: str,
    *,
    frontend_url: str | None = None,
) -> bool:
    """Send a password-reset email via Resend.

    Args:
        to_email: The recipient's email address.
        reset_token: The signed password-reset JWT to include in the link.
        frontend_url: Override for the frontend base URL.  Falls back to
            ``FRONTEND_URL`` from settings.

    Returns:
        True if the email was accepted by Resend, False otherwise.

    Raises:
        RuntimeError: If ``RESEND_API_KEY`` or ``FRONTEND_URL`` is not
            configured.
    """
    api_key = RESEND_API_KEY
    base_url = frontend_url or FRONTEND_URL

    if not api_key:
        raise RuntimeError(
            "RESEND_API_KEY is not configured — cannot send email."
        )
    if not base_url:
        raise RuntimeError(
            "FRONTEND_URL is not configured — cannot build reset link."
        )

    resend.api_key = api_key

    reset_url = f"{base_url.rstrip('/')}/reset-password?token={reset_token}"

    try:
        response = resend.Emails.send({
            "from": "TrackFlow <onboarding@resend.dev>",
            "to": [to_email],
            "subject": "Reset your TrackFlow password",
            "text": (
                f"Hello,\n\n"
                f"You requested a password reset for your TrackFlow account.\n\n"
                f"Click the link below to reset your password. "
                f"This link expires in 30 minutes.\n\n"
                f"{reset_url}\n\n"
                f"If you did not request this, you can safely ignore this email.\n\n"
                f"— TrackFlow Team"
            ),
            "html": (
                f"<p>Hello,</p>"
                f"<p>You requested a password reset for your TrackFlow account.</p>"
                f'<p><a href="{reset_url}">Reset your password</a></p>'
                f"<p>This link expires in 30 minutes.</p>"
                f"<p>If you did not request this, you can safely ignore this email.</p>"
                f"<p>— TrackFlow Team</p>"
            ),
        })
        _LOGGER.info("Password-reset email sent to %s (Resend id=%s)", to_email, response.get("id"))
        return True
    except Exception:
        _LOGGER.exception("Failed to send password-reset email to %s", to_email)
        return False