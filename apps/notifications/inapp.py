"""
In-app notifications — distinct from services.py's SMS/WhatsApp/email senders.
These write to the Notification table the frontend's bell icon polls; they
never leave the app and never need Twilio/SMTP configured.
"""

from apps.notifications.models import Notification


def notify(*, recipient, title: str, message: str, link: str = "") -> Notification:
    return Notification.objects.create(
        recipient=recipient, title=title, message=message, link=link
    )


def notify_many(*, recipients, title: str, message: str, link: str = "") -> None:
    Notification.objects.bulk_create(
        [
            Notification(recipient=recipient, title=title, message=message, link=link)
            for recipient in recipients
        ]
    )
