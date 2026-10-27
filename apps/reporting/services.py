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

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import (
    Count,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    Sum,
)
from django.db.models.functions import TruncDate
from django.utils import timezone

from apps.common.models import AuditLog
from apps.duepayments.services import due_summary
from apps.enrollments.models import InstallmentPlan, MonthlyEnrollment
from apps.expenses.models import Expense
from apps.patients.models import Patient
from apps.payments.models import Payment, PaymentStatus, RefundRequest
from apps.staff.models import SalaryPayment

# One row's `type` in `branch_activity` — also the value the frontend keys its
# badge colour and icon off of, so renaming one of these is a two-repo change.
ACTIVITY_TYPE_INVOICE = "invoice"
ACTIVITY_TYPE_EXPENSE = "expense"
ACTIVITY_TYPE_REFUND = "refund"
ACTIVITY_TYPE_PATIENT = "patient"
ACTIVITY_TYPE_ENROLLMENT = "enrollment"
ACTIVITY_TYPE_SALARY = "salary"

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
    """
    Headline collection figures for a day and its month.

    Three queries however many figures there are: one grouped read of the
    payments (the all-time total, the count, the day's and the month's
    figures, and the by-method split are all sums over the same rows), one of
    the refunds and one of the expenses. Each figure is a conditional sum
    (`Sum(..., filter=...)`) instead of a query of its own.
    """
    reference = as_of or timezone.localdate()
    in_month = Q(created_at__year=reference.year, created_at__month=reference.month)

    per_method = list(
        _revenue_queryset(branch_id)
        .values("method")
        .annotate(
            total=Sum("amount"),
            count=Count("id"),
            day=Sum("amount", filter=Q(created_at__date=reference)),
            month=Sum("amount", filter=in_month),
        )
        .order_by("-total")
    )

    refunds = _refund_queryset(branch_id).aggregate(
        total=Sum("amount"),
        month=Sum(
            "amount",
            filter=Q(reviewed_at__year=reference.year, reviewed_at__month=reference.month),
        ),
    )

    # Every approved/pending expense, salaries included: a salary payment
    # becomes an Expense (category SALARIES) the moment it's disbursed
    # (apps/staff/services.py::disburse_salary_payment), so this one query
    # already covers both without a separate salary total to keep in sync.
    expenses = Expense.objects.filter(status__in=COUNTED_EXPENSE_STATUSES)
    if branch_id:
        expenses = expenses.filter(branch_id=branch_id)
    total_expenses = _sum(expenses)

    return {
        "totalCollected": sum((row["total"] for row in per_method), Decimal("0.00")),
        "totalRefunded": refunds["total"] or Decimal("0.00"),
        "totalExpenses": total_expenses,
        "transactionCount": sum(row["count"] for row in per_method),
        "todayCollected": sum((row["day"] or Decimal("0.00") for row in per_method), Decimal("0.00")),
        "monthCollected": sum((row["month"] or Decimal("0.00") for row in per_method), Decimal("0.00")),
        "monthRefunded": refunds["month"] or Decimal("0.00"),
        "byMethod": [{"method": row["method"], "amount": row["total"]} for row in per_method],
    }


def branch_headline_figures(branch_ids) -> dict[int, dict]:
    """
    Patient count, all-time collection and this month's collection for each
    of `branch_ids` -- two queries however many branches there are.

    The Admin branches grid used to ask for these one branch at a time (a few
    queries per branch); this is the same three numbers, grouped by branch.
    A branch with no patients or payments reads as zeros.
    """
    ids = list(branch_ids)
    today = timezone.localdate()

    figures = {
        branch_id: {"patients": 0, "collected": Decimal("0.00"), "month": Decimal("0.00")}
        for branch_id in ids
    }
    for row in (
        Patient.objects.registered()
        .filter(branch_id__in=ids)
        .values("branch_id")
        .annotate(n=Count("pk"))
    ):
        figures[row["branch_id"]]["patients"] = row["n"]
    for row in (
        _revenue_queryset()
        .filter(branch_id__in=ids)
        .values("branch_id")
        .annotate(
            total=Sum("amount"),
            month=Sum(
                "amount",
                filter=Q(created_at__year=today.year, created_at__month=today.month),
            ),
        )
    ):
        figures[row["branch_id"]]["collected"] = row["total"] or Decimal("0.00")
        figures[row["branch_id"]]["month"] = row["month"] or Decimal("0.00")
    return figures


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
    day = _revenue_queryset(branch_id).filter(created_at__date=reference).aggregate(
        patients=Count("patient_id", distinct=True),
        due=Sum("amount", filter=Q(category__in=["monthly", "installment"])),
    )

    return {
        "todayPatientsSeen": day["patients"],
        "todayDueCollected": day["due"] or Decimal("0.00"),
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

    # Counter buyers are not patients and must not inflate these cards.
    patients = Patient.objects.registered()
    if branch_id:
        patients = patients.filter(branch_id=branch_id)

    # Does the patient hold an active monthly service / installment plan?
    # Asked as EXISTS subqueries inside one aggregate, so the four counts
    # (everyone, active care, in progress, this month's intake) are one query.
    has_monthly = Exists(
        MonthlyEnrollment.objects.filter(patient=OuterRef("pk"), status="active")
    )
    has_plan = Exists(
        InstallmentPlan.objects.filter(patient=OuterRef("pk"), status="active")
    )
    counts = patients.aggregate(
        total=Count("pk"),
        active_care=Count("pk", filter=has_monthly),
        in_progress=Count("pk", filter=has_plan & ~has_monthly),
        intake=Count(
            "pk",
            filter=Q(created_at__year=reference.year, created_at__month=reference.month),
        ),
    )

    return {
        "total": counts["total"],
        "activeCare": counts["active_care"],
        "inProgress": counts["in_progress"],
        "actionNeeded": counts["total"] - counts["active_care"] - counts["in_progress"],
        "intake": counts["intake"],
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

    # Revenue: one grouped read by (method, category) -- the total, the count
    # and both breakdowns are sums over those same rows -- plus one for the
    # distinct patients, which can't be summed from groups.
    revenue = _revenue_queryset(branch_id).filter(**in_range)
    groups = list(
        revenue.values("method", "category").annotate(total=Sum("amount"), count=Count("id"))
    )
    patients_seen = revenue.aggregate(n=Count("patient_id", distinct=True))["n"]
    gross = sum((row["total"] for row in groups), Decimal("0.00"))

    by_method_totals: dict[str, Decimal] = {}
    by_category_totals: dict[str, Decimal] = {}
    for row in groups:
        by_method_totals[row["method"]] = by_method_totals.get(row["method"], Decimal("0.00")) + row["total"]
        # The category split leaves out uncategorised payments and material
        # sales, exactly as `revenue_by_category` does.
        if row["category"] not in ("", "material_sale"):
            by_category_totals[row["category"]] = (
                by_category_totals.get(row["category"], Decimal("0.00")) + row["total"]
            )
    by_method = [
        {"method": method, "amount": amount}
        for method, amount in sorted(by_method_totals.items(), key=lambda item: -item[1])
    ]
    by_category = [
        {"category": category, "amount": amount}
        for category, amount in sorted(by_category_totals.items(), key=lambda item: -item[1])
    ]

    refunds = _refund_queryset(branch_id).filter(
        reviewed_at__date__gte=date_from, reviewed_at__date__lte=date_to
    ).aggregate(total=Sum("amount"), count=Count("id"))
    refunded = refunds["total"] or Decimal("0.00")

    expenses = Expense.objects.filter(status__in=COUNTED_EXPENSE_STATUSES)
    if branch_id:
        expenses = expenses.filter(branch_id=branch_id)
    expense_stats = expenses.filter(**in_range).aggregate(total=Sum("amount"), count=Count("id"))
    expense_total = expense_stats["total"] or Decimal("0.00")

    patients = Patient.objects.registered()
    if branch_id:
        patients = patients.filter(branch_id=branch_id)
    patient_stats = patients.aggregate(
        new=Count("pk", filter=Q(**in_range)), total=Count("pk")
    )

    return {
        "dateFrom": date_from,
        "dateTo": date_to,
        "grossCollected": gross,
        "refunded": refunded,
        "expenses": expense_total,
        "netRevenue": gross - refunded - expense_total,
        "paymentCount": sum(row["count"] for row in groups),
        "patientsSeen": patients_seen,
        "newPatients": patient_stats["new"],
        "totalPatients": patient_stats["total"],
        "expenseCount": expense_stats["count"],
        "refundCount": refunds["count"],
        # Reuses the due-payments module rather than re-deriving it: that one
        # already handles partially-paid bills (outstanding is amount − paid,
        # not amount), and two implementations of "what is owed" would drift.
        "outstandingDue": due_summary(branch_id=branch_id)["totalDue"],
        "byMethod": by_method,
        "byCategory": by_category,
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

    for row in rows.values():
        row["netRevenue"] = row["collected"] - row["refunded"] - row["expenses"]

    # Newest first: a ledger is read from what just happened backwards.
    return [rows[day] for day in sorted(rows, reverse=True)]


def branch_activity(*, branch_id=None, date_from: date, date_to: date) -> list[dict]:
    """
    Every invoice, expense, refund, new patient, service enrollment and
    salary-payment decision in one reverse-chronological feed — what a
    manager scans to see "everything that happened" in a range, money-moving
    or not, instead of flipping between half a dozen separate screens.

    The money events (invoice/expense/refund) follow the same accounting
    rules as the rest of this module: an invoice is dated by when it was
    collected, an expense by when it was logged, a refund by when it was
    **approved** (a still-pending request has no `reviewed_at` yet, so it
    doesn't appear until it's decided). The non-money events (patient,
    enrollment, salary) are read from AuditLog — they carry no `direction`
    that could ever be "in" or "out", since registering a patient or
    requesting a salary payment doesn't move any money by itself (a salary
    request that's later *disbursed* shows up separately as its own expense
    row, the moment the money actually leaves).

    Fetches the whole range and sorts in Python rather than in the database:
    these differently-shaped querysets can't be UNIONed by date across
    several tables without raw SQL, and the ranges this feeds (a day, a
    month) are small enough that this is the plain-Django code without a
    performance cost.
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

    audit_in_range = AuditLog.objects.filter(**in_range)
    if branch_id:
        audit_in_range = audit_in_range.filter(branch_id=branch_id)

    salary_action_label = {
        AuditLog.Action.CREATE: "requested",
        AuditLog.Action.APPROVE: "approved",
        AuditLog.Action.REJECT: "rejected",
    }
    # Patient registrations, service enrollments and salary decisions all
    # come from the audit log: read together in ONE query (with the actor
    # joined, since each row names who did it), then told apart here.
    audit_rows = list(
        audit_in_range.filter(
            Q(
                action=AuditLog.Action.CREATE,
                target_type__in=["Patient", "MonthlyEnrollment", "InstallmentPlan"],
            )
            | Q(action__in=list(salary_action_label), target_type="SalaryPayment")
        ).select_related("actor")
    )
    patient_registrations = [
        entry for entry in audit_rows
        if entry.action == AuditLog.Action.CREATE and entry.target_type == "Patient"
    ]
    for entry in patient_registrations:
        name = entry.changes.get("name", "")
        rows.append(
            {
                "id": f"patient-{entry.id}",
                "type": ACTIVITY_TYPE_PATIENT,
                "occurredAt": entry.created_at,
                "reference": entry.changes.get("patient_code", ""),
                "description": f"New patient registered — {name}" if name else "New patient registered",
                "person": name,
                "performedBy": entry.actor.name if entry.actor else entry.actor_email,
                "amount": Decimal("0.00"),
                "direction": "neutral",
                "status": "registered",
            }
        )

    enrollment_logs = [
        entry for entry in audit_rows
        if entry.action == AuditLog.Action.CREATE
        and entry.target_type in ("MonthlyEnrollment", "InstallmentPlan")
    ]
    monthly_by_id = {
        str(row.id): row
        for row in MonthlyEnrollment.objects.filter(
            id__in=[e.target_id for e in enrollment_logs if e.target_type == "MonthlyEnrollment"]
        ).select_related("patient")
    }
    installment_by_id = {
        str(row.id): row
        for row in InstallmentPlan.objects.filter(
            id__in=[e.target_id for e in enrollment_logs if e.target_type == "InstallmentPlan"]
        ).select_related("patient")
    }
    for entry in enrollment_logs:
        enrollment = (
            monthly_by_id if entry.target_type == "MonthlyEnrollment" else installment_by_id
        ).get(entry.target_id)
        patient_name = enrollment.patient.name if enrollment else ""
        service_name = entry.changes.get("service", "")
        description = f"Enrolled in {service_name}" if service_name else "New service enrollment"
        rows.append(
            {
                "id": f"enrollment-{entry.id}",
                "type": ACTIVITY_TYPE_ENROLLMENT,
                "occurredAt": entry.created_at,
                "reference": entry.target_id,
                "description": f"{description} — {patient_name}" if patient_name else description,
                "person": patient_name,
                "performedBy": entry.actor.name if entry.actor else entry.actor_email,
                "amount": Decimal("0.00"),
                "direction": "neutral",
                "status": "enrolled",
            }
        )

    salary_logs = [entry for entry in audit_rows if entry.target_type == "SalaryPayment"]
    salary_by_id = {
        str(row.id): row
        for row in SalaryPayment.objects.filter(
            id__in=[e.target_id for e in salary_logs]
        ).select_related("staff")
    }
    for entry in salary_logs:
        payment = salary_by_id.get(entry.target_id)
        staff_name = payment.staff.name if payment else entry.changes.get("staff", "")
        verb = salary_action_label[entry.action]
        description = f"Salary payment {verb}" + (f" — {staff_name}" if staff_name else "")
        rows.append(
            {
                "id": f"salary-{entry.id}",
                "type": ACTIVITY_TYPE_SALARY,
                "occurredAt": entry.created_at,
                "reference": entry.target_id,
                "description": description,
                "person": staff_name,
                "performedBy": entry.actor.name if entry.actor else entry.actor_email,
                "amount": payment.amount if payment else Decimal("0.00"),
                "direction": "neutral",
                "status": verb,
            }
        )

    rows.sort(key=lambda row: row["occurredAt"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Branch report (the PDF the manager hands to the admin)
# ---------------------------------------------------------------------------


def _as_decimal_strings(value):
    """
    Every money figure out as a string, however deep it sits.

    The other reporting views declare DRF `DecimalField`s, which honour
    COERCE_DECIMAL_TO_STRING; this payload is nine nested sections and
    declaring forty fields to restate what the service already shapes would
    be a second description to keep in step. Walking it once is the smaller
    thing -- and without it the renderer emits floats, which is the one
    representation this codebase never uses for money.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _as_decimal_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_decimal_strings(item) for item in value]
    return value

# Money that belongs to no package: a daily visit or a counter sale goes
# through the generic payment endpoint, which records a description and no
# service. Named rather than dropped so "collected by service" still adds up
# to Collected.
UNLINKED_SERVICE_LABEL = "Other (daily visits, materials)"


def branch_report(*, branch_id=None, date_from: date, date_to: date) -> dict:
    """
    One branch's whole working period, in one read.

    Composed rather than invented: the money half is `branch_summary`
    unchanged, so the PDF and the Summary screen can never disagree about a
    figure. What this adds is everything the Summary page does not load --
    expenses by category, what each service earned, the patients taken on,
    attendance, materials, staff and enquiries -- plus the payment list that
    lets an Admin trace any number back to a receipt.

    Gathered server-side in one request on purpose. Fetching nine sections
    from nine endpoints would make them nine different moments: a payment
    taken between the first call and the last would appear in the total and
    not in the list, and the report would be quietly wrong in a way nobody
    could reproduce.
    """
    from apps.enquiries.models import Enquiry
    from apps.enrollments.models import (
        Booking,
        Installment,
        InstallmentPlan,
        MonthlyBill,
        MonthlyEnrollment,
    )
    from apps.materials.models import MaterialSaleItem
    from apps.patients.models import PatientAttendance
    from apps.staff.models import StaffAttendance

    in_range = {"created_at__date__gte": date_from, "created_at__date__lte": date_to}

    def scoped(queryset, field="branch_id"):
        return queryset.filter(**{field: branch_id}) if branch_id else queryset

    # --- expenses, split the way the Expenses screen splits them ----------
    expenses = scoped(Expense.objects.filter(**in_range))
    expenses_by_category = [
        {"category": row["category"], "amount": row["total"], "count": row["n"]}
        for row in expenses.filter(status__in=COUNTED_EXPENSE_STATUSES)
        .values("category")
        .annotate(total=Sum("amount"), n=Count("pk"))
        .order_by("-total")
    ]
    expense_status = expenses.aggregate(
        approved=Sum("amount", filter=Q(status=Expense.Status.APPROVED)),
        pending=Sum("amount", filter=Q(status=Expense.Status.PENDING)),
    )

    # --- patients and attendance -------------------------------------------
    #
    # Split by service kind rather than totalled. The two are different
    # rosters on different billing rules, and a combined "9 absent" says
    # nothing about which group stopped turning up -- which is the only
    # question the figure is read to answer.
    attendance = {
        kind: {"present": 0, "informed": 0, "absent": 0}
        for kind in PatientAttendance.ServiceKind.values
    }
    for row in (
        scoped(PatientAttendance.objects.filter(date__gte=date_from, date__lte=date_to))
        .values("service_kind")
        .annotate(
            present=Count("pk", filter=Q(status=PatientAttendance.Status.PRESENT)),
            informed=Count(
                "pk", filter=Q(status=PatientAttendance.Status.INFORMED_ABSENCE)
            ),
            absent=Count("pk", filter=Q(status=PatientAttendance.Status.ABSENT)),
        )
    ):
        attendance[row["service_kind"]] = {
            "present": row["present"],
            "informed": row["informed"],
            "absent": row["absent"],
        }

    # Who was taken on, not just how many. `registered()` because a walk-in
    # buyer is a billing row, never a new patient (docs/02, docs/06).
    new_patients = scoped(Patient.objects.registered().filter(**in_range)).order_by(
        "created_at"
    )
    # What each of them signed up for, so the list answers "and for what?"
    # without an Admin having to open the system. First enrolment wins: the
    # service they were registered *for*.
    signed_up: dict = {}
    for queryset in (
        scoped(MonthlyEnrollment.objects.filter(**in_range)),
        scoped(InstallmentPlan.objects.filter(**in_range)),
    ):
        for patient_id, service_name in queryset.values_list(
            "patient_id", "service__name"
        ):
            signed_up.setdefault(patient_id, service_name)
    newly_registered = [
        {
            "code": patient.patient_code,
            "name": patient.name,
            "age": patient.age,
            "gender": patient.gender,
            "phone": patient.phone,
            "service": signed_up.get(patient.pk, ""),
            "createdAt": patient.created_at,
        }
        for patient in new_patients
    ]

    # --- services taken up in the period -----------------------------------
    monthly_new = scoped(MonthlyEnrollment.objects.filter(**in_range)).count()
    plans_new = scoped(InstallmentPlan.objects.filter(**in_range)).count()
    bookings = scoped(
        Booking.objects.filter(date__gte=date_from, date__lte=date_to)
    ).aggregate(
        total=Count("pk"),
        cancelled=Count("pk", filter=Q(status=Booking.Status.CANCELLED)),
    )

    # --- materials sold ----------------------------------------------------
    sold = (
        MaterialSaleItem.objects.filter(
            payment__status=PaymentStatus.PAID,
            payment__created_at__date__gte=date_from,
            payment__created_at__date__lte=date_to,
        )
        .filter(**({"payment__branch_id": branch_id} if branch_id else {}))
        .values("material__name")
        # `qty`, not `quantity`: an annotation shadows the column it is named
        # after, and `F("quantity")` below would then resolve to this very
        # aggregate instead of the field.
        .annotate(
            qty=Sum("quantity"),
            amount=Sum(
                ExpressionWrapper(
                    F("quantity") * F("unit_price"),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                )
            ),
        )
        .order_by("-amount")
    )
    materials_sold = [
        {"name": row["material__name"], "quantity": row["qty"], "amount": row["amount"]}
        for row in sold
    ]

    # --- staff attendance --------------------------------------------------
    staff = scoped(
        StaffAttendance.objects.filter(date__gte=date_from, date__lte=date_to)
    ).aggregate(
        present=Count("pk", filter=Q(status=StaffAttendance.Status.PRESENT)),
        early_leave=Count("pk", filter=Q(status=StaffAttendance.Status.EARLY_LEAVE)),
        on_leave=Count("pk", filter=Q(status=StaffAttendance.Status.ON_LEAVE)),
        absent=Count("pk", filter=Q(status=StaffAttendance.Status.ABSENT)),
    )

    # --- enquiries: which ads produced calls, and how many came in ---------
    enquiries_qs = scoped(
        Enquiry.objects.filter(called_at__date__gte=date_from, called_at__date__lte=date_to)
    )
    enquiries = {
        "total": enquiries_qs.count(),
        "converted": enquiries_qs.filter(status=Enquiry.Status.CONVERTED).count(),
        "bySource": [
            {"source": row["source"], "count": row["n"]}
            for row in enquiries_qs.values("source").annotate(n=Count("pk")).order_by("-n")
        ],
    }

    # --- every receipt, so any figure above can be traced ------------------
    paid_payments = list(
        scoped(Payment.objects.filter(status=PaymentStatus.PAID, **in_range))
        .select_related("patient", "collected_by")
        .order_by("created_at")
    )
    payments = [
        {
            "receiptNumber": p.receipt_number,
            "patientName": p.patient.name,
            "patientCode": p.patient.patient_code,
            "category": p.category,
            "method": p.method,
            "status": p.status,
            "amount": p.amount,
            "collectedBy": p.collected_by.name if p.collected_by else "",
            "createdAt": p.created_at,
        }
        for p in paid_payments
    ]

    # --- what each service earned, and how many signed up for it -----------
    #
    # A Payment has no service FK -- only the free-text description that
    # prints on the receipt (payments/models.py) -- so the name is reached
    # through whichever table booked the money: a monthly bill, an
    # installment, or an online booking.
    #
    # The amount summed is the *payment*, not the bill's `amount_paid`: it is
    # the money that actually came through the desk, so these rows total to
    # Collected exactly. Anything with no package behind it lands in the
    # unlinked row for the same reason.
    service_of: dict = {}
    for queryset, name_path in (
        (MonthlyBill.objects, "enrollment__service__name"),
        (Installment.objects, "plan__service__name"),
        (Booking.objects, "service__name"),
    ):
        for payment_id, service_name in queryset.filter(
            payment_id__in=[p.pk for p in paid_payments]
        ).values_list("payment_id", name_path):
            service_of[payment_id] = service_name

    collected_by_service: dict = defaultdict(lambda: Decimal("0.00"))
    for payment in paid_payments:
        collected_by_service[
            service_of.get(payment.pk, UNLINKED_SERVICE_LABEL)
        ] += payment.amount

    enrolled_by_service: Counter = Counter()
    for queryset in (
        scoped(MonthlyEnrollment.objects.filter(**in_range)),
        scoped(InstallmentPlan.objects.filter(**in_range)),
    ):
        enrolled_by_service.update(queryset.values_list("service__name", flat=True))

    by_service = sorted(
        (
            {
                "service": service_name,
                "enrolled": enrolled_by_service.get(service_name, 0),
                "amount": collected_by_service.get(service_name, Decimal("0.00")),
            }
            for service_name in set(collected_by_service) | set(enrolled_by_service)
        ),
        # Biggest earner first; the unlinked row always last, because it is a
        # footnote and would otherwise read as the branch's best seller.
        key=lambda row: (
            row["service"] == UNLINKED_SERVICE_LABEL,
            -row["amount"],
            row["service"],
        ),
    )

    return _as_decimal_strings({
        **branch_summary(branch_id=branch_id, date_from=date_from, date_to=date_to),
        "expensesByCategory": expenses_by_category,
        "expensesApproved": expense_status["approved"] or Decimal("0.00"),
        "expensesPending": expense_status["pending"] or Decimal("0.00"),
        "byService": by_service,
        "attendance": attendance,
        "newlyRegistered": newly_registered,
        "services": {
            "newMonthly": monthly_new,
            "newInstallmentPlans": plans_new,
            "bookings": bookings["total"],
            "bookingsCancelled": bookings["cancelled"],
        },
        "materialsSold": materials_sold,
        "staffAttendance": staff,
        "enquiries": enquiries,
        "payments": payments,
    })
