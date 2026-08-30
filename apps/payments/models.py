"""
Payment — the single source of truth for every rupee in the system.

Daily enrollments, monthly bills, installments, online bookings, material
sales and due collections all produce a Payment row. All reporting reads from
this one table; parallel money tables or per-module counters are exactly how
reporting figures drift apart from each other.

Money rules in force here (docs/00-OVERVIEW.md):
  * Decimal everywhere, never float.
  * Soft delete only — a financial record is never removed.
  * Per-branch receipt series, race-safe, issuable offline.
  * Idempotency keys, so a retry or a replayed offline mutation cannot
    double-charge a patient.
"""

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from apps.common.models import SoftDeleteModel, TimeStampedModel


class PaymentMethod(models.TextChoices):
    """
    Stored values match the frontend's keys in src/utils/paymentMethod.ts
    exactly — `bank_transfer`, not "Bank Transfer". The client maps keys to
    display labels itself.
    """

    CASH = "cash", "Cash"
    BKASH = "bkash", "bKash"
    NAGAD = "nagad", "Nagad"
    ROCKET = "rocket", "Rocket"
    BANK_TRANSFER = "bank_transfer", "Bank Transfer"
    ONLINE_PAYMENT = "online_payment", "Online Payment"
    CARD = "card", "Card"


class PaymentStatus(models.TextChoices):
    PAID = "paid", "Paid"
    DUE = "due", "Due"
    UPCOMING = "upcoming", "Upcoming"
    PARTIAL = "partial", "Partially refunded"
    CANCELLED = "cancelled", "Cancelled"
    REFUNDED = "refunded", "Refunded"
    VOID = "void", "Void"


class PaymentCategory(models.TextChoices):
    DAILY = "daily", "Daily"
    MONTHLY = "monthly", "Monthly"
    INSTALLMENT = "installment", "Installment"
    ONLINE = "online", "Online"
    MATERIAL_SALE = "material_sale", "Material sale"


class Payment(TimeStampedModel, SoftDeleteModel):
    transaction_id = models.CharField(max_length=48, unique=True, db_index=True)
    receipt_number = models.CharField(max_length=48, unique=True, db_index=True)

    patient = models.ForeignKey(
        "patients.Patient", on_delete=models.PROTECT, related_name="payments"
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    method = models.CharField(max_length=20, choices=PaymentMethod.choices, db_index=True)
    status = models.CharField(
        max_length=16, choices=PaymentStatus.choices, default=PaymentStatus.PAID, db_index=True
    )
    category = models.CharField(
        max_length=20, choices=PaymentCategory.choices, blank=True, db_index=True
    )

    collected_by = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL, related_name="collected_payments"
    )
    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="payments"
    )

    # Free-text description of what was paid for, shown on the receipt.
    # Denormalised deliberately: a receipt reprinted years later must read the
    # same even if the service was renamed or retired.
    description = models.CharField(max_length=255, blank=True)

    # Set on the client per user-initiated action. Unique so a replayed request
    # (network retry, or an offline queue flushing) returns the original
    # payment instead of charging twice.
    idempotency_key = models.CharField(
        max_length=64, unique=True, null=True, blank=True, db_index=True
    )

    # The device's clock is not trusted (docs/00, offline-first). Reports use
    # created_at, the server's own timestamp; this records what the client
    # claimed so a queued offline payment can still be traced to when it
    # actually happened.
    client_created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # Every report filters branch + date, and most also filter status.
            models.Index(fields=["branch", "-created_at"]),
            models.Index(fields=["branch", "status", "-created_at"]),
            models.Index(fields=["patient", "-created_at"]),
            models.Index(fields=["category"]),
        ]

    def __str__(self):
        return f"{self.receipt_number} — {self.amount}"

    @property
    def counts_as_revenue(self) -> bool:
        """
        Refunded and void payments are not revenue.

        Reporting must filter on this rule consistently; getting it wrong
        inflates every figure on every screen.
        """
        return self.status == PaymentStatus.PAID

    @property
    def refunded_amount(self) -> Decimal:
        """Total already returned across all approved refund requests."""
        approved = self.refund_requests.filter(status=RefundRequest.Status.APPROVED)
        return sum((r.amount for r in approved), Decimal("0.00"))

    @property
    def refundable_amount(self) -> Decimal:
        """What's left that could still be refunded."""
        return self.amount - self.refunded_amount


class RefundRequest(TimeStampedModel):
    """
    Separation of duties: a manager requests, an admin approves.

    The person who takes money must not be able to send it back on their own
    authority — otherwise pocketing cash and recording a "refund" leaves no
    trace. Nothing changes on the payment until an admin approves.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class BillAction(models.TextChoices):
        # The patient got their money back, so they owe it again.
        REOPEN = "reopen", "Reopen the bill"
        # Deliberately forgiving the money — e.g. closing a departing patient's
        # account. Logged as the decision it is.
        WRITE_OFF = "write_off", "Write off the bill"

    payment = models.ForeignKey(
        Payment, on_delete=models.PROTECT, related_name="refund_requests"
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    reason = models.TextField()

    requested_by = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL, related_name="refund_requests"
    )
    requested_at = models.DateTimeField(auto_now_add=True)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )

    reviewed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_refunds",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    bill_action = models.CharField(
        max_length=16, choices=BillAction.choices, default=BillAction.REOPEN
    )
    refund_method = models.CharField(
        max_length=20, choices=PaymentMethod.choices, blank=True
    )

    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="refund_requests"
    )

    class Meta:
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["status", "-requested_at"]),
            models.Index(fields=["branch", "status"]),
        ]

    def __str__(self):
        return f"Refund {self.amount} on {self.payment.receipt_number} ({self.status})"


class RefundRequestItem(models.Model):
    """
    Line items for a partial material-sale refund.

    `unit_price` is copied from the original sale rather than looked up live,
    so a later price change can't retroactively alter what a past refund was
    worth.
    """

    refund_request = models.ForeignKey(
        RefundRequest, on_delete=models.CASCADE, related_name="items"
    )
    material = models.ForeignKey(
        "materials.Material", on_delete=models.PROTECT, related_name="refund_items"
    )
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        indexes = [models.Index(fields=["refund_request"])]

    def __str__(self):
        return f"{self.quantity} × {self.material_id} @ {self.unit_price}"

    @property
    def line_total(self) -> Decimal:
        return self.unit_price * self.quantity
