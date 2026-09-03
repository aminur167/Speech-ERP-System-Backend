"""
Monthly enrollments, installment plans, and online bookings.

Two design points carry most of the weight here:

**`amount_paid` instead of a binary paid flag.** Partial refunds mean a bill
can hold a partial balance — refunding ৳2,000 of a ৳5,000 bill leaves ৳2,000
owing, not ৳5,000. Outstanding Due must sum `amount − amount_paid`; summing
`amount` overstates what the patient owes (docs/04).

**Bills are rows, not an embedded list.** The frontend mock keeps bills inside
the enrollment object. Here each bill is its own row so due dates, payments
and reporting can query them directly.
"""

from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from apps.common.models import TimeStampedModel


class EnrollmentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    TERMINATED = "terminated", "Terminated"


class BillStatus(models.TextChoices):
    PAID = "paid", "Paid"
    DUE = "due", "Due"
    UPCOMING = "upcoming", "Upcoming"
    OVERDUE = "overdue", "Overdue"
    # Deliberately forgiven via an admin-approved refund write-off. Excluded
    # from Outstanding Due — the only sanctioned way to close an uncollectable
    # enrollment, since termination is otherwise blocked while dues exist.
    WRITTEN_OFF = "written_off", "Written off"


class PayableMixin(models.Model):
    """
    Shared behaviour for monthly bills and installments.

    Both are "an amount owed by a date, partially or fully settled", and the
    oldest-first rule, overdue detection and Outstanding Due all treat them
    identically — so the balance logic lives in one place.
    """

    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    # How much has actually been settled. Enables partial refunds; also makes
    # partial *payments* representable, which is deliberately NOT enabled yet
    # (the confirmed rule is full payment by the 5th).
    amount_paid = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    status = models.CharField(
        max_length=16, choices=BillStatus.choices, default=BillStatus.UPCOMING, db_index=True
    )
    due_date = models.DateField(db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    payment = models.ForeignKey(
        "payments.Payment",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        abstract = True

    @property
    def Status(self):
        """Lets shared code reach the choices without importing them."""
        return BillStatus

    @property
    def outstanding(self) -> Decimal:
        """What's still owed. Written-off amounts are owed by nobody."""
        if self.status == BillStatus.WRITTEN_OFF:
            return Decimal("0.00")
        return max(Decimal("0.00"), self.amount - self.amount_paid)

    @property
    def is_settled(self) -> bool:
        return self.amount_paid >= self.amount

    def is_overdue(self, *, on: date | None = None) -> bool:
        """
        Unpaid past its due date.

        Derived, never stored: a stored flag would go stale the moment a date
        passed without anything writing to the row.
        """
        if self.status in {BillStatus.PAID, BillStatus.WRITTEN_OFF} or self.is_settled:
            return False
        reference = on or date.today()
        return reference > self.due_date

    def effective_status(self, *, on: date | None = None) -> str:
        """Status with overdue applied, for display and reporting."""
        if self.status in {BillStatus.PAID, BillStatus.WRITTEN_OFF}:
            return self.status
        if self.is_settled:
            return BillStatus.PAID
        if self.is_overdue(on=on):
            return BillStatus.OVERDUE
        return self.status


class MonthlyEnrollment(TimeStampedModel):
    patient = models.ForeignKey(
        "patients.Patient", on_delete=models.PROTECT, related_name="monthly_enrollments"
    )
    service = models.ForeignKey(
        "services.Service", on_delete=models.PROTECT, related_name="monthly_enrollments"
    )
    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="monthly_enrollments"
    )
    status = models.CharField(
        max_length=16, choices=EnrollmentStatus.choices,
        default=EnrollmentStatus.ACTIVE, db_index=True,
    )
    terminated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["patient", "status"]),
            models.Index(fields=["service", "status"]),
        ]

    def __str__(self):
        return f"{self.patient_id} — {self.service_id} (monthly)"

    @property
    def is_active(self) -> bool:
        return self.status == EnrollmentStatus.ACTIVE

    def outstanding_total(self) -> Decimal:
        return sum((bill.outstanding for bill in self.bills.all()), Decimal("0.00"))

    def oldest_unpaid_bill(self):
        """
        The only bill that may be paid next.

        Oldest-first: a patient can't settle September while August is
        outstanding, or old debt ages forever while they keep paying the
        current month.
        """
        return (
            self.bills.exclude(status__in=[BillStatus.PAID, BillStatus.WRITTEN_OFF])
            .filter(amount_paid__lt=models.F("amount"))
            .order_by("month")
            .first()
        )


class MonthlyBill(PayableMixin):
    enrollment = models.ForeignKey(
        MonthlyEnrollment, on_delete=models.CASCADE, related_name="bills"
    )
    month = models.CharField(max_length=7, db_index=True)  # "2026-08"
    label = models.CharField(max_length=32)                # "August 2026"

    class Meta:
        ordering = ["month"]
        constraints = [
            # The monthly generation job must be safely re-runnable — running
            # it twice in a month must not produce a duplicate bill. Enforced
            # here rather than trusting the scheduler to fire exactly once.
            models.UniqueConstraint(
                fields=["enrollment", "month"], name="uniq_bill_per_enrollment_month"
            )
        ]
        indexes = [models.Index(fields=["status", "due_date"])]

    def __str__(self):
        return f"{self.label} — {self.amount}"


class InstallmentPlan(TimeStampedModel):
    patient = models.ForeignKey(
        "patients.Patient", on_delete=models.PROTECT, related_name="installment_plans"
    )
    service = models.ForeignKey(
        "services.Service", on_delete=models.PROTECT, related_name="installment_plans"
    )
    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="installment_plans"
    )
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=16, choices=EnrollmentStatus.choices,
        default=EnrollmentStatus.ACTIVE, db_index=True,
    )
    terminated_at = models.DateTimeField(null=True, blank=True)

    # The window the manager agreed with the patient: the first installment
    # falls due on `starts_on`, the last on `ends_on`, and the plan has to be
    # cleared inside it. Null on plans created before this existed -- those
    # keep the monthly-on-the-5th schedule they were built with.
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["patient", "status"]),
            models.Index(fields=["service", "status"]),
        ]

    def __str__(self):
        return f"{self.patient_id} — {self.service_id} (installment)"

    @property
    def is_active(self) -> bool:
        return self.status == EnrollmentStatus.ACTIVE

    def outstanding_total(self) -> Decimal:
        return sum((i.outstanding for i in self.installments.all()), Decimal("0.00"))

    def oldest_unpaid_installment(self):
        return (
            self.installments.exclude(status__in=[BillStatus.PAID, BillStatus.WRITTEN_OFF])
            .filter(amount_paid__lt=models.F("amount"))
            .order_by("index")
            .first()
        )


class Installment(PayableMixin):
    plan = models.ForeignKey(
        InstallmentPlan, on_delete=models.CASCADE, related_name="installments"
    )
    index = models.PositiveIntegerField()
    label = models.CharField(max_length=32)  # "1st Installment"

    class Meta:
        ordering = ["index"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "index"], name="uniq_installment_per_plan_index"
            )
        ]
        indexes = [models.Index(fields=["status", "due_date"])]

    def __str__(self):
        return f"{self.label} — {self.amount}"


class Booking(TimeStampedModel):
    """Online session booking with an advance payment."""

    class Status(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"

    booking_code = models.CharField(max_length=48, unique=True, db_index=True)
    patient = models.ForeignKey(
        "patients.Patient", on_delete=models.PROTECT, related_name="bookings"
    )
    service = models.ForeignKey(
        "services.Service", on_delete=models.PROTECT, related_name="bookings"
    )
    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="bookings"
    )
    date = models.DateField(db_index=True)
    time = models.CharField(max_length=16)
    advance_amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.CONFIRMED, db_index=True
    )
    payment = models.ForeignKey(
        "payments.Payment", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="bookings",
    )

    class Meta:
        ordering = ["-date"]
        indexes = [models.Index(fields=["branch", "date"])]

    def __str__(self):
        return f"{self.booking_code} — {self.date}"


def due_date_for_month(month: str) -> date:
    """
    The 5th of the bill's own month — not five days after it was generated.

    Confirmed rule: payment is due by the 5th, and an unpaid bill is overdue
    after that.
    """
    year, month_number = (int(part) for part in month.split("-"))
    return date(year, month_number, settings.MONTHLY_BILL_DUE_DAY)
