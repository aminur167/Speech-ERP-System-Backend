"""
Installment collection is computed in memory and written in one statement;
this proves it does exactly what the step-by-step version it replaced did.

`reference_collect` below is that earlier implementation, kept as test code:
one read or write at a time against the database, with the schedule logic
spelled out in the order the rules were first written. Two identical plans
are built, the reference is run on one and the real function on the other,
through a random series of payments (the scheduled amount, short, over,
zero, out of order, on forgiven schedules), and after every step the two
plans must hold identical installments and have produced identical payments
and identical refusals.
"""

import collections
import random
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db.models import F, Sum
from django.utils import timezone

from apps.enrollments import services
from apps.enrollments.models import (
    CLOSED_STATUSES,
    FORGIVEN_STATUSES,
    BillStatus,
    EnrollmentStatus,
    Installment,
)
from apps.payments import services as payment_services
from apps.payments.models import Payment, PaymentCategory
from apps.services.models import Service

pytestmark = [pytest.mark.django_db, pytest.mark.money]


# ---------------------------------------------------------------------------
# The reference: the original implementation, verbatim in behaviour.
# ---------------------------------------------------------------------------


def _ref_redistribute(plan, *, after_index):
    later = list(
        plan.installments.filter(index__gt=after_index)
        .exclude(status__in=CLOSED_STATUSES)
        .order_by("index")
    )
    if not later:
        return
    collected = plan.installments.aggregate(s=Sum("amount_paid"))["s"] or Decimal("0.00")
    remaining = plan.total_amount - collected
    if remaining <= 0:
        plan.installments.filter(pk__in=[row.pk for row in later]).delete()
        return
    amounts = services.split_equally(remaining, len(later))
    for row, amount in zip(later, amounts):
        row.amount = amount
    Installment.objects.bulk_update(later, ["amount"])


def reference_collect(*, actor, branch, installment, method, amount=None):
    plan = installment.plan
    installment = Installment.objects.select_for_update().get(pk=installment.pk)

    if installment.is_settled or installment.status in CLOSED_STATUSES:
        raise services.EnrollmentError("settled", code="already_paid")

    services._assert_is_oldest_unpaid(plan, installment)

    scheduled = installment.outstanding
    collecting = scheduled if amount is None else Decimal(amount)
    if collecting <= 0:
        raise services.EnrollmentError("zero", code="invalid_amount")

    plan_outstanding = plan.total_amount - (
        plan.installments.aggregate(s=Sum("amount_paid"))["s"] or Decimal("0.00")
    )
    if collecting > plan_outstanding:
        raise services.EnrollmentError("too much", code="exceeds_outstanding")

    is_last = not plan.installments.filter(index__gt=installment.index).exists()
    short = collecting < scheduled

    payment, created = payment_services.record_payment(
        actor=actor, branch=branch, patient=plan.patient, amount=collecting, method=method,
        category=PaymentCategory.INSTALLMENT,
        description=f"{plan.service.name} — {installment.label}",
    )

    installment.amount_paid = installment.amount_paid + collecting
    if short and is_last:
        installment.status = (
            BillStatus.OVERDUE if installment.due_date < timezone.localdate() else BillStatus.DUE
        )
        installment.payment = payment
        installment.save(update_fields=["amount_paid", "status", "payment"])
    else:
        installment.amount = installment.amount_paid
        installment.status = BillStatus.PAID
        installment.paid_at = timezone.now()
        installment.payment = payment
        installment.save(update_fields=["amount", "amount_paid", "status", "paid_at", "payment"])
        _ref_redistribute(plan, after_index=installment.index)

    nxt = plan.installments.filter(status=BillStatus.UPCOMING).order_by("index").first()
    if nxt is not None:
        nxt.status = BillStatus.DUE
        nxt.save(update_fields=["status"])

    remaining = plan.installments.exclude(status__in=FORGIVEN_STATUSES).aggregate(
        owed=Sum(F("amount") - F("amount_paid"))
    )["owed"] or Decimal("0.00")
    payment.due_after = max(Decimal("0.00"), remaining)
    payment.save(update_fields=["due_after"])
    return payment


# ---------------------------------------------------------------------------


# Which hard cases the random runs actually reached.
EVENTS = collections.Counter()


def snapshot(plan):
    return [
        (row.index, row.amount, row.amount_paid, row.status, row.paid_at is not None,
         row.payment_id is not None)
        for row in Installment.objects.filter(plan=plan).order_by("index")
    ]


def twin_plans(rng, *, manager, branch, patient_factory, service_factory):
    package = service_factory(
        category=Service.Category.INSTALLMENT,
        fee=Decimal(rng.choice(["9000.00", "15000.00", "18500.00", "10000.00"])),
    )
    parts = rng.randint(2, 6)
    window = rng.choice([None, (timezone.localdate() - timedelta(days=40), timezone.localdate() + timedelta(days=80))])
    kwargs = {"number_of_installments": parts}
    if window:
        kwargs.update(starts_on=window[0], ends_on=window[1])
    plans = [
        services.create_installment_plan(
            actor=manager, branch=branch, patient=patient_factory(), service=package, **kwargs
        )
        for _ in range(2)
    ]
    # The same forgiven rows on both, sometimes.
    if rng.random() < 0.3 and parts > 2:
        cancelled = rng.randint(2, parts)
        for plan in plans:
            Installment.objects.filter(plan=plan, index=cancelled).update(
                status=BillStatus.CANCELLED
            )
    return plans


def outcome(call):
    try:
        payment = call()
    except services.EnrollmentError as exc:
        return ("refused", exc.code)
    return ("paid", payment.amount, payment.due_after, payment.description)


@pytest.mark.parametrize("seed", range(12))
def test_new_collection_matches_the_reference(
    seed, manager, branch, patient_factory, service_factory
):
    rng = random.Random(seed)
    for _ in range(4):
        reference_plan, real_plan = twin_plans(
            rng, manager=manager, branch=branch,
            patient_factory=patient_factory, service_factory=service_factory,
        )

        for _step in range(rng.randint(2, 9)):
            index = (
                rng.randint(1, Installment.objects.filter(plan=real_plan).count() + 1)
                if rng.random() < 0.15
                else None
            )
            real_rows = list(real_plan.installments.order_by("index"))
            reference_rows = list(reference_plan.installments.order_by("index"))
            if not real_rows:
                break
            if index is None:
                oldest = real_plan.oldest_unpaid_installment()
                pick = (oldest.index if oldest else real_rows[0].index)
            else:
                pick = min(index, real_rows[-1].index)
            real_target = next((r for r in real_rows if r.index == pick), real_rows[0])
            reference_target = next((r for r in reference_rows if r.index == pick), reference_rows[0])

            scheduled = real_target.outstanding
            whole_remainder = real_plan.total_amount - sum(
                (r.amount_paid for r in real_rows), Decimal("0.00")
            )
            amount = rng.choice([
                whole_remainder,  # clears the plan at once: later installments are dropped
                None, None, None,
                (scheduled * Decimal(rng.choice(["0.2", "0.5", "0.9"]))).quantize(Decimal("0.01")),
                scheduled + Decimal(rng.choice(["100.00", "1500.00", "99999.00"])),
                Decimal("0.00"),
            ])

            expected = outcome(lambda: reference_collect(
                actor=manager, branch=branch, installment=reference_target,
                method="cash", amount=amount))
            got = outcome(lambda: services.collect_installment_payment(
                actor=manager, branch=branch, installment=real_target,
                method="cash", amount=amount)[0])

            # The description names the plan's own installment label, which is the same.
            before_rows = len(real_rows)
            assert got == expected, (seed, _step, pick, amount)
            assert snapshot(real_plan) == snapshot(reference_plan), (seed, _step, pick, amount)
            if got[0] == "refused":
                EVENTS[got[1]] += 1
            else:
                EVENTS["paid"] += 1
                if got[1] < scheduled:
                    EVENTS["short"] += 1
                    if real_target.index == real_rows[-1].index:
                        EVENTS["short_on_last"] += 1
                if got[1] > scheduled:
                    EVENTS["over"] += 1
            if Installment.objects.filter(plan=real_plan).count() < before_rows:
                EVENTS["later_installments_dropped"] += 1


    # The comparison has to have actually moved money, not agreed on nothing.
    assert Payment.objects.filter(category=PaymentCategory.INSTALLMENT).count() >= 8


def test_the_random_runs_reached_the_hard_cases():
    """Ran after the seeds above: agreement only means something if they got there."""
    if not EVENTS:
        pytest.skip("run on its own, without the seeded runs")
    for case in ("paid", "short", "short_on_last", "over", "later_installments_dropped",
                 "not_oldest_unpaid", "already_paid", "invalid_amount", "exceeds_outstanding"):
        assert EVENTS[case] > 0, f"the random runs never produced: {case} ({dict(EVENTS)})"
