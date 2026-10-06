"""
`due_summary` is computed by the database; this proves it agrees with the
plain-Python rules it replaced.

The reference below is the readable specification — the original loops,
kept as test code. Random bills (every status, paid or not, paid before or
after the date asked about, advances, forgiven months, partial balances,
enrollments created after the date) are generated, and the SQL figures must
equal the reference for every date and branch tried. Any future change to
how "what was owed on that day" is derived has to keep this green.
"""

import random
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.duepayments.services import due_summary
from apps.enrollments.models import (
    FORGIVEN_STATUSES,
    BillStatus,
    Installment,
    InstallmentPlan,
    MonthlyBill,
    MonthlyEnrollment,
)
from apps.enrollments.services import month_key

pytestmark = [pytest.mark.django_db, pytest.mark.money]


def _end_of_day(day):
    return timezone.make_aware(datetime.combine(day, time.max))


def _was_outstanding_at(item, cutoff):
    if item.status in FORGIVEN_STATUSES:
        return False
    if item.paid_at is None:
        return True
    return item.paid_at > cutoff


def _outstanding_at(item, cutoff):
    if item.paid_at is not None and item.paid_at > cutoff:
        return item.amount
    return item.amount - item.amount_paid


def reference_historical(*, branch_id, as_of):
    """The original loops: the specification."""
    cutoff = _end_of_day(as_of)
    monthly = Decimal("0.00")
    bills = MonthlyBill.objects.select_related("enrollment").order_by("enrollment_id", "month")
    if branch_id:
        bills = bills.filter(enrollment__branch_id=branch_id)
    seen = set()
    for bill in bills:
        if bill.enrollment_id in seen:
            continue
        if bill.enrollment.created_at > cutoff:
            continue
        if bill.status == BillStatus.ADVANCE and bill.month > month_key(as_of):
            continue
        if not _was_outstanding_at(bill, cutoff):
            continue
        seen.add(bill.enrollment_id)
        monthly += _outstanding_at(bill, cutoff)

    installment_total = Decimal("0.00")
    installments = Installment.objects.select_related("plan").order_by("plan_id", "index")
    if branch_id:
        installments = installments.filter(plan__branch_id=branch_id)
    for installment in installments:
        if installment.plan.created_at > cutoff:
            continue
        if not _was_outstanding_at(installment, cutoff):
            continue
        installment_total += _outstanding_at(installment, cutoff)
    return monthly, installment_total


def reference_current(*, branch_id):
    """Today's snapshot: first collectable bill per enrollment; every unpaid installment."""
    from apps.enrollments.models import CLOSED_STATUSES, NON_OUTSTANDING_STATUSES

    bills = (
        MonthlyBill.objects.exclude(status__in=NON_OUTSTANDING_STATUSES)
        .order_by("enrollment_id", "month")
    )
    if branch_id:
        bills = bills.filter(enrollment__branch_id=branch_id)
    seen, monthly = set(), Decimal("0.00")
    for bill in bills:
        if bill.enrollment_id in seen or bill.outstanding <= 0:
            continue
        seen.add(bill.enrollment_id)
        monthly += bill.outstanding

    installments = Installment.objects.exclude(status__in=CLOSED_STATUSES)
    if branch_id:
        installments = installments.filter(plan__branch_id=branch_id)
    installment_total = sum(
        (i.outstanding for i in installments if i.outstanding > 0), Decimal("0.00")
    )
    return monthly, installment_total


STATUSES = [s for s in BillStatus.values]


def build_data(rng, *, branches, patient_factory, service_factory, today):
    for branch in branches:
        service = service_factory(branch=branch, fee=Decimal("5000.00"))
        plan_service = service_factory(branch=branch, code=f"INS-{branch.pk}", fee=Decimal("9000.00"))
        for _ in range(12):
            patient = patient_factory(branch=branch)
            enrollment = MonthlyEnrollment.objects.create(patient=patient, service=service, branch=branch)
            # Enrollments created at different times, some after the dates asked about.
            MonthlyEnrollment.objects.filter(pk=enrollment.pk).update(
                created_at=timezone.now() - timedelta(days=rng.randint(0, 300))
            )
            for offset in rng.sample(range(0, 9), rng.randint(1, 6)):
                first = (today.replace(day=1) - timedelta(days=30 * offset)).replace(day=1)
                amount = Decimal(rng.choice([3000, 5000, 7500]))
                status = rng.choice(STATUSES)
                paid = rng.choice([Decimal("0.00"), amount, amount / 2])
                paid_at = None
                if rng.random() < 0.6:
                    paid_at = timezone.now() - timedelta(days=rng.randint(0, 280))
                MonthlyBill.objects.create(
                    enrollment=enrollment, month=month_key(first), label=month_key(first),
                    amount=amount, amount_paid=paid, status=status,
                    due_date=first.replace(day=5), paid_at=paid_at,
                )
        for _ in range(5):
            plan = InstallmentPlan.objects.create(
                patient=patient_factory(branch=branch), service=plan_service, branch=branch,
                total_amount=Decimal("9000.00"),
            )
            InstallmentPlan.objects.filter(pk=plan.pk).update(
                created_at=timezone.now() - timedelta(days=rng.randint(0, 300))
            )
            for index in range(1, rng.randint(2, 4) + 1):
                amount = Decimal("3000.00")
                Installment.objects.create(
                    plan=plan, index=index, label=f"{index}", amount=amount,
                    amount_paid=rng.choice([Decimal("0.00"), amount, Decimal("1000.00")]),
                    status=rng.choice(STATUSES), due_date=today,
                    paid_at=(timezone.now() - timedelta(days=rng.randint(0, 280)))
                    if rng.random() < 0.6 else None,
                )


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_sql_summary_matches_the_reference(
    seed, branch, other_branch, patient_factory, service_factory
):
    rng = random.Random(seed)
    today = timezone.localdate()
    build_data(
        rng, branches=[branch, other_branch],
        patient_factory=patient_factory, service_factory=service_factory, today=today,
    )

    dates = [today] + [today - timedelta(days=rng.randint(1, 270)) for _ in range(9)]
    seen_monthly, seen_installment = set(), set()
    for branch_id in (None, branch.pk, other_branch.pk):
        for as_of in dates:
            expected_monthly, expected_installment = reference_historical(
                branch_id=branch_id, as_of=as_of
            )
            got = due_summary(branch_id=branch_id, as_of=as_of)
            assert got["monthlyDue"] == expected_monthly, (seed, branch_id, as_of)
            assert got["installmentDue"] == expected_installment, (seed, branch_id, as_of)
            assert got["totalDue"] == expected_monthly + expected_installment
            seen_monthly.add(expected_monthly)
            seen_installment.add(expected_installment)

        expected_monthly, expected_installment = reference_current(branch_id=branch_id)
        got = due_summary(branch_id=branch_id)
        assert got["monthlyDue"] == expected_monthly, (seed, branch_id)
        assert got["installmentDue"] == expected_installment, (seed, branch_id)

    # The comparison has to be about something: the random data must produce
    # real, varying figures, not a test that agrees because both sides are 0.
    assert max(seen_monthly) > 0 and max(seen_installment) > 0
    assert len(seen_monthly) >= 3 and len(seen_installment) >= 3
