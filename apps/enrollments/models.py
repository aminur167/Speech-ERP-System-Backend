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
from django.utils import timezone

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
    # Paid before the month arrived. **Always fully prepaid** — the advance
    # flow settles whole months only, so `advance` implies
    # `amount_paid == amount` and every read site can rely on that. The
    # invariant is what keeps this status from spawning a dozen partial cases.
    ADVANCE = "advance", "Paid in advance"


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

    def _advance_arrived(self, *, on: date | None = None) -> bool:
        """Only a month-shaped payable can be paid ahead; installments can't."""
        return True

    def settled_status(self) -> str:
        """What this becomes once it is paid. See MonthlyBill for the twist."""
        return BillStatus.PAID

    def unsettled_status(self) -> str:
        """What it returns to if a payment is reversed."""
        return BillStatus.DUE

    def effective_status(self, *, on: date | None = None) -> str:
        """Status with overdue applied, for display and reporting."""
        if self.status in {BillStatus.PAID, BillStatus.WRITTEN_OFF}:
            return self.status
        if self.status == BillStatus.ADVANCE:
            # Derived as well as stored, and checked here *before*
            # `is_settled` below: a prepaid bill is settled, so the next
            # branch would report it as plain "paid" and the advance would be
            # invisible everywhere. Deriving it also means the month turning
            # over reads correctly on the 1st with no job having run.
            return BillStatus.PAID if self._advance_arrived(on=on) else BillStatus.ADVANCE
        if self.is_settled:
            return BillStatus.PAID
        if self.is_overdue(on=on):
            return BillStatus.OVERDUE
        return self.status


class MonthlyEnrollment(TimeStampedModel):
    class TerminationKind(models.TextChoices):
        """
        Why a monthly service stopped — which decides what can happen next.

        A manager stopping a service forgives whatever is owed (see
        `services.terminate`), so there is nothing left to collect and nothing
        to resume *with*. A service stopped automatically for an unpaid due
        keeps the debt intact, because the patient may come back and settle
        it. Only the second kind appears on the Terminated Services screen.
        """

        MANUAL = "manual", "Stopped by a manager"
        UNPAID_DUE = "unpaid_due", "Stopped automatically — the month's due went unpaid"

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
    terminated_kind = models.CharField(
        max_length=16, choices=TerminationKind.choices, blank=True, db_index=True
    )
    # The cycle whose unpaid due ended it, e.g. "2026-10". Blank when a
    # manager stopped it: no single month is to blame for that.
    terminated_month = models.CharField(max_length=7, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["patient", "status"]),
            models.Index(fields=["service", "status"]),
            # The Terminated Services screen's own listing.
            models.Index(fields=["branch", "terminated_kind", "-terminated_at"]),
        ]

    def __str__(self):
        return f"{self.patient_id} — {self.service_id} (monthly)"

    @property
    def is_active(self) -> bool:
        return self.status == EnrollmentStatus.ACTIVE

    def outstanding_total(self) -> Decimal:
        return sum((bill.outstanding for bill in self.bills.all()), Decimal("0.00"))

    def unpaid_bills(self):
        """
        Every bill still owed, oldest first.

        `amount_paid < amount` rather than status alone: a part-paid bill
        still has a balance, and one left DUE with nothing outstanding would
        otherwise look collectable.
        """
        return (
            self.bills.exclude(
                status__in=[BillStatus.PAID, BillStatus.WRITTEN_OFF, BillStatus.ADVANCE]
            )
            .filter(amount_paid__lt=models.F("amount"))
            .order_by("month")
        )

    def oldest_unpaid_bill(self):
        """
        The only bill that may be paid next.

        Oldest-first: a patient can't settle September while August is
        outstanding, or old debt ages forever while they keep paying the
        current month.
        """
        return self.unpaid_bills().first()


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

    def _advance_arrived(self, *, on: date | None = None) -> bool:
        from apps.enrollments.services import month_key

        return self.month <= month_key(on or timezone.localdate())

    def settled_status(self) -> str:
        """
        A month paid before it arrives is an advance, not a payment for now.

        One helper rather than a literal at each write site: the moment two
        places decide independently what a settled bill becomes, they drift —
        which is exactly how a refunded December bill ended up marked `due`
        in October.
        """
        return (
            BillStatus.PAID if self._advance_arrived() else BillStatus.ADVANCE
        )

    def unsettled_status(self) -> str:
        return BillStatus.DUE if self._advance_arrived() else BillStatus.UPCOMING

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
