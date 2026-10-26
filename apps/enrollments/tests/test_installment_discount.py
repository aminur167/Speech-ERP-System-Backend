"""
Discounting an installment package as the plan is created.

The rule (confirmed 2026-10-26): the discount comes off the package fee
first, and the installments are split from what is left — so the discounted
figure **is** the plan's price, not a credit applied to it later. Everything
downstream already reads `total_amount`, so pinning that one number pins the
collection, the balance and the refund with it.

What the tests here are really guarding:

1. **The installments add up to the discounted total**, not the package fee.
   A plan whose parts sum to more than its price is a patient overcharged in
   instalments, which nothing downstream would notice.
2. **The package fee survives** in `gross_amount` with the reason beside it.
   A plan worth less than its package, with no record of why, is exactly what
   a reconciliation cannot follow a year later.
3. **A discount cannot swallow the plan.** Every installment carries
   MinValueValidator(0.01), so a zero-value plan cannot be written down at
   all; it is refused in words rather than surfacing as a row-level error.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.common.models import AuditLog
from apps.enrollments import services
from apps.enrollments.models import InstallmentPlan
from apps.services.models import Service

pytestmark = pytest.mark.django_db

PLAN_URL = reverse("enrollments:installment-plan-list")


@pytest.fixture
def package(service_factory):
    return service_factory(
        name="Assessment Package", code="PKG-DISC-01",
        category=Service.Category.INSTALLMENT, fee=Decimal("18000.00"),
    )


@pytest.fixture
def patient(patient_factory):
    return patient_factory(name="Nusrat Jahan")


def create_plan(client, patient, package, **extra):
    body = {
        "patient": str(patient.id),
        "service": str(package.id),
        "numberOfInstallments": 3,
        **extra,
    }
    return client.post(PLAN_URL, body, format="json")


class TestDiscountedPlan:
    def test_the_discount_becomes_the_plans_price(
        self, manager_client, patient, package
    ):
        response = create_plan(
            manager_client, patient, package,
            discount="3000.00", discountReason="Long-standing family",
        )

        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["totalAmount"] == "15000.00"
        assert body["grossAmount"] == "18000.00"
        assert body["discountAmount"] == "3000.00"
        assert body["discountReason"] == "Long-standing family"

    def test_the_installments_are_split_from_the_discounted_total(
        self, manager_client, patient, package
    ):
        body = create_plan(
            manager_client, patient, package,
            discount="3000.00", discountReason="Long-standing family",
        ).json()

        amounts = [Decimal(i["amount"]) for i in body["installments"]]
        assert sum(amounts) == Decimal("15000.00")
        assert amounts == [Decimal("5000.00")] * 3

    def test_an_uneven_discounted_total_still_adds_up(
        self, manager_client, patient, package
    ):
        """The remainder rides on the last part, as it does undiscounted."""
        body = create_plan(
            manager_client, patient, package,
            numberOfInstallments=3, discount="1000.00", discountReason="Hardship",
        ).json()

        amounts = [Decimal(i["amount"]) for i in body["installments"]]
        assert sum(amounts) == Decimal("17000.00")
        assert amounts[-1] >= amounts[0]

    def test_no_discount_leaves_the_plan_exactly_as_before(
        self, manager_client, patient, package
    ):
        body = create_plan(manager_client, patient, package).json()

        assert body["totalAmount"] == "18000.00"
        assert body["grossAmount"] is None
        assert body["discountAmount"] == "0.00"
        assert body["discountReason"] == ""

    def test_the_discount_and_reason_are_audited(
        self, manager_client, patient, package
    ):
        body = create_plan(
            manager_client, patient, package,
            discount="3000.00", discountReason="Long-standing family",
        ).json()

        entry = AuditLog.objects.get(
            target_type="InstallmentPlan", target_id=str(body["id"]),
            action=AuditLog.Action.CREATE,
        )
        assert entry.reason == "Long-standing family"
        assert entry.changes["fee"] == "18000.00"
        assert entry.changes["discount"] == "3000.00"
        assert entry.changes["total"] == "15000.00"


class TestDiscountRules:
    def test_a_discount_without_a_reason_is_refused(
        self, manager_client, patient, package
    ):
        response = create_plan(manager_client, patient, package, discount="3000.00")

        assert response.status_code == 400
        assert response.json()["code"] == "discount_reason_required"
        assert not InstallmentPlan.objects.exists()

    def test_a_blank_reason_counts_as_none(self, manager_client, patient, package):
        response = create_plan(
            manager_client, patient, package, discount="3000.00", discountReason="   ",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "discount_reason_required"

    def test_a_discount_above_the_package_fee_is_refused(
        self, manager_client, patient, package
    ):
        response = create_plan(
            manager_client, patient, package, discount="18000.01", discountReason="x",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "discount_exceeds_fee"
        assert not InstallmentPlan.objects.exists()

    def test_a_discount_covering_the_whole_fee_is_refused(
        self, manager_client, patient, package
    ):
        """
        Not an arbitrary limit: an installment of 0 fails its own validator,
        so there is no plan to write. Refused with words rather than left to
        fail on the rows.
        """
        response = create_plan(
            manager_client, patient, package, discount="18000.00", discountReason="Free",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "discount_leaves_nothing"
        assert not InstallmentPlan.objects.exists()

    def test_a_negative_discount_is_refused(self, manager_client, patient, package):
        response = create_plan(
            manager_client, patient, package, discount="-500.00", discountReason="x",
        )

        assert response.status_code == 400
        assert not InstallmentPlan.objects.exists()


class TestDownstreamReadsTheDiscountedTotal:
    def test_the_outstanding_balance_is_the_discounted_total(
        self, manager, branch, patient, package
    ):
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient, service=package,
            number_of_installments=3,
            discount=Decimal("3000.00"), discount_reason="Long-standing family",
        )

        assert plan.outstanding_total() == Decimal("15000.00")

    def test_collecting_every_installment_clears_the_plan(
        self, manager, branch, patient, package
    ):
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient, service=package,
            number_of_installments=3,
            discount=Decimal("3000.00"), discount_reason="Long-standing family",
        )

        collected = Decimal("0.00")
        while plan.oldest_unpaid_installment() is not None:
            payment, _ = services.collect_installment_payment(
                actor=manager, branch=branch,
                installment=plan.oldest_unpaid_installment(), method="cash",
            )
            collected += payment.amount

        # The patient paid the discounted price, not the package fee.
        assert collected == Decimal("15000.00")
        assert plan.outstanding_total() == Decimal("0.00")
