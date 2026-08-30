"""Daily closing: the day's collection summary, submission, and amendment."""

from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from apps.common import audit
from apps.common.models import AuditLog
from apps.dailyclosing.models import DailyClosing, DailyClosingAmendment
from apps.payments.models import Payment, PaymentStatus


class ClosingError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


def day_summary(*, branch, day: date | None = None) -> dict:
    """
    What the manager reconciles against, plus what moved money against them.

    `total` counts only `paid`, matching the revenue rule in reporting — the
    closing screen and the reports must never disagree.

    But excluding refunds silently isn't enough: if ৳3,000 was refunded today,
    the drawer is short by that much and the manager needs to know why. So the
    day's refunds and voids come back itemised alongside. Reconcile against one
    clean number; show every event that moved money.

    Deliberately *not* auto-adjusting the expected cash for refunds: a cash
    refund reduces the drawer, a bKash one doesn't, and encoding a guess about
    which would be worse than showing the facts.
    """
    target = day or timezone.localdate()

    payments = Payment.objects.filter(branch=branch, created_at__date=target)

    paid = payments.filter(status=PaymentStatus.PAID)
    totals = paid.aggregate(total=Sum("amount"), count=Count("id"))

    by_method = [
        {"method": row["method"], "amount": row["amount"]}
        for row in paid.values("method").annotate(amount=Sum("amount")).order_by("-amount")
    ]

    adjustments = payments.filter(
        status__in=[PaymentStatus.REFUNDED, PaymentStatus.VOID, PaymentStatus.PARTIAL]
    ).select_related("patient")

    refunded_total = adjustments.filter(
        status__in=[PaymentStatus.REFUNDED, PaymentStatus.PARTIAL]
    ).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    void_total = adjustments.filter(status=PaymentStatus.VOID).aggregate(
        s=Sum("amount")
    )["s"] or Decimal("0.00")

    return {
        "total": totals["total"] or Decimal("0.00"),
        "transactionCount": totals["count"] or 0,
        "byMethod": by_method,
        "adjustments": {
            "refundedTotal": refunded_total,
            "voidTotal": void_total,
            "items": [
                {
                    "receiptNumber": item.receipt_number,
                    "patientName": item.patient.name,
                    "method": item.method,
                    "amount": item.amount,
                    "status": item.status,
                    "createdAt": item.created_at,
                }
                for item in adjustments
            ],
        },
    }


@transaction.atomic
def submit_closing(*, actor, branch, actual_total: Decimal, day: date | None = None):
    """
    Sign off the day's cash.

    `system_total`, `difference` and `status` are all computed here from the
    payment records — accepting them from the client would let the screen
    claim a day balanced when it didn't.
    """
    target = day or timezone.localdate()

    if DailyClosing.objects.filter(branch=branch, date=target).exists():
        raise ClosingError(
            "Today's closing has already been submitted.", code="already_submitted"
        )

    summary = day_summary(branch=branch, day=target)
    system_total = summary["total"]
    difference, status = DailyClosing.compute(system_total, actual_total)

    closing = DailyClosing.objects.create(
        branch=branch,
        date=target,
        system_total=system_total,
        actual_total=actual_total,
        difference=difference,
        status=status,
        submitted_by=actor,
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=closing,
        branch=branch,
        changes={
            "date": str(target),
            "system_total": str(system_total),
            "actual_total": str(actual_total),
            "status": status,
        },
    )
    return closing


@transaction.atomic
def amend_closing(*, actor, closing: DailyClosing, corrected_actual_total: Decimal, reason: str):
    """
    Correct a submitted closing — Admin only, and never in place.

    The amendment row keeps the original figure, so the history shows both what
    was signed off and what it was corrected to. `system_total` is not
    amendable: it's derived from payments, and changing it would be rewriting
    history rather than fixing a miscount.
    """
    if not reason or not reason.strip():
        raise ClosingError(
            "A reason is required when correcting a closing.", code="reason_required"
        )

    previous = closing.actual_total
    difference, status = DailyClosing.compute(closing.system_total, corrected_actual_total)

    DailyClosingAmendment.objects.create(
        closing=closing,
        previous_actual_total=previous,
        corrected_actual_total=corrected_actual_total,
        reason=reason,
        amended_by=actor,
    )

    closing.actual_total = corrected_actual_total
    closing.difference = difference
    closing.status = status
    closing.save(update_fields=["actual_total", "difference", "status"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.AMEND,
        target=closing,
        branch=closing.branch,
        reason=reason,
        changes={"actual_total": {"from": str(previous), "to": str(corrected_actual_total)}},
    )
    return closing
