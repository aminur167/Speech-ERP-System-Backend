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

from django.db import connection

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

    One statement: the notifications are inserted straight from a SELECT of
    the admins, rather than reading the admins into Python and then writing
    one row each. The audience is the same as ever -- every active user with
    the admin role, bar `exclude`.
    """
    from apps.accounts.models import User

    exclude_id = getattr(exclude, "pk", None) if exclude is not None else None
    sql = f"""
        INSERT INTO {Notification._meta.db_table}
            (created_at, updated_at, recipient_id, title, message, link, is_read)
        SELECT clock_timestamp(), clock_timestamp(), id, %s, %s, %s, FALSE
        FROM {User._meta.db_table}
        WHERE role = %s AND is_active
    """
    params = [title, message, link, User.Role.ADMIN]
    if exclude_id is not None:
        sql += " AND id <> %s"
        params.append(exclude_id)

    with connection.cursor() as cursor:
        cursor.execute(sql, params)


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
