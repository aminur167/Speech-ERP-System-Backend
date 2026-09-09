"""
Advance payment — paying named future months before they arrive.

Three rules carry the weight:

  * **the manager names the months.** October and December with November
    deliberately left alone is a real choice a patient makes, so this is a set
    of ticks, not a "pay through" range.
  * **arrears first, always.** Nothing may be paid ahead while anything is
    still owed. The September trap below is why: a patient who had just handed
    over three months of cash would otherwise be caught by an open current
    month.
  * **an advance is not a payment for now.** A prepaid month must not appear as
    money owed, must not be collectable twice, and must read as `advance` until
    its month actually arrives.
"""

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.duepayments.services import due_summary
from apps.enrollments import services
from apps.enrollments.models import BillStatus, EnrollmentStatus, due_date_for_month

pytestmark = pytest.mark.django_db


@pytest.fixture
def monthly_service(service_factory):
    return service_factory(name="Individual Therapy", code="ADV-1", fee=Decimal("5000.00"))


@pytest.fixture
def enrollment(manager, branch, patient_factory, monthly_service):
    return services.create_monthly_enrollment(
        actor=manager, branch=branch,
        patient=patient_factory(name="Nusrat Jahan"), service=monthly_service,
    )


@pytest.fixture
def settled(enrollment, settle_dues):
    """An enrollment with nothing owed — the only state advance is allowed in."""
    settle_dues(enrollment.patient)
    return enrollment


def options_url(enrollment):
    return reverse("enrollments:monthly-enrollment-advance-options", args=[enrollment.pk])


def preview_url(enrollment):
    return reverse("enrollments:monthly-enrollment-advance-preview", args=[enrollment.pk])


def pay_advance_url(enrollment):
    return reverse("enrollments:monthly-enrollment-pay-advance", args=[enrollment.pk])


def month_ahead(count):
    return services.month_key(services.add_months(timezone.localdate(), count))


@pytest.mark.money
class TestCollectingAhead:
    def test_only_the_ticked_months_are_created(self, manager_client, settled):
        """
        The heart of it. Tick October and December, and November is not
        created at all — it is not a skipped due, it does not exist yet, and
        the billing job will raise it when it arrives.
        """
        response = manager_client.post(
            pay_advance_url(settled),
            {"months": [month_ahead(1), month_ahead(3)], "method": "cash"},
            format="json",
        )

        assert response.status_code == 200
        months = set(settled.bills.values_list("month", flat=True))
        assert month_ahead(1) in months
        assert month_ahead(3) in months
        assert month_ahead(2) not in months

    def test_each_month_gets_its_own_receipt(self, manager_client, settled):
        response = manager_client.post(
            pay_advance_url(settled),
            {"months": [month_ahead(1), month_ahead(2)], "method": "cash"},
            format="json",
        )

        payments = response.json()["payments"]
        assert len(payments) == 2
        assert len({p["receiptNumber"] for p in payments}) == 2
        assert sum(Decimal(p["amount"]) for p in payments) == Decimal("10000.00")

    def test_the_ticked_months_read_advance(self, manager_client, settled):
        manager_client.post(
            pay_advance_url(settled),
            {"months": [month_ahead(1), month_ahead(2)], "method": "cash"},
            format="json",
        )

        by_month = {b.month: b.effective_status() for b in settled.bills.all()}
        assert by_month[services.month_key(timezone.localdate())] == BillStatus.PAID
        assert by_month[month_ahead(1)] == BillStatus.ADVANCE
        assert by_month[month_ahead(2)] == BillStatus.ADVANCE

    def test_a_prepaid_month_is_not_money_owed(self, manager_client, branch, settled):
        manager_client.post(
            pay_advance_url(settled),
            {"months": [month_ahead(1), month_ahead(2)], "method": "cash"},
            format="json",
        )

        assert Decimal(due_summary(branch_id=branch.id)["totalDue"]) == Decimal("0.00")

    def test_a_prepaid_month_cannot_be_ticked_again(self, manager_client, settled):
        manager_client.post(
            pay_advance_url(settled), {"months": [month_ahead(1)], "method": "cash"},
            format="json",
        )

        again = manager_client.post(
            pay_advance_url(settled), {"months": [month_ahead(1)], "method": "cash"},
            format="json",
        )
        assert again.status_code == 400
        assert again.json()["code"] == "already_paid"

    def test_the_options_list_marks_an_already_paid_month_as_covered(
        self, manager_client, settled
    ):
        """
        Shown, not hidden. "December is already paid" is the answer the
        manager came for; an absent row does not give it.
        """
        manager_client.post(
            pay_advance_url(settled), {"months": [month_ahead(2)], "method": "cash"},
            format="json",
        )

        options = manager_client.get(options_url(settled)).json()["months"]
        covered = {row["month"]: row["covered"] for row in options}
        assert covered[month_ahead(2)] is True
        assert covered[month_ahead(1)] is False


@pytest.mark.money
class TestArrearsComeFirst:
    def test_an_unpaid_month_blocks_paying_ahead(self, manager_client, enrollment):
        """
        Not a preference. Every route into advance goes through here, so a
        patient can never be holding months of credit while an open month
        quietly ages behind it.
        """
        response = manager_client.post(
            pay_advance_url(enrollment),
            {"months": [month_ahead(1)], "method": "cash"},
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "arrears_first"
        assert enrollment.bills.count() == 1  # nothing was created

    def test_the_options_endpoint_says_what_is_blocking(
        self, manager_client, enrollment
    ):
        """The screen has to explain the disabled button, not just show it."""
        body = manager_client.get(options_url(enrollment)).json()

        assert Decimal(body["outstandingTotal"]) == Decimal("5000.00")
        assert len(body["outstandingItems"]) == 1

    def test_clearing_the_arrears_unblocks_it(
        self, manager_client, enrollment, settle_dues
    ):
        settle_dues(enrollment.patient)

        response = manager_client.post(
            pay_advance_url(enrollment),
            {"months": [month_ahead(1)], "method": "cash"},
            format="json",
        )
        assert response.status_code == 200


@pytest.mark.money
class TestTheSeptemberTrap:
    """
    The reason arrears come first.

    `terminate_unpaid_monthly_services` stops a service on any unpaid bill for
    a month that has finished. A patient who has just paid three months ahead
    must not be caught by it because the current month was still open — so the
    current month cannot still be open when the advance is taken.
    """

    def test_paying_ahead_survives_the_nightly_job(
        self, manager_client, manager, enrollment, settle_dues
    ):
        settle_dues(enrollment.patient)
        manager_client.post(
            pay_advance_url(enrollment),
            {"months": [month_ahead(1), month_ahead(2), month_ahead(3)], "method": "cash"},
            format="json",
        )

        next_month_first = services.add_months(timezone.localdate(), 1)
        result = services.terminate_unpaid_monthly_services(
            actor=manager, on=next_month_first
        )

        assert result["terminated"] == 0
        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.ACTIVE


@pytest.mark.money
class TestWhenTheMonthArrives:
    def test_the_generator_creates_no_competing_due(self, manager_client, settled):
        manager_client.post(
            pay_advance_url(settled), {"months": [month_ahead(1)], "method": "cash"},
            format="json",
        )

        before = settled.bills.count()
        services.generate_due_bills(up_to=services.add_months(timezone.localdate(), 1))

        assert settled.bills.count() == before

    def test_a_month_left_unticked_is_billed_when_it_arrives(
        self, manager_client, settled
    ):
        """
        The other half of "only the ticked months". November was skipped, so
        November must be raised as an ordinary due when it comes round — and
        the walk must not sail past it because December already exists.
        """
        manager_client.post(
            pay_advance_url(settled),
            {"months": [month_ahead(1), month_ahead(3)], "method": "cash"},
            format="json",
        )

        services.generate_due_bills(up_to=services.add_months(timezone.localdate(), 2))

        skipped = settled.bills.get(month=month_ahead(2))
        assert skipped.status == BillStatus.DUE
        assert skipped.outstanding == Decimal("5000.00")

    def test_an_arrived_advance_becomes_paid(self, manager_client, settled):
        manager_client.post(
            pay_advance_url(settled), {"months": [month_ahead(1)], "method": "cash"},
            format="json",
        )

        services.generate_due_bills(up_to=services.add_months(timezone.localdate(), 1))

        assert settled.bills.get(month=month_ahead(1)).status == BillStatus.PAID

    def test_it_reads_as_paid_from_the_first_even_with_no_job_run(
        self, manager_client, settled
    ):
        """Derived as well as stored: a missed run is cosmetic, not wrong."""
        manager_client.post(
            pay_advance_url(settled), {"months": [month_ahead(1)], "method": "cash"},
            format="json",
        )

        bill = settled.bills.get(month=month_ahead(1))
        arrived = services.add_months(timezone.localdate(), 1)

        assert bill.status == BillStatus.ADVANCE
        assert bill.effective_status(on=arrived) == BillStatus.PAID

    def test_a_prepaid_month_never_reads_as_overdue(self, manager_client, settled):
        manager_client.post(
            pay_advance_url(settled), {"months": [month_ahead(1)], "method": "cash"},
            format="json",
        )

        bill = settled.bills.get(month=month_ahead(1))
        assert not bill.is_overdue(on=due_date_for_month(month_ahead(1)))


class TestAdvanceRefusals:
    def test_a_month_in_the_past_is_refused(self, manager_client, settled):
        response = manager_client.post(
            pay_advance_url(settled),
            {
                "months": [
                    services.month_key(services.add_months(timezone.localdate(), -1))
                ],
                "method": "cash",
            },
            format="json",
        )
        assert response.status_code == 400
        assert response.json()["code"] == "not_future"

    def test_the_current_month_is_refused(self, manager_client, settled):
        """It is this month's fee, collected on Due Payments — not an advance."""
        response = manager_client.post(
            pay_advance_url(settled),
            {"months": [services.month_key(timezone.localdate())], "method": "cash"},
            format="json",
        )
        assert response.status_code == 400
        assert response.json()["code"] == "not_future"

    def test_beyond_the_cap_is_refused(self, manager_client, settled, settings):
        response = manager_client.post(
            pay_advance_url(settled),
            {"months": [month_ahead(settings.MAX_ADVANCE_MONTHS + 1)], "method": "cash"},
            format="json",
        )
        assert response.status_code == 400
        assert response.json()["code"] == "too_far_ahead"

    def test_an_empty_selection_is_refused(self, manager_client, settled):
        response = manager_client.post(
            pay_advance_url(settled), {"months": [], "method": "cash"}, format="json"
        )
        assert response.status_code == 400

    def test_an_inactive_service_is_refused(self, manager_client, manager, settled):
        services.terminate(actor=manager, container=settled)

        response = manager_client.post(
            pay_advance_url(settled), {"months": [month_ahead(1)], "method": "cash"},
            format="json",
        )
        assert response.status_code == 400
        assert response.json()["code"] == "terminated"

    @pytest.mark.money
    def test_replaying_the_request_charges_once(self, manager_client, settled):
        body = {
            "months": [month_ahead(1), month_ahead(2)],
            "method": "cash",
            "idempotencyKey": "adv-key-1",
        }

        first = manager_client.post(pay_advance_url(settled), body, format="json")
        second = manager_client.post(pay_advance_url(settled), body, format="json")

        assert first.status_code == 200
        assert second.status_code == 200
        assert [p["id"] for p in first.json()["payments"]] == [
            p["id"] for p in second.json()["payments"]
        ]
        assert settled.bills.filter(status=BillStatus.ADVANCE).count() == 2


class TestPreview:
    def test_it_totals_the_ticked_months(self, manager_client, settled):
        body = manager_client.get(
            preview_url(settled), {"months": f"{month_ahead(1)},{month_ahead(3)}"}
        ).json()

        assert [row["month"] for row in body["months"]] == [month_ahead(1), month_ahead(3)]
        assert Decimal(body["total"]) == Decimal("10000.00")

    def test_it_refuses_exactly_what_the_collection_would(self, manager_client, settled):
        body = manager_client.get(
            preview_url(settled), {"months": services.month_key(timezone.localdate())}
        )
        assert body.status_code == 400
        assert body.json()["code"] == "not_future"


class TestAdvanceAccess:
    def test_admin_can_preview_but_not_collect(self, admin_client, settled):
        assert admin_client.get(options_url(settled)).status_code == 200
        assert (
            admin_client.post(
                pay_advance_url(settled), {"months": [month_ahead(1)], "method": "cash"},
                format="json",
            ).status_code
            == 403
        )

    def test_a_manager_cannot_collect_for_another_branch(
        self, other_manager_client, settled
    ):
        response = other_manager_client.post(
            pay_advance_url(settled), {"months": [month_ahead(1)], "method": "cash"},
            format="json",
        )
        assert response.status_code == 404
