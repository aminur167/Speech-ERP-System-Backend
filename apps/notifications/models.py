from django.conf import settings
from django.db import models

from apps.common.models import TimeStampedModel


class Notification(TimeStampedModel):
    """
    In-app notification for one user.

    Deliberately per-user, not per-branch: a branch's Manager account is what
    actually receives it, which already scopes it to that branch in practice
    (one manager per branch today) without needing a separate branch field or
    fan-out logic if that ever changes.
    """

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    title = models.CharField(max_length=200)
    message = models.TextField()
    # Where the frontend navigates on click, e.g. "/admin/services" -- blank
    # if there's nowhere useful to send the user.
    link = models.CharField(max_length=200, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "is_read"])]

    def __str__(self):
        return f"{self.title} -> {self.recipient_id}"
