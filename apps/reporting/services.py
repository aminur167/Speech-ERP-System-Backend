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
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.dailyclosing.models import DailyClosing
from apps.duepayments.services import due_summary
from apps.expenses.models import Expense
from apps.patients.models import Patient
from apps.payments.models import Payment, PaymentStatus, RefundRequest

# One row's `type` in `branch_activity` — also the value the frontend keys its
# badge colour and icon off of, so renaming one of these is a two-repo change.
ACTIVITY_TYPE_INVOICE = "invoice"
ACTIVITY_TYPE_EXPENSE = "expense"
ACTIVITY_TYPE_REFUND = "refund"

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
    total_refunds = _sum(refunds)

    # Every approved/pending expense, salaries included: a salary payment
    # becomes an Expense (category SALARIES) the moment it's disbursed
    # (apps/staff/services.py::disburse_salary_payment), so this one query
    # already covers both without a separate salary total to keep in sync.
    expenses = Expense.objects.filter(status__in=COUNTED_EXPENSE_STATUSES)
    if branch_id:
        expenses = expenses.filter(branch_id=branch_id)
    total_expenses = _sum(expenses)

    by_method = [
        {"method": row["method"], "amount": row["amount"]}
        for row in revenue.values("method").annotate(amount=Sum("amount")).order_by("-amount")
    ]

    return {
        "totalCollected": _sum(revenue),
        "totalRefunded": total_refunds,
        "totalExpenses": total_expenses,
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


def branch_summary(*, branch_id=None, date_from: date, date_to: date) -> dict:
    """
    Everything one branch did between two dates.

    The date-range equivalent of the month-scoped figures above, for the
    branch Summary page. Same accounting rules throughout (see this module's
    header): revenue is dated by when the payment was taken, a refund by when
    it was approved, a void never happened at all, and an expense counts while
    approved or pending.

    `outstandingDue` is deliberately not range-scoped -- money still owed is a
    position as of now, not something that happened during a window, and
    showing it as though it belonged to the range would misread it.
    """
    # Inclusive of both endpoints: a user picking 1st-31st means the whole
    # month, so `created_at__date` (not a datetime __range that would cut the
    # last day off at midnight).
    in_range = {"created_at__date__gte": date_from, "created_at__date__lte": date_to}

    revenue = _revenue_queryset(branch_id).filter(**in_range)
    gross = _sum(revenue)

    refunds = _refund_queryset(branch_id).filter(
        reviewed_at__date__gte=date_from, reviewed_at__date__lte=date_to
    )
    refunded = _sum(refunds)

    expenses = Expense.objects.filter(status__in=COUNTED_EXPENSE_STATUSES)
    if branch_id:
        expenses = expenses.filter(branch_id=branch_id)
    expenses = expenses.filter(**in_range)
    expense_total = _sum(expenses)

    patients = Patient.objects.all()
    if branch_id:
        patients = patients.filter(branch_id=branch_id)

    by_method = [
        {"method": row["method"], "amount": row["amount"]}
        for row in revenue.values("method").annotate(amount=Sum("amount")).order_by("-amount")
    ]
    by_category = [
        {"category": row["category"], "amount": row["amount"]}
        for row in revenue.exclude(category="")
        .exclude(category="material_sale")
        .values("category")
        .annotate(amount=Sum("amount"))
        .order_by("-amount")
    ]

    closings = DailyClosing.objects.filter(date__gte=date_from, date__lte=date_to)
    if branch_id:
        closings = closings.filter(branch_id=branch_id)

    return {
        "dateFrom": date_from,
        "dateTo": date_to,
        "grossCollected": gross,
        "refunded": refunded,
        "expenses": expense_total,
        "netRevenue": gross - refunded - expense_total,
        "paymentCount": revenue.count(),
        "patientsSeen": revenue.values("patient_id").distinct().count(),
        "newPatients": patients.filter(**in_range).count(),
        "totalPatients": patients.count(),
        "expenseCount": expenses.count(),
        "refundCount": refunds.count(),
        # Reuses the due-payments module rather than re-deriving it: that one
        # already handles partially-paid bills (outstanding is amount − paid,
        # not amount), and two implementations of "what is owed" would drift.
        "outstandingDue": due_summary(branch_id=branch_id)["totalDue"],
        "byMethod": by_method,
        "byCategory": by_category,
        "closingsSubmitted": closings.count(),
        "closingsMismatched": closings.exclude(status=DailyClosing.Status.MATCHED).count(),
    }


def daily_ledger(*, branch_id=None, date_from: date, date_to: date) -> list[dict]:
    """
    The branch's day-by-day ledger: one row per day that anything happened on.

    Same accounting rules as `branch_summary` above, just resolved per day
    instead of rolled into one figure — so the two always reconcile: summing a
    column here equals the matching total there.

    Days with no activity at all are omitted rather than padded with zeros. A
    year-long range would otherwise be mostly empty rows, and a reader
    scanning for the day something went wrong has to skip past them.

    Viewed across every branch (Admin without `?branch=`), the closing columns
    aggregate: `closingDifference` sums each branch's variance for that day and
    `closingStatus` reads "mismatched" if any one of them did not balance —
    the safe direction to round, since it can only draw attention to a day
    that deserves it.
    """
    rows: dict[date, dict] = {}

    def row_for(day: date) -> dict:
        return rows.setdefault(
            day,
            {
                "date": day,
                "transactionCount": 0,
                "patientsSeen": 0,
                "collected": Decimal("0.00"),
                "refundCount": 0,
                "refunded": Decimal("0.00"),
                "expenseCount": 0,
                "expenses": Decimal("0.00"),
                "closingsSubmitted": 0,
                "closingStatus": "",
                "closingDifference": Decimal("0.00"),
            },
        )

    revenue = (
        _revenue_queryset(branch_id)
        .filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(
            amount=Sum("amount"),
            count=Count("id"),
            patients=Count("patient_id", distinct=True),
        )
    )
    for entry in revenue:
        row = row_for(entry["day"])
        row["collected"] = entry["amount"] or Decimal("0.00")
        row["transactionCount"] = entry["count"]
        row["patientsSeen"] = entry["patients"]

    # Dated by approval, not by when the refund was asked for — the module
    # header's rule, and what keeps a closed month closed.
    refunds = (
        _refund_queryset(branch_id)
        .filter(reviewed_at__date__gte=date_from, reviewed_at__date__lte=date_to)
        .annotate(day=TruncDate("reviewed_at"))
        .values("day")
        .annotate(amount=Sum("amount"), count=Count("id"))
    )
    for entry in refunds:
        row = row_for(entry["day"])
        row["refunded"] = entry["amount"] or Decimal("0.00")
        row["refundCount"] = entry["count"]

    expenses = Expense.objects.filter(
        status__in=COUNTED_EXPENSE_STATUSES,
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
    )
    if branch_id:
        expenses = expenses.filter(branch_id=branch_id)
    for entry in (
        expenses.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(amount=Sum("amount"), count=Count("id"))
    ):
        row = row_for(entry["day"])
        row["expenses"] = entry["amount"] or Decimal("0.00")
        row["expenseCount"] = entry["count"]

    closings = DailyClosing.objects.filter(date__gte=date_from, date__lte=date_to)
    if branch_id:
        closings = closings.filter(branch_id=branch_id)
    for closing in closings:
        row = row_for(closing.date)
        row["closingsSubmitted"] += 1
        row["closingDifference"] += closing.difference
        if closing.status != DailyClosing.Status.MATCHED:
            row["closingStatus"] = "mismatched"
        elif not row["closingStatus"]:
            row["closingStatus"] = "matched"

    for row in rows.values():
        row["netRevenue"] = row["collected"] - row["refunded"] - row["expenses"]

    # Newest first: a ledger is read from what just happened backwards.
    return [rows[day] for day in sorted(rows, reverse=True)]


def branch_activity(*, branch_id=None, date_from: date, date_to: date) -> list[dict]:
    """
    Every invoice, expense and refund in one reverse-chronological feed —
    what a manager scans instead of flipping between the Invoices, Expenses
    and Refunds tabs to see "everything that happened" in a range.

    Same accounting rules as the rest of this module: an invoice is dated by
    when it was collected, an expense by when it was logged, and a refund by
    when it was **approved** — a still-pending refund request has no
    `reviewed_at` yet, so it doesn't appear here until it's decided, matching
    every other period-attribution rule in this file.

    Fetches the whole range and sorts in Python rather than in the database:
    three differently-shaped querysets can't be UNIONed by date across
    Payment/Expense/RefundRequest without raw SQL, and the ranges this feeds
    (a day, a month) are small enough that this is the plain-Django code
    without a performance cost.
    """
    in_range = {"created_at__date__gte": date_from, "created_at__date__lte": date_to}

    payments = (
        _revenue_queryset(branch_id).filter(**in_range).select_related("patient", "collected_by")
    )

    expenses = Expense.objects.filter(**in_range).select_related("submitted_by")
    if branch_id:
        expenses = expenses.filter(branch_id=branch_id)

    refunds = _refund_queryset(branch_id).filter(
        reviewed_at__date__gte=date_from, reviewed_at__date__lte=date_to
    ).select_related("payment__patient", "reviewed_by")

    rows = []
    for payment in payments:
        rows.append(
            {
                "id": f"invoice-{payment.id}",
                "type": ACTIVITY_TYPE_INVOICE,
                "occurredAt": payment.created_at,
                "reference": payment.receipt_number,
                "description": payment.description or payment.get_category_display(),
                "person": payment.patient.name,
                "performedBy": payment.collected_by.name if payment.collected_by else "",
                "amount": payment.amount,
                "direction": "in",
                "status": payment.status,
            }
        )
    for expense in expenses:
        rows.append(
            {
                "id": f"expense-{expense.id}",
                "type": ACTIVITY_TYPE_EXPENSE,
                "occurredAt": expense.created_at,
                "reference": expense.expense_code,
                "description": expense.description,
                "person": expense.paid_to,
                "performedBy": expense.submitted_by.name if expense.submitted_by else "",
                "amount": expense.amount,
                "direction": "out",
                "status": expense.status,
            }
        )
    for refund in refunds:
        rows.append(
            {
                "id": f"refund-{refund.id}",
                "type": ACTIVITY_TYPE_REFUND,
                "occurredAt": refund.reviewed_at,
                "reference": refund.payment.receipt_number,
                "description": refund.reason,
                "person": refund.payment.patient.name,
                "performedBy": refund.reviewed_by.name if refund.reviewed_by else "",
                "amount": refund.amount,
                "direction": "out",
                "status": refund.status,
            }
        )

    rows.sort(key=lambda row: row["occurredAt"], reverse=True)
    return rows
