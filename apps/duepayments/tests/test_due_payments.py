"""
Due payment tests — follows the required-test list in docs/07.

Two properties carry the weight:

  * outstanding sums `amount − amount_paid`, so a part-refunded bill reports
    what is actually still owed rather than the original full amount;
  * historical reconstruction compares against the END of the target day, not
    its start — otherwise a payment taken this afternoon still reads as
    outstanding for today.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.duepayments import services as due_services
from apps.enrollments import services as enrollment_services
from apps.enrollments.models import BillStatus, EnrollmentStatus, MonthlyBill

pytestmark = pytest.mark.django_db


@pytest.fixture
def monthly_service(service_factory):
    return service_factory(name="Individual Therapy", fee=Decimal("5000.00"))


@pytest.fixture
def enrollment(manager, branch, patient_factory, monthly_service):
    return enrollment_services.create_monthly_enrollment(
        actor=manager, branch=branch,
        patient=patient_factory(name="Nusrat Jahan"), service=monthly_service,
    )


class TestDueListing:
    def test_lists_the_currently_payable_bill(self, manager_client, enrollment):
        results = manager_client.get(reverse("duepayments:due-list")).json()["results"]

        assert len(results) == 1
        assert results[0]["type"] == "monthly"
        assert Decimal(results[0]["amount"]) == Decimal("5000.00")

    def test_only_the_oldest_unpaid_appears_per_enrollment(
        self, manager_client, enrollment
    ):
        """
        Three bills exist, but only one is payable under oldest-first. Listing
        the others would offer actions that would be refused.
        """
        assert enrollment.bills.count() == 3
        assert len(manager_client.get(reverse("duepayments:due-list")).json()["results"]) == 1

    def test_paid_enrollment_drops_out(self, manager_client, manager, branch, enrollment):
        enrollment_services.collect_bill_payment(
            actor=manager, branch=branch,
            bill=enrollment.oldest_unpaid_bill(), method="cash",
        )

        results = manager_client.get(reverse("duepayments:due-list")).json()["results"]
        assert len(results) == 1  # the next month is now payable

    def test_terminated_enrollment_is_excluded(self, manager_client, enrollment):
        enrollment.status = EnrollmentStatus.TERMINATED
        enrollment.save(update_fields=["status"])

        assert manager_client.get(reverse("duepayments:due-list")).json()["count"] == 0

    def test_written_off_bill_is_excluded(self, manager_client, enrollment):
        enrollment.bills.update(status=BillStatus.WRITTEN_OFF)
        assert manager_client.get(reverse("duepayments:due-list")).json()["count"] == 0

    def test_filter_by_type(self, manager_client, enrollment):
        assert (
            manager_client.get(reverse("duepayments:due-list"), {"type": "installment"}).json()["count"]
            == 0
        )
        assert (
            manager_client.get(reverse("duepayments:due-list"), {"type": "monthly"}).json()["count"]
            == 1
        )

    def test_search_by_patient_name(self, manager_client, enrollment):
        found = manager_client.get(reverse("duepayments:due-list"), {"search": "nusrat"})
        missing = manager_client.get(reverse("duepayments:due-list"), {"search": "nobody"})

        assert found.json()["count"] == 1
        assert missing.json()["count"] == 0

    def test_overdue_status_is_reported(self, manager_client, enrollment):
        MonthlyBill.objects.filter(enrollment=enrollment).update(
            due_date=timezone.localdate() - timedelta(days=30)
        )

        results = manager_client.get(reverse("duepayments:due-list")).json()["results"]
        assert results[0]["status"] == BillStatus.OVERDUE


@pytest.mark.money
class TestOutstandingTotals:
    def test_summary_totals(self, manager_client, enrollment):
        body = manager_client.get(reverse("duepayments:due-summary")).json()

        assert Decimal(body["monthlyDue"]) == Decimal("5000.00")
        assert Decimal(body["totalDue"]) == Decimal("5000.00")

    def test_partially_settled_bill_reports_only_the_remainder(
        self, manager_client, enrollment
    ):
        """
        The overstatement bug this guards against: summing `amount` would
        report 5,000 owing when only 2,000 is.
        """
        bill = enrollment.oldest_unpaid_bill()
        bill.amount_paid = Decimal("3000.00")
        bill.save(update_fields=["amount_paid"])

        body = manager_client.get(reverse("duepayments:due-summary")).json()
        assert Decimal(body["totalDue"]) == Decimal("2000.00")

    def test_installment_and_monthly_are_reported_separately(
        self, manager_client, manager, branch, patient_factory, service_factory, enrollment
    ):
        from apps.services.models import Service

        enrollment_services.create_installment_plan(
            actor=manager, branch=branch, patient=patient_factory(),
            service=service_factory(
                code="INS-1", category=Service.Category.INSTALLMENT, fee=Decimal("6000.00")
            ),
            number_of_installments=3,
        )

        body = manager_client.get(reverse("duepayments:due-summary")).json()
        assert Decimal(body["monthlyDue"]) == Decimal("5000.00")
        assert Decimal(body["installmentDue"]) == Decimal("2000.00")
        assert Decimal(body["totalDue"]) == Decimal("7000.00")

    def test_empty_branch_returns_zero_not_an_error(self, other_manager_client):
        body = other_manager_client.get(reverse("duepayments:due-summary")).json()
        assert Decimal(body["totalDue"]) == Decimal("0.00")


@pytest.mark.money
class TestHistoricalReconstruction:
    """
    "What was outstanding on 27 August?" is answered from paid_at, not from
    current status.
    """

    def test_a_bill_paid_today_still_counts_as_outstanding_yesterday(
        self, manager, branch, enrollment
    ):
        """
        Backdate the enrollment itself, not just the payment: a bill's row
        can only have been outstanding "yesterday" if it already existed
        then. Without this the enrollment (created moments ago by the
        `enrollment` fixture) genuinely didn't exist as of yesterday, and the
        correct answer would be zero for a reason unrelated to what this test
        is checking.
        """
        from apps.enrollments.models import MonthlyEnrollment

        MonthlyEnrollment.objects.filter(pk=enrollment.pk).update(
            created_at=timezone.now() - timedelta(days=30)
        )

        enrollment_services.collect_bill_payment(
            actor=manager, branch=branch,
            bill=enrollment.oldest_unpaid_bill(), method="cash",
        )

        yesterday = timezone.localdate() - timedelta(days=1)
        historical = due_services.due_summary(branch_id=branch.id, as_of=yesterday)

        assert historical["totalDue"] == Decimal("5000.00")

    def test_a_bill_paid_this_afternoon_is_not_outstanding_today(
        self, manager, branch, enrollment
    ):
        """
        The off-by-one: comparing against the START of the day would still
        report it as owed. Already caught once in the frontend.
        """
        enrollment_services.collect_bill_payment(
            actor=manager, branch=branch,
            bill=enrollment.oldest_unpaid_bill(), method="cash",
        )

        today = due_services.due_summary(branch_id=branch.id, as_of=timezone.localdate())
        current = due_services.due_summary(branch_id=branch.id)

        assert today["totalDue"] == current["totalDue"]

    def test_current_snapshot_and_todays_reconstruction_agree(
        self, branch, enrollment
    ):
        assert (
            due_services.due_summary(branch_id=branch.id)["totalDue"]
            == due_services.due_summary(
                branch_id=branch.id, as_of=timezone.localdate()
            )["totalDue"]
        )

    def test_written_off_bills_never_count_historically(self, branch, enrollment):
        enrollment.bills.update(status=BillStatus.WRITTEN_OFF)

        historical = due_services.due_summary(
            branch_id=branch.id, as_of=timezone.localdate()
        )
        assert historical["totalDue"] == Decimal("0.00")


@pytest.mark.isolation
class TestDueBranchIsolation:
    def test_manager_sees_only_their_branchs_dues(
        self, manager_client, other_manager, other_branch,
        patient_factory, service_factory, enrollment,
    ):
        enrollment_services.create_monthly_enrollment(
            actor=other_manager, branch=other_branch,
            patient=patient_factory(branch=other_branch),
            service=service_factory(code="OTHER-1"),
        )

        results = manager_client.get(reverse("duepayments:due-list")).json()["results"]
        assert len(results) == 1
        assert results[0]["branchId"] == str(enrollment.branch_id)

    def test_manager_summary_excludes_other_branches(
        self, manager_client, other_manager, other_branch,
        patient_factory, service_factory, enrollment,
    ):
        enrollment_services.create_monthly_enrollment(
            actor=other_manager, branch=other_branch,
            patient=patient_factory(branch=other_branch),
            service=service_factory(code="OTHER-2", fee=Decimal("9999.00")),
        )

        body = manager_client.get(reverse("duepayments:due-summary")).json()
        assert Decimal(body["totalDue"]) == Decimal("5000.00")

    def test_admin_sees_all_branches_combined(
        self, admin_client, other_manager, other_branch,
        patient_factory, service_factory, enrollment,
    ):
        enrollment_services.create_monthly_enrollment(
            actor=other_manager, branch=other_branch,
            patient=patient_factory(branch=other_branch),
            service=service_factory(code="OTHER-3", fee=Decimal("3000.00")),
        )

        body = admin_client.get(reverse("duepayments:due-summary")).json()
        assert Decimal(body["totalDue"]) == Decimal("8000.00")

    def test_admin_can_narrow_to_one_branch(self, admin_client, branch, enrollment):
        body = admin_client.get(
            reverse("duepayments:due-summary"), {"branch": str(branch.id)}
        ).json()
        assert Decimal(body["totalDue"]) == Decimal("5000.00")
