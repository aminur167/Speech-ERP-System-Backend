"""Expense submission and the approval workflow."""

from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.common import audit
from apps.common.models import AuditLog
from apps.common.sequences import next_value
from apps.expenses.models import Expense


class ExpenseError(Exception):
    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


@transaction.atomic
def create_expense(*, actor, branch, data: dict) -> Expense:
    """
    Record an expense.

    Small amounts are auto-approved so a manager buying stationery doesn't
    wait on an admin; anything at or above the threshold needs review. The
    threshold is a setting rather than a constant — a clinic will want to
    change it, and a code deploy for that is poor design over a decade.

    `expense_code` stays org-wide (not per-branch like receipts): expenses
    aren't taken at the counter under time pressure, so offline issuance
    isn't a concern here.
    """
    year = timezone.localdate().year
    value = next_value("expense", year)

    amount = Decimal(data["amount"])
    threshold = Decimal(settings.EXPENSE_AUTO_APPROVE_THRESHOLD)

    auto_approved = amount < threshold

    expense = Expense.objects.create(
        expense_code=f"EXP-{year}-{str(value).zfill(5)}",
        branch=branch,
        submitted_by=actor,
        status=Expense.Status.APPROVED if auto_approved else Expense.Status.PENDING,
        reviewed_at=timezone.now() if auto_approved else None,
        review_note="Auto-approved (below approval threshold)" if auto_approved else "",
        **data,
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=expense,
        branch=branch,
        changes={
            "code": expense.expense_code,
            "amount": str(expense.amount),
            "status": expense.status,
        },
    )
    return expense


@transaction.atomic
def review_expense(*, actor, expense: Expense, approve: bool, review_note: str = "") -> Expense:
    """
    Approve or reject, including reversing an earlier decision.

    A reason is required to reject, and also to *reverse* any prior decision —
    including reversing to approved, the one case a note isn't otherwise
    needed. A silent flip on a money record is exactly what an audit has to be
    able to explain later.

    Reversing changes whether the amount counts toward totals, so a past
    period's reported figure can move. That's correct and intended.
    """
    was_reviewed = expense.status != Expense.Status.PENDING
    new_status = Expense.Status.APPROVED if approve else Expense.Status.REJECTED

    if not approve and not review_note.strip():
        raise ExpenseError(
            "A reason is required when rejecting an expense.", code="note_required"
        )

    if was_reviewed and not review_note.strip():
        raise ExpenseError(
            "A reason is required when changing a decision that was already made.",
            code="note_required",
        )

    if expense.status == new_status:
        raise ExpenseError(f"This expense is already {new_status}.", code="no_change")

    previous = expense.status
    expense.status = new_status
    expense.review_note = review_note
    expense.reviewed_by = actor
    expense.reviewed_at = timezone.now()
    expense.save(update_fields=["status", "review_note", "reviewed_by", "reviewed_at"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.APPROVE if approve else AuditLog.Action.REJECT,
        target=expense,
        branch=expense.branch,
        reason=review_note,
        changes={"status": {"from": previous, "to": new_status}},
    )
    return expense
