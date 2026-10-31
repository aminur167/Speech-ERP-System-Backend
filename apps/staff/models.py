"""
Staff HR: each branch's team roster, daily attendance, and bonuses.

Branch-scoped, like Material — a therapist or receptionist belongs to one
branch's roster; Admin can see across branches but (per the Manager/Admin
split in docs/00) doesn't check people in or out on a branch's behalf.
"""

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from apps.common.models import SoftDeleteModel, TimeStampedModel


class StaffMember(TimeStampedModel, SoftDeleteModel):
    class Designation(models.TextChoices):
        THERAPIST = "therapist", "Therapist"
        # Sits next to Therapist rather than at the end of the list: both
        # work with the children directly, and the dropdown reads better
        # with the care roles together and "Other" still last.
        PLAY_TEACHER = "play_teacher", "Play Teacher"
        RECEPTIONIST = "receptionist", "Receptionist"
        ACCOUNTANT = "accountant", "Accountant"
        SUPPORT_STAFF = "support_staff", "Support Staff"
        CLEANER = "cleaner", "Cleaner"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    staff_code = models.CharField(max_length=32, unique=True, db_index=True)  # e.g. STF-DHK-001
    name = models.CharField(max_length=150)
    designation = models.CharField(max_length=32, choices=Designation.choices)
    phone = models.CharField(max_length=32)
    email = models.EmailField(blank=True)
    joined_at = models.DateField()
    monthly_salary = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))]
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    # A data URL the client produces client-side (resize + compress, capped at
    # 512KB before encoding) rather than a hosted file -- there's no real
    # object storage wired up yet, same tradeoff Material.image_url made.
    # TextField, not URLField: a data URL is neither shaped like a URL nor
    # bounded by URLField's short default max_length.
    photo_url = models.TextField(blank=True)

    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="staff_members"
    )

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["branch", "name"]),
            models.Index(fields=["branch", "status"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.staff_code})"


class StaffAttendance(TimeStampedModel):
    """
    One row per staff member per calendar day.

    Unique on (staff, date) so "check in" is always an upsert onto today's
    row rather than a growing pile of punches — the UI only ever needs one
    in-time and one out-time per person per day.
    """

    class Status(models.TextChoices):
        PRESENT = "present", "Present"
        EARLY_LEAVE = "early_leave", "Early Leave"
        ON_LEAVE = "on_leave", "On Leave"
        ABSENT = "absent", "Absent"

    staff = models.ForeignKey(
        StaffMember, on_delete=models.CASCADE, related_name="attendance_records"
    )
    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="staff_attendance_records"
    )
    date = models.DateField()
    check_in_at = models.DateTimeField(null=True, blank=True)
    check_out_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices)

    class Meta:
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(fields=["staff", "date"], name="uniq_staff_attendance_per_day")
        ]
        indexes = [
            models.Index(fields=["branch", "date"]),
            models.Index(fields=["staff", "-date"]),
        ]

    def __str__(self):
        return f"{self.staff_id} {self.date} {self.status}"


class StaffBonus(TimeStampedModel):
    staff = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name="bonuses")
    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="staff_bonuses"
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    reason = models.CharField(max_length=255)
    awarded_by = models.ForeignKey(
        "accounts.User",
        null=True,
        on_delete=models.SET_NULL,
        related_name="staff_bonuses_awarded",
    )

    class Meta:
        # `-id` breaks ties between bonuses created in the same request, where
        # `created_at`'s auto_now_add timestamps can be identical to the tick.
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["staff", "-created_at"])]

    def __str__(self):
        return f"{self.amount} to {self.staff_id}"


class SalaryPayment(TimeStampedModel):
    """
    A Manager's request to pay one staff member's salary for one month,
    gated on Admin approval before the money moves.

    Deliberately **not** built on Expense's own pending/approved status: an
    Expense records money *already spent* (docs: a pending expense is cash
    that has left the clinic, awaiting verification) — the opposite of what
    this is. This is a pre-spend authorization. Only once a Manager actually
    disburses an *approved* request does an Expense get created, at which
    point the money really has left and Expense's own "already spent"
    invariant holds true.
    """

    class Status(models.TextChoices):
        PENDING_APPROVAL = "pending_approval", "Pending Approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        PAID = "paid", "Paid"

    staff = models.ForeignKey(
        StaffMember, on_delete=models.PROTECT, related_name="salary_payments"
    )
    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="staff_salary_payments"
    )
    month = models.CharField(max_length=7, db_index=True)  # "YYYY-MM"
    # Snapshot at request time (base salary + that month's bonuses so far) —
    # never recomputed later, so approving this request always pays exactly
    # what the Manager asked for, even if a bonus is added afterwards.
    #
    # `amount` is what will actually be handed over: gross less whatever
    # outstanding advances this month takes back. The two figures beside it
    # are kept so a payslip can show the subtraction instead of a number
    # nobody can reconcile — the same gross/deduction/net shape the bills use
    # for discounts. `gross_amount` is null on requests made before advances
    # existed; there was nothing to deduct, so `amount` was already gross.
    # Zero is allowed, unlike a bonus or an advance: a month whose advances
    # take all of it is a real payroll event that moved no money, the same
    # shape a fully discounted bill has (enrollments: settled, no payment, no
    # receipt). Refusing it instead would deadlock -- the salary could never
    # be requested, so the advance could never be recovered.
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.00"))]
    )
    gross_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    advance_deduction = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING_APPROVAL, db_index=True
    )

    requested_by = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL,
        related_name="salary_payments_requested",
    )

    review_note = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="salary_payments_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    payment_method = models.CharField(max_length=20, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    # The Expense this disbursement created — the paper trail a bookkeeper
    # follows from "why did payroll go up this month" back to who approved it.
    expense = models.ForeignKey(
        "expenses.Expense", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="salary_payment",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["staff", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.staff_id} {self.month} {self.status}"


class StaffAdvance(TimeStampedModel):
    """
    Salary paid early, recovered from a later month.

    Runs the same three steps as SalaryPayment — Manager asks, Admin
    approves, Manager disburses — for the same reason: no money leaves a
    branch on one person's say-so, and the Expense is written at the moment
    it actually leaves, not when it is promised.

    What makes this its own model rather than a `kind` on SalaryPayment is
    `recovered_amount`. A salary request is settled the moment it is paid; an
    advance stays open afterwards, shrinking across however many months it
    takes to recover. Folding a running balance into a table whose rows are
    finished when they reach PAID would have meant every read of a salary row
    carrying a field that is meaningless for it.
    """

    class Status(models.TextChoices):
        PENDING_APPROVAL = "pending_approval", "Pending Approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        PAID = "paid", "Paid"

    staff = models.ForeignKey(
        StaffMember, on_delete=models.PROTECT, related_name="advances"
    )
    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="staff_advances"
    )
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    # Required, unlike a salary request's amount -- that one the server
    # computes from the roster, this one somebody chose, and an unexplained
    # early payment is exactly what an audit asks about.
    reason = models.CharField(max_length=255)

    # How much of `amount` later salary has already taken back. Advances are
    # recovered oldest-first, so this is the only field that moves after the
    # advance is paid; `amount` is what was handed over and never changes.
    recovered_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING_APPROVAL,
        db_index=True,
    )

    requested_by = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL,
        related_name="staff_advances_requested",
    )
    review_note = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="staff_advances_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    payment_method = models.CharField(max_length=20, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    expense = models.ForeignKey(
        "expenses.Expense", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="staff_advance",
    )

    class Meta:
        # Oldest first is the recovery order, so the default ordering is the
        # order the deduction walks -- nothing has to re-sort to be correct.
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["staff", "status"]),
        ]

    def __str__(self):
        return f"{self.staff_id} advance {self.amount} ({self.status})"

    @property
    def outstanding(self) -> Decimal:
        """What is still to come out of future salary."""
        return self.amount - self.recovered_amount
