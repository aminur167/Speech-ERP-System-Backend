"""
"Remaining due" on installment receipts.

The figure is what the plan still owed the moment the payment settled —
stamped on the Payment, so a reprint months later reads the same however many
payments have followed.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.enrollments import services
from apps.services.models import Service

pytestmark = [pytest.mark.django_db, pytest.mark.money]


@pytest.fixture
def plan(manager, branch, patient_factory, service_factory):
    package = service_factory(
        name="Assessment Package", code="INS-1",
        category=Service.Category.INSTALLMENT, fee=Decimal("15000.00"),
    )
    return services.create_installment_plan(
        actor=manager, branch=branch, patient=patient_factory(), service=package,
        number_of_installments=3,
    )


def installment(plan, index):
    return plan.installments.get(index=index)


def pay(manager, branch, plan, index, amount=None, key=None):
    payment, _ = services.collect_installment_payment(
        actor=manager, branch=branch, installment=installment(plan, index),
        method="cash", amount=amount, idempotency_key=key,
    )
    return payment


class TestRemainingDue:
    def test_a_scheduled_payment_leaves_the_rest_of_the_plan(self, manager, branch, plan):
        payment = pay(manager, branch, plan, 1)

        assert payment.due_after == Decimal("10000.00")

    def test_a_short_payment_carries_the_difference_into_it(self, manager, branch, plan):
        payment = pay(manager, branch, plan, 1, amount=Decimal("3000.00"))

        assert payment.due_after == Decimal("12000.00")

    def test_the_last_payment_leaves_nothing(self, manager, branch, plan):
        pay(manager, branch, plan, 1)
        pay(manager, branch, plan, 2)
        last = pay(manager, branch, plan, 3)

        assert last.due_after == Decimal("0.00")

    def test_a_short_last_payment_leaves_its_remainder(self, manager, branch, plan):
        pay(manager, branch, plan, 1)
        pay(manager, branch, plan, 2)
        last = pay(manager, branch, plan, 3, amount=Decimal("2000.00"))

        assert last.due_after == Decimal("3000.00")

    def test_an_earlier_receipt_is_not_rewritten_by_later_payments(
        self, manager, branch, plan
    ):
        first = pay(manager, branch, plan, 1)
        pay(manager, branch, plan, 2)

        first.refresh_from_db()
        assert first.due_after == Decimal("10000.00")

    def test_a_replay_returns_the_original_figure(self, manager, branch, plan):
        first = pay(manager, branch, plan, 1, key="inst-key-1")
        again = pay(manager, branch, plan, 1, key="inst-key-1")

        assert again.pk == first.pk
        assert again.due_after == Decimal("10000.00")

    def test_forgiven_installments_are_not_counted(self, manager, branch, plan):
        pay(manager, branch, plan, 1)
        third = installment(plan, 3)
        third.status = third.Status.CANCELLED
        third.save(update_fields=["status"])

        payment = pay(manager, branch, plan, 2)

        assert payment.due_after == Decimal("0.00")


class TestOnTheReceipt:
    def test_the_pay_endpoint_returns_it(self, manager_client, plan):
        response = manager_client.post(
            reverse(
                "enrollments:installment-plan-pay-installment",
                args=[plan.pk, installment(plan, 1).pk],
            ),
            {"method": "cash"},
            format="json",
        )

        assert response.status_code == 200, response.json()
        assert response.json()["payment"]["dueAfter"] == "10000.00"

    def test_the_transaction_history_carries_it_for_reprints(
        self, manager, branch, manager_client, plan
    ):
        pay(manager, branch, plan, 1)

        rows = manager_client.get(reverse("reporting:transaction-list")).json()["results"]

        assert rows[0]["dueAfter"] == "10000.00"

    def test_other_categories_have_none(self, manager, branch, patient_factory, service_factory):
        monthly = service_factory(fee=Decimal("5000.00"))
        enrollment = services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient_factory(), service=monthly
        )

        payment, _ = services.collect_bill_payment(
            actor=manager, branch=branch, bill=enrollment.bills.first(), method="cash"
        )

        assert payment.due_after is None
