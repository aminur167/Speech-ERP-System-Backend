"""
Stopping one service, deciding each unpaid month separately.

The rule that matters: **waiving needs a written reason**, because the record
exists so Admin can see why the amount owed went down. A write-off with no
justification records that money vanished and nothing else, which is the part
that mattered.
"""

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.models import AuditLog
from apps.enrollments import services
from apps.enrollments.models import BillStatus, EnrollmentStatus, due_date_for_month

pytestmark = pytest.mark.django_db


@pytest.fixture
def monthly_service(service_factory):
    return service_factory(name="Individual Therapy", code="STOP-1", fee=Decimal("5000.00"))


@pytest.fixture
def enrollment(manager, branch, patient_factory, monthly_service):
    return services.create_monthly_enrollment(
        actor=manager, branch=branch,
        patient=patient_factory(name="Nusrat Jahan"), service=monthly_service,
    )


@pytest.fixture
def owing_two_months(enrollment):
    """Two months that have actually fallen due, plus the usual lookahead."""
    start = services.add_months(timezone.localdate().replace(day=1), -1)
    for offset, bill in enumerate(enrollment.bills.order_by("month")):
        month_date = services.add_months(start, offset)
        bill.month = services.month_key(month_date)
        bill.label = services.month_label(month_date)
        bill.due_date = due_date_for_month(bill.month)
        bill.status = BillStatus.DUE if offset < 2 else BillStatus.UPCOMING
        bill.save()
    return enrollment


def preview_url(enrollment):
    return reverse("enrollments:monthly-enrollment-stop-preview", args=[enrollment.pk])


def stop_url(enrollment):
    return reverse("enrollments:monthly-enrollment-stop", args=[enrollment.pk])


def two_owed(enrollment):
    return list(enrollment.unpaid_bills())[:2]


class TestPreview:
    def test_it_separates_what_is_owed_from_what_gets_dropped(
        self, manager_client, owing_two_months
    ):
        body = manager_client.get(preview_url(owing_two_months)).json()

        assert len(body["owed"]) == 2
        assert Decimal(body["owedTotal"]) == Decimal("10000.00")
        # The third bill is a lookahead nobody was ever asked to pay.
        assert len(body["droppedMonths"]) == 1
        assert body["prepaid"] == []


class TestStopping:
    def test_keeping_and_waiving_in_the_same_stop(
        self, manager_client, owing_two_months
    ):
        bills = two_owed(owing_two_months)

        response = manager_client.post(
            stop_url(owing_two_months),
            {
                "decisions": [
                    {"billId": bills[0].pk, "action": "keep"},
                    {"billId": bills[1].pk, "action": "waive", "reason": "Hardship"},
                ]
            },
            format="json",
        )

        assert response.status_code == 200
        owing_two_months.refresh_from_db()
        assert owing_two_months.status == EnrollmentStatus.TERMINATED

        bills[0].refresh_from_db()
        bills[1].refresh_from_db()
        assert bills[0].status == BillStatus.DUE
        assert bills[1].status == BillStatus.WRITTEN_OFF

    def test_the_waived_month_and_its_reason_reach_the_audit_log(
        self, manager_client, owing_two_months
    ):
        bills = two_owed(owing_two_months)

        manager_client.post(
            stop_url(owing_two_months),
            {
                "decisions": [
                    {"billId": bills[0].pk, "action": "keep"},
                    {
                        "billId": bills[1].pk,
                        "action": "waive",
                        "reason": "Family could not pay",
                    },
                ]
            },
            format="json",
        )

        entry = AuditLog.objects.filter(
            target_type="MonthlyEnrollment", action=AuditLog.Action.WRITE_OFF
        ).latest("created_at")

        assert Decimal(entry.changes["writtenOff"]) == Decimal("5000.00")
        assert entry.changes["months"][0]["reason"] == "Family could not pay"
        assert entry.changes["months"][0]["label"] == bills[1].label

    def test_the_kept_total_is_recorded_too(self, manager_client, owing_two_months):
        """A partial write-off has to be as legible as a wholesale one."""
        bills = two_owed(owing_two_months)

        manager_client.post(
            stop_url(owing_two_months),
            {
                "decisions": [
                    {"billId": bills[0].pk, "action": "keep"},
                    {"billId": bills[1].pk, "action": "waive", "reason": "Hardship"},
                ]
            },
            format="json",
        )

        entry = AuditLog.objects.filter(
            target_type="MonthlyEnrollment", action=AuditLog.Action.TERMINATE
        ).latest("created_at")

        assert Decimal(entry.changes["keptDue"]) == Decimal("5000.00")
        assert entry.changes["keptMonths"] == [bills[0].label]

    def test_waiving_without_a_reason_is_refused(self, manager_client, owing_two_months):
        """
        The whole point of recording a waiver is that Admin can see *why* the
        amount owed dropped. A blank reason records that it dropped and
        nothing else.
        """
        bills = two_owed(owing_two_months)

        response = manager_client.post(
            stop_url(owing_two_months),
            {
                "decisions": [
                    {"billId": bills[0].pk, "action": "keep"},
                    {"billId": bills[1].pk, "action": "waive", "reason": "   "},
                ]
            },
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "waive_reason_required"
        owing_two_months.refresh_from_db()
        assert owing_two_months.status == EnrollmentStatus.ACTIVE

    def test_a_missing_decision_is_refused_and_names_the_month(
        self, manager_client, owing_two_months
    ):
        bills = two_owed(owing_two_months)

        response = manager_client.post(
            stop_url(owing_two_months),
            {"decisions": [{"billId": bills[0].pk, "action": "keep"}]},
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "decisions_required"
        assert bills[1].label in response.json()["months"]

    def test_a_bill_from_another_service_is_refused(
        self, manager_client, manager, branch, patient_factory, monthly_service,
        owing_two_months,
    ):
        other = services.create_monthly_enrollment(
            actor=manager, branch=branch,
            patient=patient_factory(name="Someone Else"), service=monthly_service,
        )
        theirs = other.oldest_unpaid_bill()
        mine = two_owed(owing_two_months)

        response = manager_client.post(
            stop_url(owing_two_months),
            {
                "decisions": [
                    {"billId": mine[0].pk, "action": "keep"},
                    {"billId": mine[1].pk, "action": "keep"},
                    {"billId": theirs.pk, "action": "waive", "reason": "x"},
                ]
            },
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "unknown_bill"

    def test_the_never_payable_lookahead_is_dropped(
        self, manager_client, owing_two_months
    ):
        bills = two_owed(owing_two_months)

        manager_client.post(
            stop_url(owing_two_months),
            {"decisions": [{"billId": bill.pk, "action": "keep"} for bill in bills]},
            format="json",
        )

        current = services.month_key(timezone.localdate())
        assert not owing_two_months.bills.filter(month__gt=current).exists()

    def test_a_kept_due_is_recoverable_by_resuming(
        self, manager_client, manager, owing_two_months
    ):
        """
        The note's rule: come back, clear the due, then the service runs again.
        Kept months land on the Terminated Services screen and are collected
        by the existing resume flow — no new machinery.
        """
        bills = two_owed(owing_two_months)
        manager_client.post(
            stop_url(owing_two_months),
            {
                "decisions": [
                    {"billId": bills[0].pk, "action": "keep"},
                    {"billId": bills[1].pk, "action": "waive", "reason": "Hardship"},
                ]
            },
            format="json",
        )
        owing_two_months.refresh_from_db()

        listed = manager_client.get(
            reverse("enrollments:monthly-enrollment-terminated")
        ).json()["results"][0]
        assert Decimal(listed["previousDue"]) == Decimal("5000.00")

        _, payments = services.resume_monthly_service(
            actor=manager, enrollment=owing_two_months, carry_due=True, method="cash"
        )
        assert [payment.amount for payment in payments] == [Decimal("5000.00")]

    def test_stopping_a_service_that_is_not_running_is_refused(
        self, manager_client, manager, owing_two_months
    ):
        services.terminate(actor=manager, container=owing_two_months)

        response = manager_client.post(
            stop_url(owing_two_months), {"decisions": []}, format="json"
        )

        assert response.status_code == 400
        assert response.json()["code"] == "not_active"


@pytest.mark.isolation
class TestStopAccess:
    def test_admin_cannot_stop_a_branchs_service(self, admin_client, owing_two_months):
        """Stopping a service is a branch-desk action, like collecting."""
        assert (
            admin_client.post(
                stop_url(owing_two_months), {"decisions": []}, format="json"
            ).status_code
            == 403
        )

    def test_admin_can_read_the_preview(self, admin_client, owing_two_months):
        assert admin_client.get(preview_url(owing_two_months)).status_code == 200

    def test_a_manager_cannot_stop_another_branchs_service(
        self, manager_client, other_manager, other_branch, patient_factory, service_factory
    ):
        theirs = services.create_monthly_enrollment(
            actor=other_manager, branch=other_branch,
            patient=patient_factory(branch=other_branch),
            service=service_factory(code="STOP-OTHER", branch=other_branch),
        )

        assert (
            manager_client.post(
                stop_url(theirs), {"decisions": []}, format="json"
            ).status_code
            == 404
        )
