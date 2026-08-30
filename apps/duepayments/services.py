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

from apps.enrollments.models import (
    BillStatus,
    EnrollmentStatus,
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
    if payable.status == BillStatus.WRITTEN_OFF:
        return False
    if payable.paid_at is None:
        return True
    return payable.paid_at > cutoff


def collect_due_items(*, branch_id=None, as_of: date | None = None) -> list[dict]:
    """
    Every currently-payable item, one per enrollment/plan.

    Only the **oldest** unpaid item per enrollment appears: that's the only one
    payable next under the oldest-first rule, so listing the rest would offer
    the manager actions that would be refused.
    """
    items: list[dict] = []

    bills = (
        MonthlyBill.objects.filter(enrollment__status=EnrollmentStatus.ACTIVE)
        .exclude(status__in=[BillStatus.PAID, BillStatus.WRITTEN_OFF])
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
                "amount": bill.outstanding,
                "dueDate": bill.due_date,
                "status": bill.effective_status(),
            }
        )

    installments = (
        Installment.objects.filter(plan__status=EnrollmentStatus.ACTIVE)
        .exclude(status__in=[BillStatus.PAID, BillStatus.WRITTEN_OFF])
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
                "dueDate": installment.due_date,
                "status": installment.effective_status(),
                "installmentIndex": installment.index,
                "installmentsTotal": total_parts,
                "installmentsRemaining": total_parts - installment.index,
            }
        )

    return items


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
        installment = sum(
            (i["amount"] for i in items if i["type"] == "installment"), Decimal("0.00")
        )
        return {
            "totalDue": monthly + installment,
            "monthlyDue": monthly,
            "installmentDue": installment,
        }

    cutoff = _end_of_day(as_of)

    monthly_total = Decimal("0.00")
    bills = (
        MonthlyBill.objects.filter(enrollment__status=EnrollmentStatus.ACTIVE)
        .select_related("enrollment")
        .order_by("enrollment_id", "month")
    )
    if branch_id:
        bills = bills.filter(enrollment__branch_id=branch_id)

    seen = set()
    for bill in bills:
        if bill.enrollment_id in seen:
            continue
        # A bill that hadn't been issued yet on the target date can't have
        # been owed then.
        if bill.due_date.replace(day=1) > as_of.replace(day=1):
            continue
        if not _was_outstanding_at(bill, cutoff):
            continue
        seen.add(bill.enrollment_id)
        monthly_total += bill.amount - bill.amount_paid

    installment_total = Decimal("0.00")
    installments = (
        Installment.objects.filter(plan__status=EnrollmentStatus.ACTIVE)
        .select_related("plan")
        .order_by("plan_id", "index")
    )
    if branch_id:
        installments = installments.filter(plan__branch_id=branch_id)

    seen_plans = set()
    for installment in installments:
        if installment.plan_id in seen_plans:
            continue
        if installment.plan.created_at > cutoff:
            continue
        if not _was_outstanding_at(installment, cutoff):
            continue
        seen_plans.add(installment.plan_id)
        installment_total += installment.amount - installment.amount_paid

    return {
        "totalDue": monthly_total + installment_total,
        "monthlyDue": monthly_total,
        "installmentDue": installment_total,
    }
