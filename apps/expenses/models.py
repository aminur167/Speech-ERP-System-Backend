"""
Branch expenses with an approval workflow.

Important framing: this records money **already spent**. The form captures
`paid_to` and `payment_method` — past tense — and small amounts are
auto-approved with no request step. So a `pending` expense is cash that has
genuinely left the clinic awaiting *verification*, not permission.

That's why pending counts toward reported totals (docs/08). A pre-purchase
requisition workflow would be the opposite, and getting the two confused
understates spending and overstates profit.
"""

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from apps.common.models import IdempotentModel, SoftDeleteModel, TimeStampedModel


class Expense(TimeStampedModel, SoftDeleteModel, IdempotentModel):
    class Category(models.TextChoices):
        RENT = "rent", "Rent"
        UTILITIES = "utilities", "Utilities"
        SALARIES = "salaries", "Salaries"
        SUPPLIES = "supplies", "Supplies"
        EQUIPMENT = "equipment", "Equipment"
        MAINTENANCE = "maintenance", "Maintenance"
        MARKETING = "marketing", "Marketing"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    expense_code = models.CharField(max_length=32, unique=True, db_index=True)
    category = models.CharField(max_length=16, choices=Category.choices, db_index=True)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    description = models.CharField(max_length=255)
    paid_to = models.CharField(max_length=150)
    payment_method = models.CharField(max_length=20)

    # The manager's own note explaining why this was needed.
    remarks = models.TextField(blank=True)

    is_recurring = models.BooleanField(default=False)

    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="expenses"
    )
    submitted_by = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL, related_name="submitted_expenses"
    )

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )

    # The admin's note. Required when rejecting: the manager has already spent
    # this money, so "rejected" with no explanation is unworkable — and it
    # gives the clinic a written record if the amount is later recovered.
    review_note = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_expenses",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "-created_at"]),
            models.Index(fields=["branch", "status"]),
        ]

    def __str__(self):
        return f"{self.expense_code} — {self.amount}"

    @property
    def counts_toward_totals(self) -> bool:
        """
        Approved and pending count; rejected does not.

        Rejected means the clinic has declined to own the expense — it becomes
        the manager's liability or a corrected entry, not a clinic cost.
        """
        return self.status in {self.Status.APPROVED, self.Status.PENDING}
