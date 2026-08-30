"""
Branch model — the tenant boundary for the whole system.

Almost every other record hangs off a branch, and `apps/common/mixins.py`
enforces that a Manager only ever sees their own.

Deliberate difference from the frontend mock: the mock kept `managerName`,
`managerEmail`, and a plaintext `managerPassword` as columns on the branch, and
showed the password in the Admin UI. Here the manager is a real `User` row —
`Branch.manager` points at it, credentials are hashed, and the password is
never readable back. Creating a branch provisions the manager account in the
same transaction (see services.py), which is what the mock's
`upsertManagerAccount` was imitating.
"""

from django.db import models

from apps.common.models import SoftDeleteModel, TimeStampedModel


class Branch(TimeStampedModel, SoftDeleteModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=150)
    code = models.CharField(max_length=32, unique=True, db_index=True)  # BR-DHK-001
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )

    address = models.TextField()
    phone = models.CharField(max_length=32)

    # The branch's manager account. Nullable so a branch can exist briefly
    # before its manager is provisioned, and SET_NULL so removing a user never
    # cascades into deleting a branch and everything under it.
    manager = models.OneToOneField(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="managed_branch",
    )

    therapist_count = models.PositiveIntegerField(default=0)
    support_count = models.PositiveIntegerField(default=0)
    opened_at = models.DateField()

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "branches"
        indexes = [models.Index(fields=["status"])]

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def short_code(self) -> str:
        """
        The branch part of the code — "DHK" from "BR-DHK-001".

        Used to build per-branch receipt/patient codes (RCPT-DHK-2026-00001),
        which is what lets a branch issue codes offline without colliding with
        another branch (docs/04-payments-core.md).
        """
        parts = self.code.split("-")
        return parts[1] if len(parts) >= 2 else self.code
