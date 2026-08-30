"""
Daily cash reconciliation.

Append-only by design. A submitted closing is never edited in place and never
deleted; a correction is an Admin-recorded amendment that preserves the
original figure. A manager who could quietly rewrite the number whenever cash
came up short would defeat the entire purpose of the control.
"""

from decimal import Decimal

from django.db import models

from apps.common.models import TimeStampedModel


class DailyClosing(TimeStampedModel):
    class Status(models.TextChoices):
        MATCHED = "matched", "Matched"
        OVER = "over", "Over"
        SHORT = "short", "Short"

    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="closings"
    )
    date = models.DateField(db_index=True)

    # Derived from Payments — never client-supplied, and not amendable, since
    # changing it would mean rewriting history rather than correcting a count.
    system_total = models.DecimalField(max_digits=14, decimal_places=2)
    # What the manager actually counted.
    actual_total = models.DecimalField(max_digits=14, decimal_places=2)
    difference = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=16, choices=Status.choices, db_index=True)

    submitted_by = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL, related_name="closings"
    )
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date"]
        constraints = [
            # One closing per branch per day. Also what makes the void cutoff
            # in payments meaningful — "has this day been signed off" has a
            # single unambiguous answer.
            models.UniqueConstraint(fields=["branch", "date"], name="uniq_closing_per_branch_day")
        ]
        indexes = [models.Index(fields=["branch", "-date"])]

    def __str__(self):
        return f"{self.branch_id} {self.date} — {self.status}"

    @property
    def is_amended(self) -> bool:
        return self.amendments.exists()

    @staticmethod
    def compute(system_total: Decimal, actual_total: Decimal) -> tuple[Decimal, str]:
        """
        Difference and status, computed server-side.

        Never trust a client-supplied difference or status: they're the
        reconciliation result, and accepting them would let the screen claim a
        day matched when it didn't.
        """
        difference = Decimal(actual_total) - Decimal(system_total)
        if difference == 0:
            status = DailyClosing.Status.MATCHED
        elif difference > 0:
            status = DailyClosing.Status.OVER
        else:
            status = DailyClosing.Status.SHORT
        return difference, status


class DailyClosingAmendment(models.Model):
    """
    A correction to a submitted closing.

    Both figures survive — the original in `previous_actual_total`, the
    correction in `corrected_actual_total`. Standard accounting practice, for
    the same reason ledgers use reversing entries rather than erasers.
    """

    closing = models.ForeignKey(
        DailyClosing, on_delete=models.CASCADE, related_name="amendments"
    )
    previous_actual_total = models.DecimalField(max_digits=14, decimal_places=2)
    corrected_actual_total = models.DecimalField(max_digits=14, decimal_places=2)
    # Required — a correction with no explanation isn't a correction.
    reason = models.TextField()

    amended_by = models.ForeignKey(
        "accounts.User", null=True, on_delete=models.SET_NULL, related_name="closing_amendments"
    )
    amended_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["amended_at"]

    def __str__(self):
        return f"{self.previous_actual_total} → {self.corrected_actual_total}"
