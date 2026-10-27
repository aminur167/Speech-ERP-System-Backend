"""
Charging a daily session — and the discount on it.

Why this endpoint exists at all is the first thing these pin. The browser
used to post its own `amount` to `/api/payments/`, so:

- the fee was whatever the client said it was, and
- a discount was given by typing a smaller number, with nothing recording
  that one had been given, how much, or why.

The money came out right as long as the form was honest. What was missing
was the *explanation* — absent from the receipt, the branch report and the
audit trail alike. So the tests here are mostly about what gets written
down, not about arithmetic.

The one rule that is peculiar to daily: a discount may not take the whole
fee. A daily session raises no bill, so a free one would leave no row
anywhere — unlike a fully discounted admission, which its bill still
records as settled for nothing.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.payments.models import Payment, PaymentCategory
from apps.services.models import Service

pytestmark = pytest.mark.django_db

URL = reverse("enrollments:daily-charge")


@pytest.fixture
def daily_service(service_factory):
    return service_factory(
        name="Speech Assessment",
        code="SVC-DAILY-1",
        category=Service.Category.DAILY,
        fee=Decimal("800.00"),
    )


@pytest.fixture
def patient(patient_factory):
    return patient_factory(name="Rakib Hasan")


def payload(patient, service, **overrides):
    data = {"patient": patient.id, "service": service.id, "method": "cash"}
    data.update(overrides)
    return data


@pytest.mark.money
class TestTheFeeComesFromTheCatalogue:
    def test_it_charges_the_package_fee(self, manager_client, patient, daily_service):
        response = manager_client.post(URL, payload(patient, daily_service), format="json")

        assert response.status_code == 201
        body = response.json()
        assert Decimal(body["amount"]) == Decimal("800.00")
        assert body["category"] == PaymentCategory.DAILY
        # The service is named on the receipt, not just its category.
        assert body["description"] == "Speech Assessment"

    def test_an_amount_in_the_request_is_ignored(
        self, manager_client, patient, daily_service
    ):
        """The whole point of the endpoint: there is no field for a price."""
        response = manager_client.post(
            URL, payload(patient, daily_service, amount="1.00"), format="json"
        )

        assert response.status_code == 201
        assert Decimal(response.json()["amount"]) == Decimal("800.00")

    def test_a_monthly_package_cannot_be_charged_here(
        self, manager_client, patient, service_factory
    ):
        monthly = service_factory(
            code="SVC-M-1", category=Service.Category.MONTHLY, fee=Decimal("3000.00")
        )

        response = manager_client.post(URL, payload(patient, monthly), format="json")

        assert response.status_code == 400
        assert response.json()["code"] == "not_daily"
        assert Payment.objects.count() == 0

    def test_a_replay_charges_once(self, manager_client, patient, daily_service):
        body = payload(patient, daily_service, idempotencyKey="daily-1")

        first = manager_client.post(URL, body, format="json")
        second = manager_client.post(URL, body, format="json")

        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert Payment.objects.count() == 1


@pytest.mark.money
class TestTheDailyDiscount:
    def test_it_is_recorded_beside_what_was_charged(
        self, manager_client, patient, daily_service
    ):
        response = manager_client.post(
            URL,
            payload(
                patient, daily_service, discount="200.00", discountReason="Sibling of PT-4"
            ),
            format="json",
        )

        assert response.status_code == 201
        body = response.json()
        assert Decimal(body["amount"]) == Decimal("600.00")
        assert Decimal(body["grossAmount"]) == Decimal("800.00")
        assert Decimal(body["discountAmount"]) == Decimal("200.00")
        assert body["discountReason"] == "Sibling of PT-4"

    def test_no_discount_leaves_the_gross_null(
        self, manager_client, patient, daily_service
    ):
        """`gross_amount` set means "a discount happened here", as on a plan."""
        response = manager_client.post(URL, payload(patient, daily_service), format="json")

        body = response.json()
        assert body["grossAmount"] is None
        assert Decimal(body["discountAmount"]) == Decimal("0.00")
        assert body["discountReason"] == ""

    def test_a_discount_without_a_reason_is_refused(
        self, manager_client, patient, daily_service
    ):
        response = manager_client.post(
            URL, payload(patient, daily_service, discount="200.00"), format="json"
        )

        assert response.status_code == 400
        assert response.json()["code"] == "discount_reason_required"
        assert Payment.objects.count() == 0

    def test_a_negative_discount_is_refused(
        self, manager_client, patient, daily_service
    ):
        response = manager_client.post(
            URL, payload(patient, daily_service, discount="-50.00"), format="json"
        )

        assert response.status_code == 400
        assert Payment.objects.count() == 0

    def test_the_whole_fee_cannot_be_discounted_away(
        self, manager_client, patient, daily_service
    ):
        """
        The rule that is daily's alone. Nothing would record the session: no
        payment (a Payment must be at least 0.01) and no bill to settle for
        nothing. The message names the real ceiling, 799.99.
        """
        response = manager_client.post(
            URL,
            payload(patient, daily_service, discount="800.00", discountReason="Goodwill"),
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "discount_exceeds_fee"
        assert response.json()["sessionFee"] == "799.99"
        assert Payment.objects.count() == 0

    def test_a_discount_down_to_the_last_paisa_is_allowed(
        self, manager_client, patient, daily_service
    ):
        response = manager_client.post(
            URL,
            payload(patient, daily_service, discount="799.99", discountReason="Goodwill"),
            format="json",
        )

        assert response.status_code == 201
        assert Decimal(response.json()["amount"]) == Decimal("0.01")


@pytest.mark.isolation
class TestScope:
    def test_a_patient_from_another_branch_is_not_found(
        self, manager_client, other_branch, patient_factory, daily_service
    ):
        theirs = patient_factory(branch=other_branch)

        response = manager_client.post(URL, payload(theirs, daily_service), format="json")

        assert response.status_code == 404
        assert Payment.objects.count() == 0

    def test_the_payment_is_filed_against_the_managers_own_branch(
        self, manager_client, manager, patient, daily_service
    ):
        response = manager_client.post(URL, payload(patient, daily_service), format="json")

        payment = Payment.objects.get(pk=response.json()["id"])
        assert payment.branch_id == manager.branch_id
        assert payment.collected_by_id == manager.id

    def test_an_admin_cannot_charge(self, admin_client, patient, daily_service):
        """Admin has no branch to file the session under, as everywhere else."""
        response = admin_client.post(URL, payload(patient, daily_service), format="json")

        assert response.status_code == 403
        assert Payment.objects.count() == 0
