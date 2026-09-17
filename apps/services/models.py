"""
Service catalog.

Branch-scoped, like Materials (docs/06): each branch owns its own packages.
A Manager's proposal is filed under their own branch; Admin creates directly
for a specific branch (the branch drill-down page), never organisation-wide.
`code` is therefore unique per branch, not globally -- two branches may each
run their own "MON-INDIV".

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
from django.utils import timezone

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

    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="services"
    )
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=32, db_index=True)
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
        constraints = [
            models.UniqueConstraint(fields=["branch", "code"], name="unique_service_code_per_branch"),
        ]
        indexes = [
            models.Index(fields=["branch", "category", "is_active"]),
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


class PackageActionRequest(TimeStampedModel):
    """
    A Manager asking Admin for permission to change one of their branch's
    packages: edit it, delete it, or switch it off or on.

    Separation of duties, the same as refunds. The catalog is what every
    enrollment bills against, so a branch does not change it on its own
    authority. The request records *why*, Admin records the decision, and an
    approval is a **one-time permission for that one action on that one
    package** — used up the moment the Manager performs it, and void if it
    lapses unused. It is not a blanket unlock for the branch.
    """

    class Action(models.TextChoices):
        EDIT = "edit", "Edit"
        DELETE = "delete", "Delete"
        DEACTIVATE = "deactivate", "Deactivate"
        ACTIVATE = "activate", "Activate"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        # The Manager performed the approved action; the permission is spent.
        USED = "used", "Used"

    service = models.ForeignKey(
        Service, on_delete=models.PROTECT, related_name="action_requests"
    )
    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="package_action_requests"
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    reason = models.TextField()
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )

    requested_by = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL,
        related_name="package_action_requests",
    )
    reviewed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_package_actions",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    # Set on approval. An approval nobody used should not stay a live key to
    # the catalog forever.
    expires_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["service", "action", "status"]),
        ]
        constraints = [
            # Two Managers — or one double-click — cannot open the same
            # question twice while Admin is still deciding it.
            models.UniqueConstraint(
                fields=["service", "action"],
                condition=models.Q(status="pending"),
                name="one_pending_request_per_package_action",
            )
        ]

    def __str__(self):
        return f"{self.get_action_display()} {self.service_id} ({self.status})"

    @property
    def is_expired(self) -> bool:
        return (
            self.status == self.Status.APPROVED
            and self.expires_at is not None
            and self.expires_at <= timezone.now()
        )

    @property
    def effective_status(self) -> str:
        """`expired` is derived rather than stored, so it is never stale."""
        return "expired" if self.is_expired else self.status
