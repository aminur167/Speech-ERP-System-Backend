"""
Two people collecting the same bill or installment at the same moment: exactly
one may succeed. Real threads on PostgreSQL, because row locking is the whole
property -- a sequential test cannot see it.

`transaction=True` for the same reason as the receipt-number test in
apps/payments: worker threads open their own connections and can only see
committed rows.
"""

import threading
from decimal import Decimal

import pytest
from django.db import connection

from apps.enrollments import services
from apps.payments.models import Payment
from apps.services.models import Service

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.money, pytest.mark.slow]


def run_together(calls, django_db_blocker):
    """Run each callable in its own thread, started as close together as possible."""
    outcomes, barrier = [], threading.Barrier(len(calls))

    def worker(call):
        try:
            with django_db_blocker.unblock():
                barrier.wait(timeout=10)
                outcomes.append(("ok", call()))
        except services.EnrollmentError as exc:
            outcomes.append(("refused", exc.code))
        except Exception as exc:  # anything else is a failure of the property
            outcomes.append(("error", repr(exc)))
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return outcomes


class TestSameBillAtOnce:
    def test_only_one_collection_succeeds(
        self, manager, branch, patient_factory, service_factory, django_db_blocker
    ):
        enrollment = services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient_factory(),
            service=service_factory(fee=Decimal("5000.00")),
        )
        bill = enrollment.bills.first()

        outcomes = run_together(
            [
                lambda: services.collect_bill_payment_by_id(
                    actor=manager, branch=branch, enrollment=enrollment,
                    bill_id=bill.pk, method="cash",
                )
                for _ in range(4)
            ],
            django_db_blocker,
        )

        assert not [o for o in outcomes if o[0] == "error"], outcomes
        assert [o[0] for o in outcomes].count("ok") == 1, outcomes
        assert {o[1] for o in outcomes if o[0] == "refused"} == {"already_paid"}
        assert Payment.objects.count() == 1


class TestSameInstallmentAtOnce:
    def test_only_one_collection_succeeds_and_the_plan_stays_consistent(
        self, manager, branch, patient_factory, service_factory, django_db_blocker
    ):
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient_factory(),
            service=service_factory(category=Service.Category.INSTALLMENT, fee=Decimal("15000.00")),
            number_of_installments=3,
        )
        first = plan.installments.order_by("index").first()

        outcomes = run_together(
            [
                lambda: services.collect_installment_payment_by_id(
                    actor=manager, branch=branch, plan=plan,
                    installment_id=first.pk, method="cash",
                )
                for _ in range(4)
            ],
            django_db_blocker,
        )

        assert not [o for o in outcomes if o[0] == "error"], outcomes
        assert [o[0] for o in outcomes].count("ok") == 1, outcomes
        assert {o[1] for o in outcomes if o[0] == "refused"} == {"already_paid"}
        assert Payment.objects.count() == 1
        # The plan still owes exactly what it should: 15,000 less one 5,000.
        assert plan.outstanding_total() == Decimal("10000.00")

    def test_two_different_installments_of_one_plan_cannot_both_be_paid_out_of_order(
        self, manager, branch, patient_factory, service_factory, django_db_blocker
    ):
        """The second installment may not be paid while the first is open."""
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient_factory(),
            service=service_factory(category=Service.Category.INSTALLMENT, fee=Decimal("15000.00")),
            number_of_installments=3,
        )
        rows = list(plan.installments.order_by("index"))

        outcomes = run_together(
            [
                lambda: services.collect_installment_payment_by_id(
                    actor=manager, branch=branch, plan=plan,
                    installment_id=rows[0].pk, method="cash",
                ),
                lambda: services.collect_installment_payment_by_id(
                    actor=manager, branch=branch, plan=plan,
                    installment_id=rows[1].pk, method="cash",
                ),
            ],
            django_db_blocker,
        )

        assert not [o for o in outcomes if o[0] == "error"], outcomes
        # Either order of arrival is legal, but never "both paid, second one out of turn":
        # if the later installment went first it was refused; if the earlier went first,
        # the later one may then follow.
        paid = Payment.objects.count()
        assert paid in (1, 2)
        if paid == 1:
            assert any(o == ("refused", "not_oldest_unpaid") for o in outcomes), outcomes
