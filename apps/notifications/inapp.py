"""
In-app notifications — distinct from services.py's SMS/WhatsApp/email senders.
These write to the Notification table the frontend's bell icon polls; they
never leave the app and never need Twilio/SMTP configured.

`notify_admins` / `notify_requester` are the two halves of one rule: anything
a manager sends up for a decision reaches every admin, and whatever the admin
decides reaches the manager who asked. Every request flow (package proposal,
expense above the approval threshold, refund request) uses this same pair, so
a new one only has to call these rather than reinvent the routing.
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


def notify_admins(*, title: str, message: str, link: str = "", exclude=None) -> None:
    """
    Something needs an admin's decision.

    Admin is organisation-wide (no branch), so this is every admin rather
    than one — whoever gets to it first decides. `exclude` keeps an admin
    from being told about their own action.
    """
    from apps.accounts.models import User

    recipients = User.objects.filter(role=User.Role.ADMIN, is_active=True)
    if exclude is not None and getattr(exclude, "pk", None) is not None:
        recipients = recipients.exclude(pk=exclude.pk)

    notify_many(recipients=recipients, title=title, message=message, link=link)


def notify_requester(
    *, actor, recipient, title: str, message: str, link: str = ""
) -> None:
    """
    A decision going back to whoever asked for it.

    No-ops when there's nobody to tell (a record created before this
    existed, or a seeded one) or when the decider and the requester are the
    same person — an admin approving their own submission doesn't need to be
    notified about it.
    """
    if recipient is None:
        return
    if actor is not None and getattr(actor, "pk", None) == recipient.pk:
        return

    notify(recipient=recipient, title=title, message=message, link=link)
