"""
Reminder delivery — SMS, WhatsApp, and email.

Every sender here follows the same rule Sentry and Cloudinary already
established in this codebase: work with zero configuration in development
(no crash, no real message sent, just a log line saying what *would* have
gone out), and only actually deliver once real credentials are set in
production. A clinic's first deploy should never be blocked on having a
Twilio account.

Uses Twilio's plain REST API over `urllib` rather than the `twilio` SDK --
no new dependency for something this codebase already has a working pattern
for (see Cloudinary: validated by URL shape, no SDK either). Swap in the SDK
later if the raw HTTP calls ever become a maintenance burden.
"""

import base64
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

_TWILIO_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


def _twilio_configured() -> bool:
    return bool(
        getattr(settings, "TWILIO_ACCOUNT_SID", "")
        and getattr(settings, "TWILIO_AUTH_TOKEN", "")
    )


def _send_via_twilio(*, to: str, from_: str, body: str) -> bool:
    account_sid = settings.TWILIO_ACCOUNT_SID
    auth_token = settings.TWILIO_AUTH_TOKEN
    url = _TWILIO_MESSAGES_URL.format(sid=account_sid)
    data = urllib.parse.urlencode({"To": to, "From": from_, "Body": body}).encode()
    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()

    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Authorization", f"Basic {credentials}")

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except urllib.error.URLError:
        logger.exception("Failed to send message to %s via Twilio", to)
        return False


def send_sms(to_phone: str, message: str) -> bool:
    if not to_phone:
        return False
    from_number = getattr(settings, "TWILIO_SMS_FROM", "")
    if not (_twilio_configured() and from_number):
        logger.info("[notifications] SMS not sent (Twilio not configured) — to %s: %s", to_phone, message)
        return False
    return _send_via_twilio(to=to_phone, from_=from_number, body=message)


def send_whatsapp(to_phone: str, message: str) -> bool:
    if not to_phone:
        return False
    from_number = getattr(settings, "TWILIO_WHATSAPP_FROM", "")
    if not (_twilio_configured() and from_number):
        logger.info(
            "[notifications] WhatsApp not sent (Twilio not configured) — to %s: %s", to_phone, message
        )
        return False
    # Twilio's WhatsApp channel addresses both sides with a "whatsapp:" prefix.
    return _send_via_twilio(to=f"whatsapp:{to_phone}", from_=f"whatsapp:{from_number}", body=message)


def send_email_notification(to_email: str, subject: str, body: str) -> bool:
    if not to_email:
        return False
    try:
        send_mail(
            subject, body, settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=False
        )
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to_email)
        return False
