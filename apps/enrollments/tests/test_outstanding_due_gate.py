"""
The outstanding-due gate, and making one service inactive without touching the
others.

Two rules, and both have to hold on the server rather than only on the screen:

  * **a patient who owes anything cannot start a new service or have one
    reactivated.** The screen explains it; the service layer enforces it, so
    calling the API directly with the screen bypassed is refused on exactly
    the same terms as the button.
  * **inactivation is per service.** Stopping monthly therapy leaves an
    installment package running, and never deactivates the patient.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.common.models import AuditLog
from apps.enrollments import services
from apps.enrollments.models import BillStatus, EnrollmentStatus
from apps.services.models import Service

pytestmark = pytest.mark.django_db


@pytest.fixture
def monthly_service(service_factory):
    return service_factory(name="Individual Therapy", code="GATE-M", fee=Decimal("5000.00"))


@pytest.fixture
def other_monthly_service(service_factory):
    return service_factory(name="Group Therapy", code="GATE-M2", fee=Decimal("4000.00"))


@pytest.fixture
def installment_service(service_factory):
    return service_factory(
        name="Assessment Package", code="GATE-I",
        category=Service.Category.INSTALLMENT, fee=Decimal("18000.00"),
    )


@pytest.fixture
def patient(patient_factory):
    return patient_factory(name="Nusrat Jahan")


@pytest.fixture
def owing(manager, branch, patient, monthly_service):
    """A patient with this month's fee unpaid."""
    return services.create_monthly_enrollment(
        actor=manager, branch=branch, patient=patient, service=monthly_service
    )


def outstanding_url(patient):
    return reverse("patients:patient-outstanding-dues", args=[patient.pk])


class TestTheGateOnNewEnrollment:
    def test_a_second_monthly_service_is_refused_while_the_first_is_unpaid(
        self, manager, branch, owing, patient, other_monthly_service
    ):
        with pytest.raises(services.EnrollmentError) as caught:
            services.create_monthly_enrollment(
                actor=manager, branch=branch, patient=patient,
                service=other_monthly_service,
            )

        assert caught.value.code == "outstanding_dues"
        assert caught.value.extra["total"] == "5000.00"

    def test_an_installment_plan_is_refused_too(
        self, manager, branch, owing, patient, installment_service
    ):
        """Both kinds of enrollment go through the same gate."""
        with pytest.raises(services.EnrollmentError) as caught:
            services.create_installment_plan(
                actor=manager, branch=branch, patient=patient,
                service=installment_service, number_of_installments=3,
            )

        assert caught.value.code == "outstanding_dues"

    def test_clearing_the_due_allows_it(
        self, manager, branch, owing, patient, other_monthly_service, settle_dues
    ):
        settle_dues(patient)

        second = services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient,
            service=other_monthly_service,
        )

        assert second.is_active
        assert patient.monthly_enrollments.count() == 2

    def test_the_api_refuses_it_as_well_as_the_screen(
        self, manager_client, owing, patient, other_monthly_service
    ):
        """
        The rule cannot live in the UI. A request straight to the endpoint has
        to be turned away with the same explanation.
        """
        response = manager_client.post(
            reverse("enrollments:monthly-enrollment-list"),
            {"patient": patient.pk, "service": other_monthly_service.pk},
            format="json",
        )

        assert response.status_code == 400
        body = response.json()
        assert body["code"] == "outstanding_dues"
        assert body["total"] == "5000.00"
        assert len(body["items"]) == 1


class TestOutstandingDuesEndpoint:
    def test_it_names_the_months_and_the_total(self, manager_client, owing, patient):
        body = manager_client.get(outstanding_url(patient)).json()

        assert Decimal(body["total"]) == Decimal("5000.00")
        assert body["items"][0]["serviceName"] == "Individual Therapy"
        assert body["items"][0]["type"] == "monthly"

    def test_a_settled_patient_owes_nothing(
        self, manager_client, owing, patient, settle_dues
    ):
        settle_dues(patient)

        body = manager_client.get(outstanding_url(patient)).json()

        assert Decimal(body["total"]) == Decimal("0.00")
        assert body["items"] == []

    def test_a_due_kept_on_an_inactive_service_still_counts(
        self, manager_client, manager, owing, patient
    ):
        """
        The point of the gate. A kept month has to keep blocking, or a patient
        walks away from it and re-enrols the next day as if it never existed.
        """
        bill = owing.oldest_unpaid_bill()
        services.stop_monthly_service(
            actor=manager, enrollment=owing,
            decisions={bill.pk: {"action": "keep"}},
        )

        body = manager_client.get(outstanding_url(patient)).json()

        assert Decimal(body["total"]) == Decimal("5000.00")
        assert body["items"][0]["serviceActive"] is False

    def test_a_cancelled_month_does_not_count(
        self, manager_client, manager, owing, patient
    ):
        bill = owing.oldest_unpaid_bill()
        services.stop_monthly_service(
            actor=manager, enrollment=owing,
            decisions={bill.pk: {"action": "waive", "reason": "Hardship"}},
        )

        body = manager_client.get(outstanding_url(patient)).json()

        assert Decimal(body["total"]) == Decimal("0.00")


class TestInactivationIsPerService:
    def test_stopping_one_leaves_the_others_running(
        self, manager, branch, owing, patient, other_monthly_service,
        installment_service, settle_dues,
    ):
        settle_dues(patient)
        second = services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient,
            service=other_monthly_service,
        )
        settle_dues(patient)
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=3,
        )

        services.stop_monthly_service(actor=manager, enrollment=owing, decisions={})

        owing.refresh_from_db()
        second.refresh_from_db()
        plan.refresh_from_db()
        assert owing.status == EnrollmentStatus.TERMINATED
        assert second.status == EnrollmentStatus.ACTIVE
        assert plan.status == EnrollmentStatus.ACTIVE

    def test_the_patient_record_is_untouched(self, manager, owing, patient):
        """
        Inactivation is service-level. Deactivating the person as well would
        create a second, competing notion of "inactive" beside the per-service
        one the screens actually use.
        """
        before = patient.status
        bill = owing.oldest_unpaid_bill()
        services.stop_monthly_service(
            actor=manager, enrollment=owing,
            decisions={bill.pk: {"action": "keep"}},
        )

        patient.refresh_from_db()
        assert patient.status == before

    def test_an_inactive_service_stops_being_billed(self, manager, owing):
        from django.utils import timezone

        bill = owing.oldest_unpaid_bill()
        services.stop_monthly_service(
            actor=manager, enrollment=owing,
            decisions={bill.pk: {"action": "keep"}},
        )

        before = owing.bills.count()
        services.generate_due_bills(
            up_to=services.add_months(timezone.localdate(), 2)
        )

        owing.refresh_from_db()
        assert owing.bills.count() == before


class TestInactivatingAnInstallmentService:
    @pytest.fixture
    def plan(self, manager, branch, patient, installment_service):
        return services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=3,
        )

    def stop_url(self, plan):
        return reverse("enrollments:installment-plan-stop", args=[plan.pk])

    def test_the_preview_lists_every_unpaid_part(self, manager_client, plan):
        body = manager_client.get(
            reverse("enrollments:installment-plan-stop-preview", args=[plan.pk])
        ).json()

        assert len(body["owed"]) == 3
        assert Decimal(body["owedTotal"]) == Decimal("18000.00")

    def test_keeping_and_cancelling_in_the_same_action(self, manager_client, plan):
        parts = list(plan.installments.order_by("index"))

        response = manager_client.post(
            self.stop_url(plan),
            {
                "decisions": [
                    {"billId": parts[0].pk, "action": "keep"},
                    {"billId": parts[1].pk, "action": "waive", "reason": "Hardship"},
                    {"billId": parts[2].pk, "action": "waive", "reason": "Hardship"},
                ]
            },
            format="json",
        )

        assert response.status_code == 200
        plan.refresh_from_db()
        assert plan.status == EnrollmentStatus.TERMINATED

        for part in parts:
            part.refresh_from_db()
        assert parts[0].status == BillStatus.DUE
        assert parts[1].status == BillStatus.CANCELLED
        assert parts[2].status == BillStatus.CANCELLED

    def test_cancelling_without_a_reason_is_refused(self, manager_client, plan):
        parts = list(plan.installments.order_by("index"))

        response = manager_client.post(
            self.stop_url(plan),
            {"decisions": [{"billId": part.pk, "action": "waive"} for part in parts]},
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "waive_reason_required"

    def test_a_missing_decision_is_refused(self, manager_client, plan):
        first = plan.installments.order_by("index").first()

        response = manager_client.post(
            self.stop_url(plan),
            {"decisions": [{"billId": first.pk, "action": "keep"}]},
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "decisions_required"

    def test_the_cancellation_and_its_reason_reach_the_audit_log(
        self, manager_client, plan
    ):
        parts = list(plan.installments.order_by("index"))
        manager_client.post(
            self.stop_url(plan),
            {
                "decisions": [
                    {"billId": parts[0].pk, "action": "keep"},
                    {"billId": parts[1].pk, "action": "waive", "reason": "Moved away"},
                    {"billId": parts[2].pk, "action": "waive", "reason": "Moved away"},
                ]
            },
            format="json",
        )

        entry = AuditLog.objects.filter(
            target_type="InstallmentPlan", action=AuditLog.Action.WRITE_OFF
        ).latest("created_at")

        assert entry.changes["kind"] == BillStatus.CANCELLED
        assert entry.changes["months"][0]["reason"] == "Moved away"

    def test_reactivating_is_refused_while_the_kept_part_is_owed(
        self, manager_client, plan
    ):
        parts = list(plan.installments.order_by("index"))
        manager_client.post(
            self.stop_url(plan),
            {
                "decisions": [
                    {"billId": parts[0].pk, "action": "keep"},
                    {"billId": parts[1].pk, "action": "waive", "reason": "Hardship"},
                    {"billId": parts[2].pk, "action": "waive", "reason": "Hardship"},
                ]
            },
            format="json",
        )

        response = manager_client.post(
            reverse("enrollments:installment-plan-resume", args=[plan.pk]), {}
        )

        assert response.status_code == 400
        assert response.json()["code"] == "outstanding_dues"

    def test_reactivating_succeeds_once_it_is_cleared(
        self, manager_client, plan, patient, settle_dues
    ):
        parts = list(plan.installments.order_by("index"))
        manager_client.post(
            self.stop_url(plan),
            {
                "decisions": [
                    {"billId": parts[0].pk, "action": "keep"},
                    {"billId": parts[1].pk, "action": "waive", "reason": "Hardship"},
                    {"billId": parts[2].pk, "action": "waive", "reason": "Hardship"},
                ]
            },
            format="json",
        )
        settle_dues(patient)

        response = manager_client.post(
            reverse("enrollments:installment-plan-resume", args=[plan.pk]), {}
        )

        assert response.status_code == 200
        assert response.json()["status"] == EnrollmentStatus.ACTIVE


class TestDuePaymentSearchHandles:
    def test_search_by_service_id(self, manager_client, owing, monthly_service):
        """
        A manager working from another screen has the service id in front of
        them, not the name.
        """
        results = manager_client.get(
            reverse("duepayments:due-list"), {"search": str(monthly_service.pk)}
        ).json()["results"]

        assert len(results) == 1
        assert results[0]["serviceId"] == str(monthly_service.pk)

    def test_search_by_patient_id(self, manager_client, owing, patient):
        results = manager_client.get(
            reverse("duepayments:due-list"), {"search": str(patient.pk)}
        ).json()["results"]

        assert len(results) == 1

    def test_an_id_that_matches_nobody_returns_nothing(self, manager_client, owing):
        body = manager_client.get(
            reverse("duepayments:due-list"), {"search": "99999999"}
        ).json()

        assert body["count"] == 0
