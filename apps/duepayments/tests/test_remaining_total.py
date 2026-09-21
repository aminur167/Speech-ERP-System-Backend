"""
The Due Payments list sums each enrollment's remaining balance in the
database. It must agree with the models' own `outstanding_total()` in every
awkward case: part-paid, forgiven, and paid in full.
"""

from decimal import Decimal

import pytest

from apps.duepayments.services import collect_due_items
from apps.enrollments import services as enrollment_services
from apps.enrollments.models import BillStatus

pytestmark = pytest.mark.django_db


def _row(items, ref_id, kind):
    return next(i for i in items if i["refId"] == str(ref_id) and i["type"] == kind)


def test_monthly_total_matches_the_model(manager, branch, patient_factory, service_factory):
    enrollment = enrollment_services.create_monthly_enrollment(
        actor=manager, branch=branch, patient=patient_factory(), service=service_factory(),
        months_ahead=4,
    )
    bills = list(enrollment.bills.order_by("month"))
    assert len(bills) >= 4
    # Oldest stays unpaid (it is the payable row); then one part-paid, one
    # written off, one cancelled.
    bills[1].amount_paid = Decimal("1234.50")
    bills[1].save(update_fields=["amount_paid"])
    bills[2].status = BillStatus.WRITTEN_OFF
    bills[2].save(update_fields=["status"])
    bills[3].status = BillStatus.CANCELLED
    bills[3].save(update_fields=["status"])

    row = _row(collect_due_items(branch_id=branch.id), enrollment.id, "monthly")

    assert row["outstandingTotal"] == enrollment.outstanding_total()
    assert row["outstandingTotal"] == bills[0].amount + bills[1].amount - Decimal("1234.50")


def test_installment_total_and_parts_match_the_model(
    manager, branch, patient_factory, service_factory
):
    plan = enrollment_services.create_installment_plan(
        actor=manager, branch=branch, patient=patient_factory(),
        service=service_factory(category="installment", fee=Decimal("9000.00")),
        number_of_installments=3,
    )
    first = plan.oldest_unpaid_installment()
    enrollment_services.collect_installment_payment(
        actor=manager, branch=branch, installment=first, method="cash", amount=Decimal("2500.00")
    )
    plan.refresh_from_db()

    row = _row(collect_due_items(branch_id=branch.id), plan.id, "installment")

    assert row["outstandingTotal"] == plan.outstanding_total()
    assert row["installmentsTotal"] == plan.installments.count()


def test_a_fully_paid_history_adds_nothing(manager, branch, patient_factory, service_factory):
    enrollment = enrollment_services.create_monthly_enrollment(
        actor=manager, branch=branch, patient=patient_factory(), service=service_factory(),
        months_ahead=2,
    )
    enrollment_services.collect_bill_payment(
        actor=manager, branch=branch, bill=enrollment.oldest_unpaid_bill(), method="cash"
    )

    items = collect_due_items(branch_id=branch.id)
    rows = [i for i in items if i["refId"] == str(enrollment.id)]

    for row in rows:
        assert row["outstandingTotal"] == enrollment.outstanding_total()
