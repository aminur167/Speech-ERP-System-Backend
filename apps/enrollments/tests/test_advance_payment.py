"""
Advance payment — paying months before they arrive.

Two rules carry the weight:

  * **arrears first, always.** The collection is a sequential oldest-first
    loop, so an unpaid September is settled before December is touched. The
    September trap below is why: under a skip-ahead design a patient who had
    just handed over three months of cash would be auto-terminated at month
    end because September was still open.
  * **an advance is not a payment for now.** A prepaid month must not appear
    as money owed, must not be collectable twice, and must read as `advance`
    until its month actually arrives.
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


def preview_url(enrollment):
    return reverse("enrollments:monthly-enrollment-advance-preview", args=[enrollment.pk])


def pay_through_url(enrollment):
    return reverse("enrollments:monthly-enrollment-pay-through", args=[enrollment.pk])


def month_ahead(count):
    return services.month_key(services.add_months(timezone.localdate(), count))


@pytest.mark.money
class TestCollectingAhead:
    def test_paying_six_months_creates_the_months_that_did_not_exist(
        self, manager_client, enrollment
    ):
        """An enrollment opens with three months; the rest have to be made."""
        assert enrollment.bills.count() == 3

        body = manager_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(5), "method": "cash"},
            format="json",
        ).json()

        assert len(body["payments"]) == 6
        assert enrollment.bills.count() == 6
        # One receipt per month, so an advance stays auditable month by month.
        assert len({p["receiptNumber"] for p in body["payments"]}) == 6

    def test_the_current_month_reads_paid_and_the_rest_advance(
        self, manager_client, enrollment
    ):
        manager_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(2), "method": "cash"},
            format="json",
        )

        current = services.month_key(timezone.localdate())
        statuses = dict(enrollment.bills.values_list("month", "status"))

        assert statuses[current] == BillStatus.PAID
        assert statuses[month_ahead(1)] == BillStatus.ADVANCE
        assert statuses[month_ahead(2)] == BillStatus.ADVANCE

    def test_a_prepaid_month_is_not_money_owed(self, manager_client, branch, enrollment):
        manager_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(2), "method": "cash"},
            format="json",
        )

        assert due_summary(branch_id=branch.id)["totalDue"] == Decimal("0.00")
        assert (
            manager_client.get(reverse("duepayments:due-list")).json()["count"] == 0
        )

    def test_a_prepaid_month_cannot_be_collected_again(
        self, manager_client, manager, branch, enrollment
    ):
        manager_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(1), "method": "cash"},
            format="json",
        )
        prepaid = enrollment.bills.get(month=month_ahead(1))

        with pytest.raises(services.EnrollmentError) as caught:
            services.collect_bill_payment(
                actor=manager, branch=branch, bill=prepaid, method="cash"
            )

        assert caught.value.code == "already_paid"

    def test_arrears_are_settled_before_the_months_ahead(
        self, manager_client, enrollment
    ):
        """Oldest-first is not bypassed; it is satisfied at every step."""
        start = services.add_months(timezone.localdate().replace(day=1), -1)
        for offset, bill in enumerate(enrollment.bills.order_by("month")):
            month_date = services.add_months(start, offset)
            bill.month = services.month_key(month_date)
            bill.label = services.month_label(month_date)
            bill.due_date = due_date_for_month(bill.month)
            bill.status = BillStatus.DUE if offset < 2 else BillStatus.UPCOMING
            bill.save()

        body = manager_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(1), "method": "cash"},
            format="json",
        ).json()

        # Last month, this month, next month — in that order.
        assert len(body["payments"]) == 3
        assert not enrollment.unpaid_bills().filter(
            month__lte=services.month_key(timezone.localdate())
        ).exists()

    def test_the_preview_names_the_arrears_separately(self, manager_client, enrollment):
        start = services.add_months(timezone.localdate().replace(day=1), -1)
        for offset, bill in enumerate(enrollment.bills.order_by("month")):
            month_date = services.add_months(start, offset)
            bill.month = services.month_key(month_date)
            bill.label = services.month_label(month_date)
            bill.due_date = due_date_for_month(bill.month)
            bill.status = BillStatus.DUE if offset < 2 else BillStatus.UPCOMING
            bill.save()

        body = manager_client.get(
            preview_url(enrollment), {"through": month_ahead(1)}
        ).json()

        assert Decimal(body["total"]) == Decimal("15000.00")
        assert Decimal(body["arrearsTotal"]) == Decimal("5000.00")
        assert body["monthsAhead"] == 2


@pytest.mark.money
class TestTheSeptemberTrap:
    """
    The reason the collection is a loop and not a skip-ahead.

    A patient with an unpaid September pays through December. Under a design
    that let them jump straight to the future months, September would still
    be open when the nightly job ran on 1 October — and the job would
    terminate the service of someone who had just handed over three months
    of cash. The loop makes that impossible by construction.
    """

    def test_paying_ahead_settles_the_open_month_and_survives_the_nightly_job(
        self, manager_client, enrollment
    ):
        # Last month unpaid, this month unpaid, one more ahead.
        start = services.add_months(timezone.localdate().replace(day=1), -1)
        for offset, bill in enumerate(enrollment.bills.order_by("month")):
            month_date = services.add_months(start, offset)
            bill.month = services.month_key(month_date)
            bill.label = services.month_label(month_date)
            bill.due_date = due_date_for_month(bill.month)
            bill.status = BillStatus.DUE if offset < 2 else BillStatus.UPCOMING
            bill.save()

        manager_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(2), "method": "cash"},
            format="json",
        )

        # The job that would have caught the open month, run for real.
        result = services.terminate_unpaid_monthly_services()
        enrollment.refresh_from_db()

        assert result["terminated"] == 0
        assert enrollment.status == EnrollmentStatus.ACTIVE


@pytest.mark.money
class TestWhenTheMonthArrives:
    def test_the_generator_creates_no_competing_due(self, manager_client, enrollment):
        """
        The cursor starts after the highest existing month, so a prepaid
        stretch pushes it past the target and the loop never runs. Pinned
        because it is load-bearing behaviour that exists by accident.
        """
        manager_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(3), "method": "cash"},
            format="json",
        )
        before = enrollment.bills.count()

        result = services.generate_due_bills()

        assert result["created"] == 0
        assert enrollment.bills.count() == before

    def test_an_arrived_advance_becomes_paid(self, manager_client, enrollment):
        manager_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(1), "method": "cash"},
            format="json",
        )
        next_month = enrollment.bills.get(month=month_ahead(1))
        assert next_month.status == BillStatus.ADVANCE

        # The job runs once that month has arrived.
        services.generate_due_bills(
            up_to=services.add_months(timezone.localdate(), 1)
        )

        next_month.refresh_from_db()
        assert next_month.status == BillStatus.PAID

    def test_it_reads_as_paid_from_the_first_even_with_no_job_run(
        self, manager_client, enrollment
    ):
        """Derived as well as stored, so a missed run is cosmetic."""
        manager_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(1), "method": "cash"},
            format="json",
        )
        bill = enrollment.bills.get(month=month_ahead(1))
        arrived = services.add_months(timezone.localdate(), 1)

        assert bill.effective_status() == BillStatus.ADVANCE
        assert bill.effective_status(on=arrived) == BillStatus.PAID

    def test_a_prepaid_month_never_reads_as_overdue(self, manager_client, enrollment):
        """Its due date passes like any other; the money is already in."""
        manager_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(1), "method": "cash"},
            format="json",
        )
        bill = enrollment.bills.get(month=month_ahead(1))
        long_after = services.add_months(timezone.localdate(), 6)

        assert bill.is_overdue(on=long_after) is False
        assert bill.effective_status(on=long_after) == BillStatus.PAID


class TestAdvanceRefusals:
    def test_a_month_in_the_past_is_refused(self, manager_client, enrollment):
        response = manager_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(-2), "method": "cash"},
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "month_in_past"

    def test_beyond_the_cap_is_refused(self, manager_client, enrollment, settings):
        response = manager_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(settings.MAX_ADVANCE_MONTHS + 1), "method": "cash"},
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "too_far_ahead"

    def test_a_terminated_service_is_refused(self, manager_client, manager, enrollment):
        services.terminate(actor=manager, container=enrollment)

        response = manager_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(1), "method": "cash"},
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "terminated"

    def test_replaying_the_request_charges_once(self, manager_client, enrollment):
        """
        The loop mints one payment per month, so one client key cannot cover
        them all — each leg derives its own deterministic key from it.
        """
        from apps.payments.models import Payment

        body = {"throughMonth": month_ahead(2), "method": "cash", "idempotencyKey": "abc-123"}

        first = manager_client.post(pay_through_url(enrollment), body, format="json").json()
        second = manager_client.post(pay_through_url(enrollment), body, format="json").json()

        assert len(first["payments"]) == 3
        assert second["payments"] == [] or [p["id"] for p in second["payments"]] == [
            p["id"] for p in first["payments"]
        ]
        assert Payment.all_objects.filter(idempotency_key__startswith="adv-").count() == 3


@pytest.mark.isolation
class TestAdvanceAccess:
    def test_admin_can_preview_but_not_collect(self, admin_client, enrollment):
        assert admin_client.get(
            preview_url(enrollment), {"through": month_ahead(1)}
        ).status_code == 200
        assert admin_client.post(
            pay_through_url(enrollment),
            {"throughMonth": month_ahead(1), "method": "cash"},
            format="json",
        ).status_code == 403

    def test_a_manager_cannot_collect_for_another_branch(
        self, manager_client, other_manager, other_branch, patient_factory, service_factory
    ):
        theirs = services.create_monthly_enrollment(
            actor=other_manager, branch=other_branch,
            patient=patient_factory(branch=other_branch),
            service=service_factory(code="ADV-OTHER", branch=other_branch),
        )

        assert manager_client.post(
            pay_through_url(theirs),
            {"throughMonth": month_ahead(1), "method": "cash"},
            format="json",
        ).status_code == 404
