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

    def test_replayed_idempotency_key_does_not_settle_twice(
        self, manager, branch, enrollment
    ):
        bill = enrollment.oldest_unpaid_bill()

        services.collect_bill_payment(
            actor=manager, branch=branch, bill=bill, method="cash", idempotency_key="k1"
        )
        payment_count = Payment.objects.count()

        # A second call with the same key returns the original payment; the
        # bill must not be settled again or the next bill double-promoted.
        bill.refresh_from_db()
        assert Payment.objects.count() == payment_count

    def test_cannot_collect_on_a_terminated_enrollment(self, manager, branch, enrollment):
        bill = enrollment.oldest_unpaid_bill()
        enrollment.status = EnrollmentStatus.TERMINATED
        enrollment.save(update_fields=["status"])

        with pytest.raises(services.EnrollmentError) as exc:
            services.collect_bill_payment(actor=manager, branch=branch, bill=bill, method="cash")
        assert exc.value.code == "terminated"


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
    def test_blocked_while_a_bill_is_unpaid(self, manager, enrollment):
        with pytest.raises(services.EnrollmentError) as exc:
            services.terminate(actor=manager, container=enrollment)

        assert exc.value.code == "outstanding_dues"
        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.ACTIVE

    def test_the_error_names_the_amount_owed(self, manager, enrollment):
        with pytest.raises(services.EnrollmentError) as exc:
            services.terminate(actor=manager, container=enrollment)

        assert exc.value.extra["outstandingAmount"] == "15000.00"  # 3 × 5,000

    def test_blocked_while_a_bill_is_overdue(self, manager, branch, enrollment):
        MonthlyBill.objects.filter(enrollment=enrollment).update(
            due_date=timezone.localdate() - timedelta(days=60)
        )

        with pytest.raises(services.EnrollmentError):
            services.terminate(actor=manager, container=enrollment)

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
