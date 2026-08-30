"""
Reporting and analytics.

⚠️ The subtlety this module exists to get right:

A naive `status == "paid"` filter is wrong for historical periods. A payment
collected in August and refunded in September has `status = "refunded"` today,
so filtering on current status removes it from **August** too — retroactively
rewriting a month that was already reconciled and reported.

The confirmed rule (docs/10): a payment counts as revenue in **its own**
month; the refund is a separate negative event dated to when it was
**approved**. Closed periods stay closed.

So revenue queries here filter on *dated events*, not on the payment's current
status alone:

    revenue(period)  = payments created in period, excluding those VOIDED
    refunds(period)  = refunds APPROVED in period
    net(period)      = revenue − refunds − expenses

Void is different: a voided transaction never happened, so it's removed from
its original day entirely. Voids are same-day-only precisely so this can never
disturb a closed period.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.expenses.models import Expense
from apps.patients.models import Patient
from apps.payments.models import Payment, PaymentStatus, RefundRequest

# Approved and pending are real spending; rejected is not the clinic's cost.
# Must stay identical to the rule in the expenses module, or the two screens
# stop reconciling.
COUNTED_EXPENSE_STATUSES = [Expense.Status.APPROVED, Expense.Status.PENDING]


def _revenue_queryset(branch_id=None):
    """
    Payments that count as revenue for the period they were created in.

    Excludes only VOID — a voided payment never happened. Refunded and
    partially-refunded payments stay in their original period; the refund is
    accounted separately, dated to its approval.
    """
    queryset = Payment.objects.exclude(status=PaymentStatus.VOID)
    if branch_id:
        queryset = queryset.filter(branch_id=branch_id)
    return queryset


def _refund_queryset(branch_id=None):
    """Approved refunds, dated by when they were approved."""
    queryset = RefundRequest.objects.filter(status=RefundRequest.Status.APPROVED)
    if branch_id:
        queryset = queryset.filter(branch_id=branch_id)
    return queryset


def _sum(queryset, field="amount") -> Decimal:
    return queryset.aggregate(s=Sum(field))["s"] or Decimal("0.00")


def transactions_summary(*, branch_id=None, as_of: date | None = None) -> dict:
    """Headline collection figures for a day and its month."""
    reference = as_of or timezone.localdate()
    revenue = _revenue_queryset(branch_id)

    day_total = _sum(revenue.filter(created_at__date=reference))
    month_total = _sum(
        revenue.filter(
            created_at__year=reference.year, created_at__month=reference.month
        )
    )

    refunds = _refund_queryset(branch_id)
    month_refunds = _sum(
        refunds.filter(
            reviewed_at__year=reference.year, reviewed_at__month=reference.month
        )
    )

    by_method = [
        {"method": row["method"], "amount": row["amount"]}
        for row in revenue.values("method").annotate(amount=Sum("amount")).order_by("-amount")
    ]

    return {
        "totalCollected": _sum(revenue),
        "transactionCount": revenue.count(),
        "todayCollected": day_total,
        "monthCollected": month_total,
        "monthRefunded": month_refunds,
        "byMethod": by_method,
    }


def revenue_trend(*, branch_id=None, days: int = 7) -> list[dict]:
    """
    Daily collection for the last `days` days, oldest first.

    One grouped query rather than a query per day — the difference is
    invisible over a week and matters for longer ranges.
    """
    today = timezone.localdate()
    start = today - timedelta(days=days - 1)

    rows = (
        _revenue_queryset(branch_id)
        .filter(created_at__date__gte=start)
        .values("created_at__date")
        .annotate(amount=Sum("amount"))
    )
    by_date = {row["created_at__date"]: row["amount"] for row in rows}

    return [
        {
            "date": (start + timedelta(days=offset)).isoformat(),
            "label": (start + timedelta(days=offset)).strftime("%b %d"),
            "amount": by_date.get(start + timedelta(days=offset), Decimal("0.00")),
        }
        for offset in range(days)
    ]


def revenue_by_method(*, branch_id=None, as_of: date | None = None) -> list[dict]:
    reference = as_of or timezone.localdate()
    rows = (
        _revenue_queryset(branch_id)
        .filter(created_at__year=reference.year, created_at__month=reference.month)
        .values("method")
        .annotate(amount=Sum("amount"))
        .order_by("-amount")
    )
    return [{"method": r["method"], "amount": r["amount"]} for r in rows]


def revenue_by_category(*, branch_id=None, as_of: date | None = None) -> list[dict]:
    reference = as_of or timezone.localdate()
    rows = (
        _revenue_queryset(branch_id)
        .filter(created_at__year=reference.year, created_at__month=reference.month)
        .exclude(category="")
        .exclude(category="material_sale")
        .values("category")
        .annotate(amount=Sum("amount"))
        .order_by("-amount")
    )
    return [{"category": r["category"], "amount": r["amount"]} for r in rows]


def dashboard_metrics(*, branch_id=None, as_of: date | None = None) -> dict:
    """Per-day figures that don't fit the other summaries."""
    reference = as_of or timezone.localdate()
    day = _revenue_queryset(branch_id).filter(created_at__date=reference)

    return {
        "todayPatientsSeen": day.values("patient_id").distinct().count(),
        "todayDueCollected": _sum(day.filter(category__in=["monthly", "installment"])),
    }


def collection_for_date(*, branch_id=None, target: date) -> Decimal:
    return _sum(_revenue_queryset(branch_id).filter(created_at__date=target))


def refunds_and_voids(*, branch_id=None) -> list[dict]:
    """Shown in reports even though they're excluded from revenue."""
    queryset = (
        Payment.objects.filter(
            status__in=[PaymentStatus.REFUNDED, PaymentStatus.VOID, PaymentStatus.PARTIAL]
        )
        .select_related("patient", "branch")
        .order_by("-created_at")
    )
    if branch_id:
        queryset = queryset.filter(branch_id=branch_id)

    return [
        {
            "id": str(p.id),
            "receiptNumber": p.receipt_number,
            "patientName": p.patient.name,
            "patientCode": p.patient.patient_code,
            "method": p.method,
            "status": p.status,
            "amount": p.amount,
            "createdAt": p.created_at,
        }
        for p in queryset[:200]
    ]


def net_revenue(*, branch_id=None, as_of: date | None = None) -> dict:
    """
    Gross collected − refunds − expenses, for the reference month.

    Expenses use the same approved+pending rule as the expenses module. If the
    two ever diverge, the Reports page and the Expenses page stop agreeing.
    """
    reference = as_of or timezone.localdate()

    gross = _sum(
        _revenue_queryset(branch_id).filter(
            created_at__year=reference.year, created_at__month=reference.month
        )
    )
    refunded = _sum(
        _refund_queryset(branch_id).filter(
            reviewed_at__year=reference.year, reviewed_at__month=reference.month
        )
    )

    expenses = Expense.objects.filter(status__in=COUNTED_EXPENSE_STATUSES)
    if branch_id:
        expenses = expenses.filter(branch_id=branch_id)
    expense_total = _sum(
        expenses.filter(created_at__year=reference.year, created_at__month=reference.month)
    )

    return {
        "grossCollected": gross,
        "refunded": refunded,
        "expenses": expense_total,
        "netRevenue": gross - refunded - expense_total,
    }


def patient_directory_summary(*, branch_id=None, as_of: date | None = None) -> dict:
    """
    Care-status counts for the patient dashboard.

    `intake` is scoped to the month of `as_of`, not the current month — a past
    date must return that month's intake, which is what the dashboard date
    picker needs.
    """
    reference = as_of or timezone.localdate()

    patients = Patient.objects.all()
    if branch_id:
        patients = patients.filter(branch_id=branch_id)

    active_care = patients.filter(
        monthly_enrollments__status="active"
    ).distinct().count()

    in_progress = (
        patients.filter(installment_plans__status="active")
        .exclude(monthly_enrollments__status="active")
        .distinct()
        .count()
    )

    total = patients.count()

    return {
        "total": total,
        "activeCare": active_care,
        "inProgress": in_progress,
        "actionNeeded": total - active_care - in_progress,
        "intake": patients.filter(
            created_at__year=reference.year, created_at__month=reference.month
        ).count(),
    }
