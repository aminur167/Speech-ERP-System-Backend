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
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
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
