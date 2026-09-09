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
                "outstandingTotal": enrollment.outstanding_total(),
                "dueDate": bill.due_date,
                "status": bill.effective_status(),
                "serviceActive": enrollment.is_active,
            }
        )

    installments = (
        Installment.objects
        .exclude(status__in=CLOSED_STATUSES)
        .select_related("plan", "plan__patient", "plan__service", "plan__branch")
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
        total_parts = plan.installments.count()
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
                "outstandingTotal": plan.outstanding_total(),
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

    return sum(
        (i.outstanding for i in installments if i.outstanding > 0), Decimal("0.00")
    )


def due_summary(*, branch_id=None, as_of: date | None = None) -> dict:
    """
    Outstanding totals, optionally reconstructed for a past date.

    With no `as_of`, this is the current snapshot. With one, it answers what
    was outstanding at the end of that day — which is what the dashboard's
    date picker needs, and why `paid_at` exists on bills at all.
    """
    if as_of is None:
        items = collect_due_items(branch_id=branch_id)
        monthly = sum(
            (i["amount"] for i in items if i["type"] == "monthly"), Decimal("0.00")
        )
        # Deliberately NOT summed from `items`: that list is what a manager can
        # collect right now, so it holds one installment per plan. What's owed
        # is the whole remaining balance of every active plan — a patient who
        # has paid the 1st of 3 still owes the other two, and the dashboard has
        # to say so. Monthly stays list-derived: those renew indefinitely, so
        # "everything ahead" isn't a finite number there.
        installment = _installment_balance(branch_id=branch_id)
        return {
            "totalDue": monthly + installment,
            "monthlyDue": monthly,
            "installmentDue": installment,
        }

    cutoff = _end_of_day(as_of)

    monthly_total = Decimal("0.00")
    # Inactive services included, matching `collect_due_items` — a kept month
    # is owed whether or not the service is still running, and the two have to
    # agree or today's reconstruction stops matching today's snapshot.
    bills = (
        MonthlyBill.objects
        .select_related("enrollment")
        .order_by("enrollment_id", "month")
    )
    if branch_id:
        bills = bills.filter(enrollment__branch_id=branch_id)

    seen = set()
    for bill in bills:
        if bill.enrollment_id in seen:
            continue
        # A bill's due_date says nothing about when the row started existing,
        # so existence is decided by when the enrollment was created —
        # mirroring the installment loop's `plan.created_at` check below.
        # Ordinary months are now raised as they arrive, but months paid ahead
        # are created early, which is what the advance guard just below is
        # for.
        if bill.enrollment.created_at > cutoff:
            continue
        # A month paid ahead was never owed on a date before it began. Its
        # `paid_at` is after the cutoff, so `_was_outstanding_at` would call
        # it outstanding and charge a past month for a future one; the
        # enrollment guard above can't catch it because the enrollment is
        # old, and `_was_outstanding_at` can't because it cannot see a month.
        #
        # Narrowed to advances on purpose. An ordinary unpaid future bill
        # still counts, because the current snapshot counts it too — the next
        # payable bill is what "outstanding" has always meant here, and
        # today's reconstruction has to agree with today's snapshot.
        if bill.status == BillStatus.ADVANCE and bill.month > month_key(as_of):
            continue
        if not _was_outstanding_at(bill, cutoff):
            continue
        seen.add(bill.enrollment_id)
        monthly_total += _outstanding_at(bill, cutoff)

    installment_total = Decimal("0.00")
    installments = (
        Installment.objects
        .select_related("plan")
        .order_by("plan_id", "index")
    )
    if branch_id:
        installments = installments.filter(plan__branch_id=branch_id)

    # Every unpaid installment, not just the currently-payable one: an
    # installment plan is a single agreed debt, so once a patient is on one
    # the whole remaining balance is money the clinic is owed. Monthly
    # enrollments above stay one-bill-at-a-time on purpose -- they renew
    # indefinitely, so "everything ahead" isn't a finite figure there.
    for installment in installments:
        if installment.plan.created_at > cutoff:
            continue
        if not _was_outstanding_at(installment, cutoff):
            continue
        installment_total += _outstanding_at(installment, cutoff)

    return {
        "totalDue": monthly_total + installment_total,
        "monthlyDue": monthly_total,
        "installmentDue": installment_total,
    }
