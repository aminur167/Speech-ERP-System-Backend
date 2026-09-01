"""
Service catalog.

**Deliberately not branch-scoped** — the one real exception to the isolation
rule. The catalog is organisation-wide so every branch offers the same
packages at the same prices. Contrast with Materials (docs/06), which *are*
per-branch because physical stock doesn't move between locations.

Two distinct retirement concepts, easily confused:

  is_active = False  →  retired from sale. Existing enrollments keep billing
                        normally; nobody new can enroll.
  is_deleted = True  →  the package was a mistake. Hidden everywhere, but FK
                        references still resolve so old receipts don't degrade
                        to "Unknown service".
"""

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from apps.common.models import SoftDeleteModel, TimeStampedModel


class Service(TimeStampedModel, SoftDeleteModel):
    class Category(models.TextChoices):
        DAILY = "daily", "Daily"
        MONTHLY = "monthly", "Monthly"
        INSTALLMENT = "installment", "Installment"
        ONLINE = "online", "Online"

    class ReviewStatus(models.TextChoices):
        APPROVED = "approved", "Approved"
        PENDING = "pending", "Pending"
        REJECTED = "rejected", "Rejected"

    name = models.CharField(max_length=150)
    code = models.CharField(max_length=32, unique=True, db_index=True)
    category = models.CharField(max_length=16, choices=Category.choices, db_index=True)

    # Decimal, never float — see the money rules in docs/00-OVERVIEW.md.
    # A fee of 0 is meaningless (registration is free and has no Service row),
    # so the minimum is the smallest payable amount.
    fee = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    # Pre-discount price. The card strikes it through when it exceeds `fee`.
    original_fee = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    is_online = models.BooleanField(default=False)
    description = models.TextField(blank=True)

    # Free text by design — descriptive copy for the package card, not values
    # the system computes with. Nothing parses these.
    duration_label = models.CharField(max_length=100, blank=True)
    sessions_label = models.CharField(max_length=100, blank=True)
    expiry_label = models.CharField(max_length=100, blank=True)

    is_active = models.BooleanField(default=True, db_index=True)

    # A Manager may propose a new package (docs/03's Admin-only catalog CRUD
    # still governs edit/delete/activate/deactivate -- only creation opens up,
    # and only as a proposal). Defaults to APPROVED so an Admin's own creates
    # -- and every service that existed before this field did -- behave
    # exactly as before, with no separate self-approval step.
    review_status = models.CharField(
        max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.APPROVED, db_index=True
    )
    proposed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="proposed_services",
    )
    review_note = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_services",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["category", "name"]
        indexes = [
            models.Index(fields=["category", "is_active"]),
            models.Index(fields=["review_status"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def is_discounted(self) -> bool:
        return self.original_fee is not None and self.original_fee > self.fee

    def active_enrollment_count(self) -> int:
        """
        How many patients are currently on this package.

        Powers both the "N enrolled" card line and the delete-blocked error.
        For a whole list, use the bulk aggregate in services.py instead — this
        per-instance version would be an N+1 in a loop.
        """
        monthly = self.monthly_enrollments.filter(status="active").count()
        installment = self.installment_plans.filter(status="active").count()
        return monthly + installment
