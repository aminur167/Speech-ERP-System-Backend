"""
Enrollment tests — follows the required-test list in docs/05.

Weight sits on three rules:

  * oldest-unpaid-first, so old debt can't age while the current month gets paid;
  * termination blocked while anything is outstanding, so a patient can't walk
    away from a bill by asking to stop;
  * the billing job being idempotent and catch-up capable, because a silently
    dead or double-firing job either stops invoicing or double-bills.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.enrollments import services
from apps.enrollments.models import (
    BillStatus,
    EnrollmentStatus,
    Installment,
    MonthlyBill,
    MonthlyEnrollment,
)
from apps.common.models import AuditLog
from apps.payments.models import Payment, PaymentCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient(patient_factory):
    return patient_factory(name="Nusrat Jahan")


@pytest.fixture
def monthly_service(service_factory):
    return service_factory(name="Individual Therapy", code="MON-INDIV", fee=Decimal("5000.00"))


@pytest.fixture
def installment_service(service_factory):
    from apps.services.models import Service

    return service_factory(
        name="Assessment", code="PKG-ASM-01",
        category=Service.Category.INSTALLMENT, fee=Decimal("18500.00"),
    )


@pytest.fixture
def enrollment(manager, branch, patient, monthly_service):
    return services.create_monthly_enrollment(
        actor=manager, branch=branch, patient=patient, service=monthly_service
    )


class TestMonthlyEnrollmentCreation:
    def test_creates_three_bills(self, enrollment):
        assert enrollment.bills.count() == 3

    def test_only_the_first_bill_is_due(self, enrollment):
        statuses = list(enrollment.bills.order_by("month").values_list("status", flat=True))
        assert statuses == [BillStatus.DUE, BillStatus.UPCOMING, BillStatus.UPCOMING]

    @pytest.mark.money
    def test_bill_amount_equals_the_service_fee_exactly(self, enrollment, monthly_service):
        for bill in enrollment.bills.all():
            assert bill.amount == monthly_service.fee

    def test_due_date_is_the_fifth_of_each_bills_own_month(self, enrollment):
        for bill in enrollment.bills.all():
            assert bill.due_date.day == 5
            assert f"{bill.due_date.year}-{bill.due_date.month:02d}" == bill.month

    def test_months_are_correct_across_a_year_boundary(
        self, manager, branch, patient, monthly_service, settings
    ):
        """A December enrollment must roll into January and February of next year."""
        from unittest.mock import patch

        with patch("apps.enrollments.services.timezone") as mocked:
            mocked.localdate.return_value = date(2026, 12, 15)
            mocked.now.return_value = timezone.now()
            created = services.create_monthly_enrollment(
                actor=manager, branch=branch, patient=patient, service=monthly_service
            )

        months = list(created.bills.order_by("month").values_list("month", flat=True))
        assert months == ["2026-12", "2027-01", "2027-02"]

    def test_enrollment_uses_the_managers_branch(self, enrollment, manager):
        assert enrollment.branch_id == manager.branch_id

    def test_api_rejects_an_inactive_service(
        self, manager_client, patient, monthly_service
    ):
        monthly_service.is_active = False
        monthly_service.save(update_fields=["is_active"])

        response = manager_client.post(
            reverse("enrollments:monthly-enrollment-list"),
            {"patient": str(patient.id), "service": str(monthly_service.id)},
        )
        assert response.status_code == 404

    def test_admin_cannot_enroll(self, admin_client, patient, monthly_service):
        response = admin_client.post(
            reverse("enrollments:monthly-enrollment-list"),
            {"patient": str(patient.id), "service": str(monthly_service.id)},
        )
        assert response.status_code == 403


@pytest.mark.money
class TestInstallmentPlanCreation:
    def test_parts_sum_to_exactly_the_total(self, manager, branch, patient, installment_service):
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=3,
        )

        total = sum(i.amount for i in plan.installments.all())
        assert total == installment_service.fee == Decimal("18500.00")

    def test_remainder_lands_on_the_final_installment(
        self, manager, branch, patient, installment_service
    ):
        """
        Regression guard: with default rounding the base rounds up and the
        final part ends up smaller, charging the patient more up front.
        """
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=3,
        )

        amounts = list(plan.installments.order_by("index").values_list("amount", flat=True))
        assert amounts[0] == amounts[1]
        assert amounts[-1] >= amounts[0]

    def test_first_installment_is_due_rest_upcoming(
        self, manager, branch, patient, installment_service
    ):
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=3,
        )

        statuses = list(plan.installments.order_by("index").values_list("status", flat=True))
        assert statuses == [BillStatus.DUE, BillStatus.UPCOMING, BillStatus.UPCOMING]

    def test_fewer_than_two_installments_rejected(
        self, manager, branch, patient, installment_service
    ):
        with pytest.raises(services.EnrollmentError):
            services.create_installment_plan(
                actor=manager, branch=branch, patient=patient,
                service=installment_service, number_of_installments=1,
            )


class TestCollectionIsAtomic:
    def test_payment_and_settlement_happen_together(self, manager, branch, enrollment):
        bill = enrollment.oldest_unpaid_bill()

        payment, bill = services.collect_bill_payment(
            actor=manager, branch=branch, bill=bill, method="cash"
        )

        assert payment.category == PaymentCategory.MONTHLY
        assert bill.status == BillStatus.PAID
        assert bill.amount_paid == bill.amount
        assert bill.paid_at is not None
        assert bill.payment_id == payment.id

    def test_paying_promotes_the_next_bill_to_due(self, manager, branch, enrollment):
        services.collect_bill_payment(
            actor=manager, branch=branch, bill=enrollment.oldest_unpaid_bill(), method="cash"
        )

        statuses = list(enrollment.bills.order_by("month").values_list("status", flat=True))
        assert statuses == [BillStatus.PAID, BillStatus.DUE, BillStatus.UPCOMING]

    def test_cannot_pay_the_same_bill_twice(self, manager, branch, enrollment):
        bill = enrollment.oldest_unpaid_bill()
        services.collect_bill_payment(actor=manager, branch=branch, bill=bill, method="cash")

        with pytest.raises(services.EnrollmentError) as exc:
            services.collect_bill_payment(actor=manager, branch=branch, bill=bill, method="cash")
        assert exc.value.code == "already_paid"

    def test_replayed_idempotency_key_returns_the_original_payment(
        self, manager, branch, enrollment
    ):
        """
        Regression test: the already-settled guard used to run before the
        idempotency-key lookup, so a genuine replay (network retry, an
        offline-queue flushing) was rejected with "already_paid" instead of
        getting back the payment its first attempt actually produced.
        """
        bill = enrollment.oldest_unpaid_bill()

        first_payment, _ = services.collect_bill_payment(
            actor=manager, branch=branch, bill=bill, method="cash", idempotency_key="k1"
        )
        payment_count = Payment.objects.count()

        # The bill is now settled -- a naive replay would trip the
        # already-settled guard if idempotency weren't checked first.
        second_payment, _ = services.collect_bill_payment(
            actor=manager, branch=branch, bill=bill, method="cash", idempotency_key="k1"
        )

        assert second_payment.pk == first_payment.pk
        assert Payment.objects.count() == payment_count  # no double charge

        bill.refresh_from_db()
        assert bill.status == BillStatus.PAID
        assert bill.amount_paid == bill.amount

    def test_cannot_collect_on_a_terminated_enrollment(self, manager, branch, enrollment):
        bill = enrollment.oldest_unpaid_bill()
        enrollment.status = EnrollmentStatus.TERMINATED
        enrollment.save(update_fields=["status"])

        with pytest.raises(services.EnrollmentError) as exc:
            services.collect_bill_payment(actor=manager, branch=branch, bill=bill, method="cash")
        assert exc.value.code == "terminated"

    def test_replayed_idempotency_key_on_an_installment_returns_the_original_payment(
        self, manager, branch, patient, installment_service
    ):
        """Same regression as the monthly-bill version above, for collect_installment_payment."""
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=3,
        )
        installment = plan.oldest_unpaid_installment()

        first_payment, _ = services.collect_installment_payment(
            actor=manager, branch=branch, installment=installment, method="cash", idempotency_key="k1"
        )
        payment_count = Payment.objects.count()

        second_payment, _ = services.collect_installment_payment(
            actor=manager, branch=branch, installment=installment, method="cash", idempotency_key="k1"
        )

        assert second_payment.pk == first_payment.pk
        assert Payment.objects.count() == payment_count


class TestOldestFirst:
    """
    Without this a patient keeps paying the current month while an old debt
    ages forever, and Outstanding Due stops describing anything actionable.
    """

    def test_cannot_pay_a_later_bill_first(self, manager, branch, enrollment):
        bills = list(enrollment.bills.order_by("month"))

        with pytest.raises(services.EnrollmentError) as exc:
            services.collect_bill_payment(
                actor=manager, branch=branch, bill=bills[1], method="cash"
            )
        assert exc.value.code == "not_oldest_unpaid"

    def test_the_error_names_what_must_be_paid_first(self, manager, branch, enrollment):
        """The manager is facing a patient and needs to know what to collect."""
        bills = list(enrollment.bills.order_by("month"))

        with pytest.raises(services.EnrollmentError) as exc:
            services.collect_bill_payment(
                actor=manager, branch=branch, bill=bills[1], method="cash"
            )

        assert bills[0].label in exc.value.message
        assert exc.value.extra["oldestLabel"] == bills[0].label

    def test_paying_the_oldest_succeeds(self, manager, branch, enrollment):
        bills = list(enrollment.bills.order_by("month"))
        payment, _ = services.collect_bill_payment(
            actor=manager, branch=branch, bill=bills[0], method="cash"
        )
        assert payment is not None

    def test_the_next_becomes_payable_after_the_oldest_clears(
        self, manager, branch, enrollment
    ):
        bills = list(enrollment.bills.order_by("month"))
        services.collect_bill_payment(actor=manager, branch=branch, bill=bills[0], method="cash")

        payment, _ = services.collect_bill_payment(
            actor=manager, branch=branch, bill=bills[1], method="cash"
        )
        assert payment is not None

    def test_no_payment_row_is_created_by_a_rejected_attempt(
        self, manager, branch, enrollment
    ):
        before = Payment.objects.count()
        bills = list(enrollment.bills.order_by("month"))

        with pytest.raises(services.EnrollmentError):
            services.collect_bill_payment(
                actor=manager, branch=branch, bill=bills[2], method="cash"
            )

        assert Payment.objects.count() == before

    def test_ordering_applies_to_installments_too(
        self, manager, branch, patient, installment_service
    ):
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=3,
        )
        parts = list(plan.installments.order_by("index"))

        with pytest.raises(services.EnrollmentError) as exc:
            services.collect_installment_payment(
                actor=manager, branch=branch, installment=parts[1], method="cash"
            )
        assert exc.value.code == "not_oldest_unpaid"

    def test_an_overdue_bill_is_still_payable_when_it_is_the_oldest(
        self, manager, branch, enrollment
    ):
        """Overdue status must not block the very payment that clears it."""
        bill = enrollment.oldest_unpaid_bill()
        MonthlyBill.objects.filter(pk=bill.pk).update(
            due_date=timezone.localdate() - timedelta(days=30)
        )
        bill.refresh_from_db()

        assert bill.is_overdue()
        payment, _ = services.collect_bill_payment(
            actor=manager, branch=branch, bill=bill, method="cash"
        )
        assert payment is not None


class TestTermination:
    def test_succeeds_while_a_bill_is_unpaid(self, manager, enrollment):
        """
        A manager has to be able to close a plan for someone who stopped
        coming. It used to be refused outright, which left the row in Due
        Payments with nothing that could clear it.
        """
        services.terminate(actor=manager, container=enrollment)

        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.TERMINATED

    def test_the_unpaid_balance_is_written_off_not_dropped(self, manager, enrollment):
        services.terminate(actor=manager, container=enrollment)

        statuses = set(enrollment.bills.values_list("status", flat=True))
        assert statuses == {BillStatus.WRITTEN_OFF}

    def test_the_write_off_is_audited_with_the_amount(self, manager, enrollment):
        """Forgiving money silently is the thing the old block existed to prevent."""
        services.terminate(actor=manager, container=enrollment)

        entry = AuditLog.objects.filter(
            action=AuditLog.Action.WRITE_OFF, target_type="MonthlyEnrollment"
        ).first()
        assert entry is not None
        assert entry.changes["writtenOff"] == "15000.00"  # 3 × 5,000

    def test_a_terminated_plans_balance_leaves_outstanding_due(
        self, manager, branch, enrollment
    ):
        from apps.duepayments.services import due_summary

        assert Decimal(due_summary(branch_id=branch.id)["totalDue"]) > 0

        services.terminate(actor=manager, container=enrollment)

        assert Decimal(due_summary(branch_id=branch.id)["totalDue"]) == Decimal("0.00")

    def test_an_overdue_bill_does_not_block_it_either(self, manager, branch, enrollment):
        MonthlyBill.objects.filter(enrollment=enrollment).update(
            due_date=timezone.localdate() - timedelta(days=60)
        )

        services.terminate(actor=manager, container=enrollment)

        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.TERMINATED

    def test_nothing_is_written_off_when_nothing_is_owed(self, manager, branch, enrollment):
        for _ in range(enrollment.bills.count()):
            services.collect_bill_payment(
                actor=manager, branch=branch,
                bill=enrollment.oldest_unpaid_bill(), method="cash",
            )

        services.terminate(actor=manager, container=enrollment)

        assert not AuditLog.objects.filter(action=AuditLog.Action.WRITE_OFF).exists()
        assert set(enrollment.bills.values_list("status", flat=True)) == {BillStatus.PAID}

    def test_succeeds_once_everything_is_settled(self, manager, branch, enrollment):
        for _ in range(enrollment.bills.count()):
            bill = enrollment.oldest_unpaid_bill()
            services.collect_bill_payment(
                actor=manager, branch=branch, bill=bill, method="cash"
            )

        services.terminate(actor=manager, container=enrollment)

        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.TERMINATED
        assert enrollment.terminated_at is not None

    def test_written_off_bills_do_not_block_termination(self, manager, enrollment):
        """
        The sanctioned escape for uncollectable money — an admin-approved
        write-off, not silent termination.
        """
        enrollment.bills.update(status=BillStatus.WRITTEN_OFF)

        services.terminate(actor=manager, container=enrollment)
        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.TERMINATED

    def test_paid_history_survives_termination(self, manager, branch, enrollment):
        for _ in range(enrollment.bills.count()):
            services.collect_bill_payment(
                actor=manager, branch=branch,
                bill=enrollment.oldest_unpaid_bill(), method="cash",
            )
        services.terminate(actor=manager, container=enrollment)

        assert Payment.objects.filter(branch=branch).count() == 3

    def test_terminating_twice_is_safe(self, manager, branch, enrollment):
        enrollment.bills.update(status=BillStatus.WRITTEN_OFF)
        services.terminate(actor=manager, container=enrollment)
        services.terminate(actor=manager, container=enrollment)  # must not raise

        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.TERMINATED


class TestMonthlyBillGenerationJob:
    def test_creates_the_next_month_for_an_active_enrollment(self, enrollment):
        last = enrollment.bills.order_by("-month").first()
        year, month = (int(p) for p in last.month.split("-"))
        target = services.add_months(date(year, month, 1), 1)

        result = services.generate_due_bills(up_to=target)

        assert result["created"] == 1
        assert enrollment.bills.count() == 4

    def test_running_twice_creates_no_duplicate(self, enrollment):
        """
        Idempotency comes from the (enrollment, month) unique constraint, not
        from trusting cron to fire exactly once.
        """
        last = enrollment.bills.order_by("-month").first()
        year, month = (int(p) for p in last.month.split("-"))
        target = services.add_months(date(year, month, 1), 1)

        services.generate_due_bills(up_to=target)
        second = services.generate_due_bills(up_to=target)

        assert second["created"] == 0
        assert enrollment.bills.count() == 4

    def test_backfills_months_missed_while_the_server_was_down(self, enrollment):
        last = enrollment.bills.order_by("-month").first()
        year, month = (int(p) for p in last.month.split("-"))
        target = services.add_months(date(year, month, 1), 3)

        result = services.generate_due_bills(up_to=target)

        assert result["created"] == 3, "missed months must be filled, not skipped"

    def test_skips_terminated_enrollments(self, enrollment):
        enrollment.status = EnrollmentStatus.TERMINATED
        enrollment.save(update_fields=["status"])

        last = enrollment.bills.order_by("-month").first()
        year, month = (int(p) for p in last.month.split("-"))
        target = services.add_months(date(year, month, 1), 1)

        assert services.generate_due_bills(up_to=target)["created"] == 0

    def test_generated_bills_get_the_right_due_date_and_amount(
        self, enrollment, monthly_service
    ):
        last = enrollment.bills.order_by("-month").first()
        year, month = (int(p) for p in last.month.split("-"))
        target = services.add_months(date(year, month, 1), 1)

        services.generate_due_bills(up_to=target)

        created = enrollment.bills.order_by("-month").first()
        assert created.due_date.day == 5
        assert created.amount == monthly_service.fee

    def test_management_command_runs(self, enrollment):
        from django.core.management import call_command

        call_command("generate_monthly_bills")  # must not raise

    def test_dry_run_writes_nothing(self, enrollment):
        from django.core.management import call_command

        before = MonthlyBill.objects.count()
        call_command("generate_monthly_bills", "--dry-run")
        assert MonthlyBill.objects.count() == before


@pytest.mark.isolation
class TestEnrollmentBranchIsolation:
    def test_manager_sees_only_their_branchs_enrollments(
        self, manager_client, other_manager, other_branch, patient_factory,
        service_factory, enrollment,
    ):
        services.create_monthly_enrollment(
            actor=other_manager, branch=other_branch,
            patient=patient_factory(branch=other_branch), service=service_factory(),
        )

        response = manager_client.get(reverse("enrollments:monthly-enrollment-list"))
        assert response.json()["count"] == 1

    def test_manager_cannot_read_another_branchs_enrollment(
        self, other_manager_client, enrollment
    ):
        response = other_manager_client.get(
            reverse("enrollments:monthly-enrollment-detail", args=[enrollment.id])
        )
        assert response.status_code == 404

    def test_cannot_enroll_another_branchs_patient(
        self, manager_client, patient_factory, other_branch, monthly_service
    ):
        foreign = patient_factory(branch=other_branch)

        response = manager_client.post(
            reverse("enrollments:monthly-enrollment-list"),
            {"patient": str(foreign.id), "service": str(monthly_service.id)},
        )
        assert response.status_code == 404


class TestPayAndTerminateEndpoints:
    """
    Everything above this class exercises the service functions directly
    (services.collect_bill_payment, services.terminate, ...), which is the
    right way to pin business logic but never once dispatches a real HTTP
    request through pay_bill / pay_installment / terminate on the viewsets.
    That gap is exactly the shape of bug the DB test run found elsewhere in
    this project (PaymentViewSet.get_permissions() silently never applying
    IsManager) — a bug that lives in the view layer is invisible to tests
    that only ever call the service layer. These hit the actual endpoints.
    """

    def pay_bill_url(self, enrollment, bill):
        return reverse(
            "enrollments:monthly-enrollment-pay-bill", args=[enrollment.id, bill.id]
        )

    def terminate_monthly_url(self, enrollment):
        return reverse("enrollments:monthly-enrollment-terminate", args=[enrollment.id])

    def pay_installment_url(self, plan, installment):
        return reverse(
            "enrollments:installment-plan-pay-installment", args=[plan.id, installment.id]
        )

    def terminate_installment_url(self, plan):
        return reverse("enrollments:installment-plan-terminate", args=[plan.id])

    def test_pay_bill_endpoint_settles_the_bill_and_advances_the_next(
        self, manager_client, enrollment
    ):
        bill = enrollment.oldest_unpaid_bill()

        response = manager_client.post(
            self.pay_bill_url(enrollment, bill), {"method": "cash"}, format="json"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["payment"]["category"] == "monthly"
        assert Decimal(body["payment"]["amount"]) == Decimal("5000.00")

        bill.refresh_from_db()
        assert bill.status == BillStatus.PAID
        assert bill.paid_at is not None

        next_bill = enrollment.bills.exclude(pk=bill.pk).order_by("month").first()
        assert next_bill.status == BillStatus.DUE

    def test_pay_bill_endpoint_rejects_paying_out_of_order(
        self, manager_client, enrollment
    ):
        september = enrollment.bills.order_by("month")[1]

        response = manager_client.post(
            self.pay_bill_url(enrollment, september), {"method": "cash"}, format="json"
        )

        assert response.status_code == 400
        assert "code" in response.json()
        assert Payment.objects.count() == 0

    def test_pay_bill_endpoint_404s_for_a_bill_id_from_another_enrollment(
        self, manager_client, manager, branch, patient_factory, monthly_service, enrollment
    ):
        other_enrollment = services.create_monthly_enrollment(
            actor=manager, branch=branch,
            patient=patient_factory(), service=monthly_service,
        )
        foreign_bill = other_enrollment.oldest_unpaid_bill()

        response = manager_client.post(
            self.pay_bill_url(enrollment, foreign_bill), {"method": "cash"}, format="json"
        )

        assert response.status_code == 404

    def test_pay_bill_endpoint_requires_manager(self, admin_client, enrollment):
        bill = enrollment.oldest_unpaid_bill()

        response = admin_client.post(
            self.pay_bill_url(enrollment, bill), {"method": "cash"}, format="json"
        )

        assert response.status_code == 403

    def test_pay_bill_endpoint_validates_the_method_choice(
        self, manager_client, enrollment
    ):
        bill = enrollment.oldest_unpaid_bill()

        response = manager_client.post(
            self.pay_bill_url(enrollment, bill), {"method": "not-a-method"}, format="json"
        )

        assert response.status_code == 400

    def test_terminate_endpoint_writes_off_an_unpaid_bill(self, manager_client, enrollment):
        """Over HTTP too: unpaid no longer refuses, it forgives and records."""
        response = manager_client.post(self.terminate_monthly_url(enrollment), format="json")

        assert response.status_code == 200
        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.TERMINATED
        assert set(enrollment.bills.values_list("status", flat=True)) == {
            BillStatus.WRITTEN_OFF
        }

    def test_terminate_endpoint_succeeds_once_settled(
        self, manager_client, manager, branch, enrollment
    ):
        for bill in enrollment.bills.order_by("month"):
            services.collect_bill_payment(
                actor=manager, branch=branch, bill=bill, method="cash"
            )

        response = manager_client.post(self.terminate_monthly_url(enrollment), format="json")

        assert response.status_code == 200
        assert response.json()["status"] == EnrollmentStatus.TERMINATED

    def test_terminate_endpoint_requires_manager(self, admin_client, enrollment):
        response = admin_client.post(self.terminate_monthly_url(enrollment), format="json")
        assert response.status_code == 403

    def test_pay_installment_endpoint_settles_and_advances(
        self, manager_client, manager, branch, patient, installment_service
    ):
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=3,
        )
        first = plan.installments.order_by("index").first()

        response = manager_client.post(
            self.pay_installment_url(plan, first), {"method": "bkash"}, format="json"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["payment"]["category"] == "installment"

        first.refresh_from_db()
        assert first.status == BillStatus.PAID

        second = plan.installments.exclude(pk=first.pk).order_by("index").first()
        assert second.status == BillStatus.DUE

    def test_pay_installment_endpoint_rejects_paying_out_of_order(
        self, manager_client, manager, branch, patient, installment_service
    ):
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=3,
        )
        second = plan.installments.order_by("index")[1]

        response = manager_client.post(
            self.pay_installment_url(plan, second), {"method": "bkash"}, format="json"
        )

        assert response.status_code == 400
        assert Payment.objects.count() == 0

    def test_pay_installment_endpoint_404s_for_an_installment_from_another_plan(
        self, manager_client, manager, branch, patient_factory, installment_service
    ):
        plan_a = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient_factory(),
            service=installment_service, number_of_installments=2,
        )
        plan_b = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient_factory(),
            service=installment_service, number_of_installments=2,
        )
        foreign_installment = plan_b.installments.order_by("index").first()

        response = manager_client.post(
            self.pay_installment_url(plan_a, foreign_installment),
            {"method": "cash"}, format="json",
        )

        assert response.status_code == 404

    def test_terminate_installment_endpoint_writes_off_and_stops_the_plan(
        self, manager_client, manager, branch, patient, installment_service
    ):
        """Over HTTP too: unpaid no longer refuses, it forgives and records."""
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=2,
        )

        response = manager_client.post(
            self.terminate_installment_url(plan), format="json"
        )

        assert response.status_code == 200
        plan.refresh_from_db()
        assert plan.status == EnrollmentStatus.TERMINATED
        assert set(plan.installments.values_list("status", flat=True)) == {
            BillStatus.WRITTEN_OFF
        }

    def test_terminate_installment_endpoint_succeeds_once_settled(
        self, manager_client, manager, branch, patient, installment_service
    ):
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=2,
        )
        for installment in plan.installments.order_by("index"):
            services.collect_installment_payment(
                actor=manager, branch=branch, installment=installment, method="cash"
            )

        response = manager_client.post(
            self.terminate_installment_url(plan), format="json"
        )

        assert response.status_code == 200
        assert response.json()["status"] == EnrollmentStatus.TERMINATED

    @pytest.mark.isolation
    def test_pay_bill_endpoint_is_branch_scoped(
        self, other_manager_client, enrollment
    ):
        bill = enrollment.oldest_unpaid_bill()

        response = other_manager_client.post(
            self.pay_bill_url(enrollment, bill), {"method": "cash"}, format="json"
        )

        assert response.status_code == 404

    @pytest.mark.isolation
    def test_terminate_endpoint_is_branch_scoped(
        self, other_manager_client, enrollment
    ):
        response = other_manager_client.post(
            self.terminate_monthly_url(enrollment), format="json"
        )

        assert response.status_code == 404


@pytest.mark.money
class TestInstallmentDateRange:
    """The manager agrees a window; the plan has to be cleared inside it."""

    def test_due_dates_span_the_range_inclusive(
        self, manager, branch, patient, installment_service
    ):
        starts = date(2026, 3, 1)
        ends = date(2026, 9, 1)

        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=3,
            starts_on=starts, ends_on=ends,
        )

        dues = list(plan.installments.order_by("index").values_list("due_date", flat=True))
        assert dues[0] == starts
        assert dues[-1] == ends
        # Evenly spaced, so nothing bunches at one end.
        assert dues[1] == date(2026, 6, 1)

    def test_the_range_is_stored_on_the_plan(
        self, manager, branch, patient, installment_service
    ):
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=2,
            starts_on=date(2026, 3, 1), ends_on=date(2026, 4, 1),
        )

        assert plan.starts_on == date(2026, 3, 1)
        assert plan.ends_on == date(2026, 4, 1)

    def test_an_end_before_the_start_is_rejected(
        self, manager, branch, patient, installment_service
    ):
        with pytest.raises(services.EnrollmentError):
            services.create_installment_plan(
                actor=manager, branch=branch, patient=patient,
                service=installment_service, number_of_installments=3,
                starts_on=date(2026, 9, 1), ends_on=date(2026, 3, 1),
            )

    def test_a_range_too_short_for_the_parts_is_rejected(
        self, manager, branch, patient, installment_service
    ):
        """Three installments need three distinct days to fall on."""
        with pytest.raises(services.EnrollmentError):
            services.create_installment_plan(
                actor=manager, branch=branch, patient=patient,
                service=installment_service, number_of_installments=3,
                starts_on=date(2026, 3, 1), ends_on=date(2026, 3, 2),
            )

    def test_omitting_the_range_keeps_the_monthly_schedule(
        self, manager, branch, patient, installment_service
    ):
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=3,
        )

        assert plan.starts_on is None
        assert plan.installments.count() == 3


@pytest.mark.money
class TestPartialInstallmentCollection:
    """
    The manager takes whatever the patient can pay today; the rest is carried
    into the later installments so the plan total never changes.
    """

    def _plan(self, manager, branch, patient, installment_service, parts=3):
        return services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=parts,
        )

    def test_paying_less_carries_the_shortfall_into_later_installments(
        self, manager, branch, patient, installment_service
    ):
        plan = self._plan(manager, branch, patient, installment_service)
        first = plan.installments.order_by("index").first()

        services.collect_installment_payment(
            actor=manager, branch=branch, installment=first,
            method="cash", amount=Decimal("1000.00"),
        )

        rows = list(plan.installments.order_by("index"))
        assert rows[0].amount == Decimal("1000.00")
        assert rows[0].status == BillStatus.PAID
        # 18,500 − 1,000 = 17,500 across the two that remain.
        assert sum(r.amount for r in rows[1:]) == Decimal("17500.00")
        assert rows[1].amount == Decimal("8750.00")

    def test_the_plan_total_survives_a_partial_payment(
        self, manager, branch, patient, installment_service
    ):
        plan = self._plan(manager, branch, patient, installment_service)
        first = plan.installments.order_by("index").first()

        services.collect_installment_payment(
            actor=manager, branch=branch, installment=first,
            method="cash", amount=Decimal("333.33"),
        )

        assert sum(i.amount for i in plan.installments.all()) == plan.total_amount

    def test_paying_more_than_scheduled_reduces_the_later_ones(
        self, manager, branch, patient, installment_service
    ):
        plan = self._plan(manager, branch, patient, installment_service)
        first = plan.installments.order_by("index").first()

        services.collect_installment_payment(
            actor=manager, branch=branch, installment=first,
            method="cash", amount=Decimal("10000.00"),
        )

        rows = list(plan.installments.order_by("index"))
        assert rows[0].amount == Decimal("10000.00")
        assert sum(r.amount for r in rows[1:]) == Decimal("8500.00")

    def test_the_outstanding_balance_reflects_what_is_still_owed(
        self, manager, branch, patient, installment_service
    ):
        plan = self._plan(manager, branch, patient, installment_service)
        first = plan.installments.order_by("index").first()

        services.collect_installment_payment(
            actor=manager, branch=branch, installment=first,
            method="cash", amount=Decimal("1000.00"),
        )

        assert plan.outstanding_total() == Decimal("17500.00")

    def test_zero_or_negative_is_rejected(
        self, manager, branch, patient, installment_service
    ):
        plan = self._plan(manager, branch, patient, installment_service)
        first = plan.installments.order_by("index").first()

        with pytest.raises(services.EnrollmentError):
            services.collect_installment_payment(
                actor=manager, branch=branch, installment=first,
                method="cash", amount=Decimal("0.00"),
            )

    def test_more_than_the_whole_plan_is_rejected(
        self, manager, branch, patient, installment_service
    ):
        plan = self._plan(manager, branch, patient, installment_service)
        first = plan.installments.order_by("index").first()

        with pytest.raises(services.EnrollmentError):
            services.collect_installment_payment(
                actor=manager, branch=branch, installment=first,
                method="cash", amount=Decimal("20000.00"),
            )

    def test_omitting_the_amount_collects_the_scheduled_figure(
        self, manager, branch, patient, installment_service
    ):
        plan = self._plan(manager, branch, patient, installment_service)
        first = plan.installments.order_by("index").first()
        scheduled = first.amount

        payment, _ = services.collect_installment_payment(
            actor=manager, branch=branch, installment=first, method="cash",
        )

        assert payment.amount == scheduled


@pytest.mark.money
class TestInstallmentEndpointCarriesTheNewFields:
    """
    Regression: the service accepted `starts_on`/`ends_on` and `amount`, and
    the serializer accepted them off the wire, but the *view* dropped both on
    the floor — so a manager's date range and partial collection silently
    became the defaults. Service-level tests can't see that; these post real
    requests.
    """

    def test_creating_a_plan_over_http_honours_the_date_range(
        self, manager_client, patient, installment_service
    ):
        response = manager_client.post(
            reverse("enrollments:installment-plan-list"),
            {
                "patient": str(patient.id),
                "service": str(installment_service.id),
                "numberOfInstallments": 3,
                "startsOn": "2026-10-01",
                "endsOn": "2027-04-01",
            },
            format="json",
        )

        assert response.status_code == 201
        body = response.json()
        assert body["startsOn"] == "2026-10-01"
        assert body["endsOn"] == "2027-04-01"
        dues = [i["dueDate"] for i in body["installments"]]
        assert dues[0] == "2026-10-01"
        assert dues[-1] == "2027-04-01"

    def test_paying_a_partial_amount_over_http_redistributes_the_rest(
        self, manager_client, manager, branch, patient, installment_service
    ):
        plan = services.create_installment_plan(
            actor=manager, branch=branch, patient=patient,
            service=installment_service, number_of_installments=3,
        )
        first = plan.installments.order_by("index").first()

        response = manager_client.post(
            reverse(
                "enrollments:installment-plan-pay-installment", args=[plan.id, first.id]
            ),
            {"method": "cash", "amount": "1000.00"},
            format="json",
        )

        assert response.status_code == 200
        body = response.json()
        assert Decimal(body["payment"]["amount"]) == Decimal("1000.00")

        rows = sorted(body["plan"]["installments"], key=lambda i: i["index"])
        assert Decimal(rows[0]["amount"]) == Decimal("1000.00")
        # 18,500 − 1,000 shared across the two that remain.
        assert Decimal(rows[1]["amount"]) == Decimal("8750.00")
        assert sum(Decimal(r["amount"]) for r in rows) == installment_service.fee
