"""
Outstanding dues — the unified view over monthly bills and installments.

Two things make this module subtle:

**Balances, not flags.** Partial refunds mean a bill can be part-settled, so
outstanding is `amount − amount_paid`, never `amount`. Summing `amount` would
overstate what patients owe — the exact bug docs/04 warns about.

**Historical reconstruction.** "What was outstanding on 27 August?" is
answered from `paid_at`, not from current status. A bill counts as outstanding
for a past date if it was unpaid then, even if it's paid now.
"""

from datetime import date, datetime, time
from decimal import Decimal

from django.db.models import Case, Count, DecimalField, F, IntegerField, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from apps.enrollments.services import month_key
from apps.enrollments.models import (
    CLOSED_STATUSES,
    FORGIVEN_STATUSES,
    NON_OUTSTANDING_STATUSES,
    BillStatus,
    Installment,
    MonthlyBill,
)


def _end_of_day(day: date):
    """
    Compare against the END of the target day, not its start.

    Otherwise a payment made this afternoon still reads as outstanding for
    today — a real off-by-one that made "today's outstanding" wrong until it
    was caught in the frontend.
    """
    return timezone.make_aware(datetime.combine(day, time.max))


def _was_outstanding_at(payable, cutoff) -> bool:
    """Unpaid as of `cutoff` — either never paid, or paid after it."""
    if payable.status in FORGIVEN_STATUSES:
        return False
    if payable.paid_at is None:
        return True
    return payable.paid_at > cutoff


def _outstanding_at(payable, cutoff) -> Decimal:
    """
    What was still owed as of `cutoff`, for an item `_was_outstanding_at`
    already confirmed was unpaid then.

    `amount_paid` is current state, not history: if the payment landed after
    `cutoff`, nothing had been paid toward this item yet as of that date, so
    the full original amount was owed -- not `amount - amount_paid`, which
    would use tomorrow's paid-in-full figure to answer a question about
    yesterday and silently report the item as already settled.

    A payment that happened at or before `cutoff` did contribute, so its
    current `amount_paid` correctly stood at that point too (nothing since
    then can have reduced it without also clearing `paid_at`, which would
    have made `_was_outstanding_at` true for a different reason).
    """
    if payable.paid_at is not None and payable.paid_at > cutoff:
        return payable.amount
    return payable.amount - payable.amount_paid


def _remaining_on(model, parent: str):
    """
    What is still owed across every row of the same parent, as an annotation.

    The database equivalent of `outstanding_total()` -- forgiven rows owe
    nothing, and a row never owes less than zero -- so only the total crosses
    the wire, not every bill of the patient's history.
    """
    money = DecimalField(max_digits=12, decimal_places=2)
    owed = (
        model.objects.filter(**{parent: OuterRef(parent)})
        .exclude(status__in=FORGIVEN_STATUSES)
        .order_by()
        .values(parent)
        .annotate(
            total=Sum(
                Greatest(F("amount") - F("amount_paid"), Value(Decimal("0.00"))),
                output_field=money,
            )
        )
        .values("total")[:1]
    )
    return Coalesce(Subquery(owed, output_field=money), Value(Decimal("0.00")), output_field=money)


def _parts_in(model, parent: str):
    """How many rows the parent has, as an annotation."""
    count = (
        model.objects.filter(**{parent: OuterRef(parent)})
        .order_by()
        .values(parent)
        .annotate(n=Count("pk"))
        .values("n")[:1]
    )
    return Subquery(count, output_field=IntegerField())


def collect_due_items(
    *, branch_id=None, as_of: date | None = None, month: str | None = None
) -> list[dict]:
    """
    Every currently-payable item, one per enrollment/plan.

    Only the **oldest** unpaid item per enrollment appears: that's the only one
    payable next under the oldest-first rule, so listing the rest would offer
    the manager actions that would be refused.

    `month` ("2026-09") answers "who owes as of the end of this month", which
    is the question the Due Payments screen's month picker asks. A monthly row
    is kept when its payable bill belongs to that month **or an earlier one**:

      * a patient who has settled September has October as their oldest unpaid
        bill, so September's view drops them — paying the running month moves
        the debt to the next one, which is the whole point of the picker;
      * a patient who has not paid September still shows in September, and so
        does one who last paid in July: they owe for September too, and hiding
        the longest-overdue patient from the current month would be the worst
        possible thing this filter could do.

    Installments are untouched by it — a plan is one agreed debt with its own
    schedule, not a monthly cycle, and the screen shows them side by side
    rather than mixed in.
    """
    items: list[dict] = []

    # Inactive services are included. Their kept months are still owed, and
    # the Due Payments screen is the only place they can be cleared — which a
    # returning patient must do before any service can be reactivated. Each
    # row carries `serviceActive` so the screen can say which is which.
    bills = (
        MonthlyBill.objects
        .exclude(
            status__in=NON_OUTSTANDING_STATUSES
        )
        .select_related(
            "enrollment", "enrollment__patient", "enrollment__service", "enrollment__branch"
        )
        # The enrollment's whole remaining balance, summed by the database:
        # reading it per row was one query per patient, and prefetching every
        # bill instead moved the patient's entire paid history each request.
        .annotate(enrollment_remaining=_remaining_on(MonthlyBill, "enrollment_id"))
        .order_by("enrollment_id", "month")
    )
    if branch_id:
        bills = bills.filter(enrollment__branch_id=branch_id)

    seen_enrollments = set()
    for bill in bills:
        if bill.enrollment_id in seen_enrollments:
            continue  # oldest-first: only the first per enrollment is payable
        if bill.outstanding <= 0:
            continue
        seen_enrollments.add(bill.enrollment_id)

        # Marked seen before the month check so a filtered-out enrollment
        # can't fall through to its *second* unpaid bill and reappear as a
        # row the manager isn't allowed to collect yet.
        if month and bill.month > month:
            continue

        enrollment = bill.enrollment
        items.append(
            {
                "key": f"monthly-{enrollment.id}-{bill.month}",
                "type": "monthly",
                "refId": str(enrollment.id),
                "itemId": str(bill.id),
                "patientId": str(enrollment.patient_id),
                "patientName": enrollment.patient.name,
                "patientCode": enrollment.patient.patient_code,
                "serviceId": str(enrollment.service_id),
                "serviceName": enrollment.service.name,
                "branchId": str(enrollment.branch_id),
                "label": bill.label,
                # The bill's own month, so the screen can say which cycle a
                # row belongs to without parsing it back out of the label.
                "month": bill.month,
                "amount": bill.outstanding,
                # Everything unpaid on the enrollment, not just this bill —
                # what terminating would write off, which the confirmation
                # has to state before the manager agrees to it.
                "outstandingTotal": bill.enrollment_remaining,
                "dueDate": bill.due_date,
                "status": bill.effective_status(),
                "serviceActive": enrollment.is_active,
            }
        )

    installments = (
        Installment.objects
        .exclude(status__in=CLOSED_STATUSES)
        .select_related("plan", "plan__patient", "plan__service", "plan__branch")
        # Same reason: the plan's part count and remaining total, from the
        # database rather than by reading every installment of every plan.
        .annotate(
            plan_remaining=_remaining_on(Installment, "plan_id"),
            plan_parts=_parts_in(Installment, "plan_id"),
        )
        .order_by("plan_id", "index")
    )
    if branch_id:
        installments = installments.filter(plan__branch_id=branch_id)

    seen_plans = set()
    for installment in installments:
        if installment.plan_id in seen_plans:
            continue
        if installment.outstanding <= 0:
            continue
        seen_plans.add(installment.plan_id)

        plan = installment.plan
        total_parts = installment.plan_parts
        items.append(
            {
                "key": f"installment-{plan.id}-{installment.index}",
                "type": "installment",
                "refId": str(plan.id),
                "itemId": str(installment.id),
                "patientId": str(plan.patient_id),
                "patientName": plan.patient.name,
                "patientCode": plan.patient.patient_code,
                "serviceId": str(plan.service_id),
                "serviceName": plan.service.name,
                "branchId": str(plan.branch_id),
                "label": installment.label,
                "amount": installment.outstanding,
                # The whole remaining plan, not just this installment — see
                # the matching note on the monthly branch above.
                "outstandingTotal": installment.plan_remaining,
                "dueDate": installment.due_date,
                "status": installment.effective_status(),
                "serviceActive": plan.is_active,
                "installmentIndex": installment.index,
                "installmentsTotal": total_parts,
                "installmentsRemaining": total_parts - installment.index,
            }
        )

    return items


def _installment_balance(*, branch_id=None) -> Decimal:
    """
    Everything still owed on installment plans, right now.

    Forgiven installments are excluded — a write-off or a cancellation is the
    sanctioned way to close an uncollectable plan, so counting them would make
    the figure impossible to ever clear. Plans made inactive are *not*
    excluded: whatever the manager chose to keep is still owed.
    """
    installments = Installment.objects.exclude(status__in=CLOSED_STATUSES)
    if branch_id:
        installments = installments.filter(plan__branch_id=branch_id)

    # Summed by the database. A row never owes less than zero (an installment
    # over-collected on cannot make the plan look overpaid), hence Greatest.
    return installments.aggregate(
        s=Sum(Greatest(F("amount") - F("amount_paid"), Value(Decimal("0.00"))))
    )["s"] or Decimal("0.00")


def _next_payable_total(*, branch_id=None) -> Decimal:
    """
    What a manager could collect right now on monthly bills: the first unpaid
    bill of each enrollment, summed -- the same figure `collect_due_items`
    lists row by row, but computed by the database instead of loading every
    unpaid bill with its enrollment, patient, service and branch.
    """
    unpaid = (
        MonthlyBill.objects.exclude(status__in=NON_OUTSTANDING_STATUSES)
        .filter(amount_paid__lt=F("amount"))
    )
    if branch_id:
        unpaid = unpaid.filter(enrollment__branch_id=branch_id)
    first_per_enrollment = unpaid.order_by("enrollment_id", "month").distinct("enrollment_id")
    return MonthlyBill.objects.filter(pk__in=first_per_enrollment.values("pk")).aggregate(
        s=Sum(F("amount") - F("amount_paid"))
    )["s"] or Decimal("0.00")


def due_summary(*, branch_id=None, as_of: date | None = None) -> dict:
    """
    Outstanding totals, optionally reconstructed for a past date.

    With no `as_of`, this is the current snapshot. With one, it answers what
    was outstanding at the end of that day — which is what the dashboard's
    date picker needs, and why `paid_at` exists on bills at all.
    """
    if as_of is None:
        monthly = _next_payable_total(branch_id=branch_id)
        # Installments are deliberately NOT "the next payable one per plan"
        # like monthly bills: what's owed is the whole remaining balance of
        # every active plan — a patient who
        # has paid the 1st of 3 still owes the other two, and the dashboard has
        # to say so. Monthly stays one-bill-at-a-time: those renew
        # indefinitely, so "everything ahead" isn't a finite number there.
        installment = _installment_balance(branch_id=branch_id)
        return {
            "totalDue": monthly + installment,
            "monthlyDue": monthly,
            "installmentDue": installment,
        }

    cutoff = _end_of_day(as_of)
    zero = Decimal("0.00")

    # What was still owed on a payable at `cutoff`. `amount_paid` is current
    # state, so a payment that landed after the cutoff must not count as
    # paid yet: that case owes the full original amount (`_outstanding_at`).
    owed_then = Case(
        When(paid_at__gt=cutoff, then=F("amount")),
        default=F("amount") - F("amount_paid"),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )
    # `_was_outstanding_at` as a filter: not forgiven, and either never paid
    # or paid only after the cutoff.
    unpaid_then = ~Q(status__in=FORGIVEN_STATUSES) & (
        Q(paid_at__isnull=True) | Q(paid_at__gt=cutoff)
    )

    # Monthly: for each enrollment, the FIRST bill (by month) that was
    # outstanding then -- the one a manager could have collected that day.
    # Inactive services included, matching `collect_due_items` -- a kept
    # month is owed whether or not the service is still running, and the two
    # have to agree or today's reconstruction stops matching today's
    # snapshot.
    #
    # A bill's due_date says nothing about when the row started existing, so
    # existence is decided by when the enrollment was created. And a month
    # paid ahead was never owed on a date before it began: its `paid_at` is
    # after the cutoff, so it would otherwise read as outstanding and charge
    # a past month for a future one. Narrowed to advances on purpose -- an
    # ordinary unpaid future bill still counts, because the current snapshot
    # counts it too.
    candidates = MonthlyBill.objects.filter(
        unpaid_then, enrollment__created_at__lte=cutoff
    ).exclude(status=BillStatus.ADVANCE, month__gt=month_key(as_of))
    if branch_id:
        candidates = candidates.filter(enrollment__branch_id=branch_id)
    first_per_enrollment = candidates.order_by("enrollment_id", "month").distinct(
        "enrollment_id"
    )
    monthly_total = MonthlyBill.objects.filter(
        pk__in=first_per_enrollment.values("pk")
    ).aggregate(s=Sum(owed_then))["s"] or zero

    # Installments: every unpaid one, not just the currently-payable one. An
    # installment plan is a single agreed debt, so once a patient is on one
    # the whole remaining balance is money the clinic is owed. Monthly
    # enrollments above stay one-bill-at-a-time on purpose -- they renew
    # indefinitely, so "everything ahead" isn't a finite figure there.
    installments = Installment.objects.filter(unpaid_then, plan__created_at__lte=cutoff)
    if branch_id:
        installments = installments.filter(plan__branch_id=branch_id)
    installment_total = installments.aggregate(s=Sum(owed_then))["s"] or zero

    return {
        "totalDue": monthly_total + installment_total,
        "monthlyDue": monthly_total,
        "installmentDue": installment_total,
    }
