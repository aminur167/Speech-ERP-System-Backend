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
        LATE = "late", "Late"
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
