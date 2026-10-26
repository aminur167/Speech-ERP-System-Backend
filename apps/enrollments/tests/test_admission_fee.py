"""
Monthly package admit fee, and enrolling with the admit fee paid up front.

The rules these pin (confirmed with the client, docs/05):

  * every monthly package carries an admit fee; other categories never do;
  * the enrollment month carries TWO bills — the one-time admit fee and that
    month's own fee (confirmed 2026-10-26, replacing the single merged bill
    confirmed 2026-09-28). Each takes its own discount, each needing its own
    written reason;
  * only the admit fee is taken at enrollment. The first month is left `due`
    and collected afterwards, or skipped onto Due Payments;
  * enrolling and paying the admit fee are one atomic request, so there is
    never an enrollment whose first month was never paid;
  * reactivating a stopped service charges the monthly fee, not the admit
    fee again.
"""

from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.urls import reverse
from django.utils import timezone

from apps.common.models import AuditLog
from apps.enrollments import services
from apps.enrollments.models import BillStatus, MonthlyBill, MonthlyEnrollment
from apps.payments import services as payment_services
from apps.payments.models import Payment, PaymentCategory, RefundRequest
from apps.services.models import Service

pytestmark = pytest.mark.django_db

ENROLL_URL = reverse("enrollments:monthly-enrollment-list")


@pytest.fixture
def package(service_factory):
    return service_factory(
        name="Individual Therapy", code="MON-ADM",
        fee=Decimal("5000.00"), admission_fee=Decimal("3000.00"),
    )


@pytest.fixture
def patient(patient_factory):
    return patient_factory(name="Nusrat Jahan")


def enroll(client, patient, package, **extra):
    body = {"patient": patient.pk, "service": package.pk, "method": "cash", **extra}
    return client.post(ENROLL_URL, body, format="json")


def by_kind(bills):
    """The enrollment month's bills, keyed by kind — `admission` / `monthly`."""
    return {bill["kind"]: bill for bill in bills}


def pay_first_month(manager, branch, enrollment):
    """Settle the month that enrollment leaves `due`."""
    bill = enrollment.bills.get(kind=MonthlyBill.Kind.MONTHLY)
    return services.collect_bill_payment(
        actor=manager, branch=branch, bill=bill, method="cash"
    )


def enroll_directly(manager, branch, patient, package, **extra):
    return services.enroll_monthly_with_admission(
        actor=manager, branch=branch, patient=patient, service=package,
        method="cash", **extra,
    )


# ---------------------------------------------------------------------------
# The package field
# ---------------------------------------------------------------------------


def package_payload(**overrides):
    payload = {
        "name": "Monthly Group Plan",
        "category": "monthly",
        "fee": "4000.00",
        "admission_fee": "1500.00",
    }
    payload.update(overrides)
    return payload


class TestAdmissionFeeOnThePackage:
    def test_a_monthly_package_is_created_with_its_admit_fee(self, admin_client, branch):
        response = admin_client.post(
            reverse("services:service-list"), package_payload(branch=branch.id)
        )

        assert response.status_code == 201, response.json()
        assert response.json()["admissionFee"] == "1500.00"

    def test_a_monthly_package_without_one_is_refused(self, admin_client, branch):
        payload = package_payload(branch=branch.id)
        del payload["admission_fee"]

        response = admin_client.post(reverse("services:service-list"), payload)

        assert response.status_code == 400
        assert "admission_fee" in response.json()

    def test_a_blank_admit_fee_is_refused_too(self, admin_client, branch):
        response = admin_client.post(
            reverse("services:service-list"),
            package_payload(branch=branch.id, admission_fee=None),
            format="json",
        )
        assert response.status_code == 400
        assert "admission_fee" in response.json()

    @pytest.mark.parametrize("value", ["0.00", "-10.00"])
    def test_zero_or_negative_is_refused(self, admin_client, branch, value):
        response = admin_client.post(
            reverse("services:service-list"),
            package_payload(branch=branch.id, admission_fee=value),
        )
        assert response.status_code == 400
        assert "admission_fee" in response.json()

    def test_a_manager_proposal_needs_one_as_well(self, manager_client):
        payload = package_payload()
        del payload["admission_fee"]

        response = manager_client.post(reverse("services:service-list"), payload)

        assert response.status_code == 400
        assert "admission_fee" in response.json()

    @pytest.mark.parametrize("category", ["daily", "installment", "online"])
    def test_other_categories_never_store_one(self, admin_client, branch, category):
        response = admin_client.post(
            reverse("services:service-list"),
            package_payload(branch=branch.id, category=category),
        )

        assert response.status_code == 201, response.json()
        assert response.json()["admissionFee"] is None

    def test_a_partial_edit_keeps_the_stored_admit_fee(self, admin_client, package):
        response = admin_client.patch(
            reverse("services:service-detail", args=[package.pk]), {"fee": "5500.00"}
        )

        assert response.status_code == 200, response.json()
        assert response.json()["admissionFee"] == "3000.00"

    def test_an_edit_cannot_blank_it(self, admin_client, package):
        response = admin_client.patch(
            reverse("services:service-detail", args=[package.pk]),
            {"admission_fee": None},
            format="json",
        )
        assert response.status_code == 400

    def test_changing_it_is_in_the_audit_log(self, admin_client, package):
        admin_client.patch(
            reverse("services:service-detail", args=[package.pk]),
            {"admission_fee": "3500.00"},
        )

        entry = AuditLog.objects.filter(
            target_type="Service", target_id=str(package.pk), action=AuditLog.Action.UPDATE
        ).first()
        assert entry.changes["admission_fee"] == {"from": "3000.00", "to": "3500.00"}


class TestBackfillMigration:
    """0008 gives the packages that predate the field their monthly fee."""

    def _backfill(self):
        from importlib import import_module

        migration = import_module("apps.services.migrations.0008_backfill_monthly_admission_fee")
        migration.backfill(django_apps, None)

    def test_monthly_packages_get_their_monthly_fee(self, service_factory):
        old = service_factory(fee=Decimal("4200.00"), admission_fee=None)

        self._backfill()

        old.refresh_from_db()
        assert old.admission_fee == Decimal("4200.00")

    def test_a_set_admit_fee_is_left_alone(self, package):
        self._backfill()

        package.refresh_from_db()
        assert package.admission_fee == Decimal("3000.00")

    def test_other_categories_stay_empty(self, service_factory):
        daily = service_factory(category=Service.Category.DAILY, fee=Decimal("800.00"))

        self._backfill()

        daily.refresh_from_db()
        assert daily.admission_fee is None

    def test_soft_deleted_packages_are_included(self, service_factory):
        gone = service_factory(fee=Decimal("4200.00"), admission_fee=None)
        gone.delete()

        self._backfill()

        assert Service.all_objects.get(pk=gone.pk).admission_fee == Decimal("4200.00")


# ---------------------------------------------------------------------------
# Enrolling
# ---------------------------------------------------------------------------


@pytest.mark.money
class TestEnrollAndPay:
    def test_enrollment_bills_the_admit_fee_and_the_month_separately(
        self, manager_client, patient, package
    ):
        response = enroll(manager_client, patient, package)

        assert response.status_code == 201, response.json()
        bills = by_kind(response.json()["enrollment"]["bills"])
        this_month = services.month_key(timezone.localdate())

        assert set(bills) == {"admission", "monthly"}
        assert bills["admission"]["month"] == this_month
        assert bills["monthly"]["month"] == this_month
        assert bills["admission"]["amount"] == "3000.00"
        assert bills["monthly"]["amount"] == "5000.00"

    def test_only_the_admit_fee_is_taken_at_enrollment(
        self, manager_client, patient, package
    ):
        body = enroll(manager_client, patient, package).json()
        bills = by_kind(body["enrollment"]["bills"])

        assert bills["admission"]["status"] == "paid"
        assert bills["admission"]["outstanding"] == "0.00"
        assert body["payment"]["amount"] == "3000.00"
        assert body["payment"]["category"] == PaymentCategory.MONTHLY

        # Billed but not collected -- that is the step a manager may skip.
        assert bills["monthly"]["status"] in {"due", "overdue"}
        assert bills["monthly"]["outstanding"] == "5000.00"
        assert Payment.objects.count() == 1

    def test_the_first_month_can_be_collected_afterwards(
        self, manager, branch, patient, package
    ):
        enrollment, _ = enroll_directly(manager, branch, patient, package)

        payment, bill = pay_first_month(manager, branch, enrollment)

        assert payment.amount == Decimal("5000.00")
        assert bill.status == BillStatus.PAID
        assert enrollment.outstanding_total() == Decimal("0.00")

    def test_skipping_the_first_month_leaves_it_owed(
        self, manager, branch, patient, package
    ):
        """Skipping is simply not collecting it -- there is no second call."""
        enrollment, _ = enroll_directly(manager, branch, patient, package)

        assert enrollment.outstanding_total() == Decimal("5000.00")
        assert enrollment.oldest_unpaid_bill().kind == MonthlyBill.Kind.MONTHLY

    def test_admission_is_collected_before_the_first_month(
        self, manager, branch, patient, package
    ):
        """
        Oldest-first across two bills sharing a month. Pinned rather than
        trusted: it falls out of "admission" sorting before "monthly", which
        is alphabetical luck, not a rule anyone wrote down.
        """
        enrollment = MonthlyEnrollment.objects.create(
            patient=patient, service=package, branch=branch
        )
        month = services.month_key(timezone.localdate())
        due = services.due_date_for_month(month)
        MonthlyBill.objects.create(
            enrollment=enrollment, month=month, label="x",
            kind=MonthlyBill.Kind.MONTHLY, amount=Decimal("5000.00"),
            due_date=due, status=BillStatus.DUE,
        )
        MonthlyBill.objects.create(
            enrollment=enrollment, month=month, label="y",
            kind=MonthlyBill.Kind.ADMISSION, amount=Decimal("3000.00"),
            due_date=due, status=BillStatus.DUE,
        )

        assert enrollment.oldest_unpaid_bill().kind == MonthlyBill.Kind.ADMISSION

    def test_the_receipt_says_it_was_the_admit_fee(self, manager_client, patient, package):
        body = enroll(manager_client, patient, package).json()
        assert "Admission" in body["payment"]["description"]

    def test_a_discount_is_taken_off_and_recorded(self, manager_client, patient, package):
        response = enroll(
            manager_client, patient, package,
            discount="500.00", discountReason="Sibling already enrolled",
        )

        assert response.status_code == 201, response.json()
        body = response.json()
        bill = by_kind(body["enrollment"]["bills"])["admission"]
        assert bill["grossAmount"] == "3000.00"
        assert bill["discountAmount"] == "500.00"
        assert bill["discountReason"] == "Sibling already enrolled"
        assert bill["amount"] == "2500.00"
        assert body["payment"]["amount"] == "2500.00"

    def test_the_month_takes_its_own_discount(self, manager_client, patient, package):
        response = enroll(
            manager_client, patient, package,
            monthDiscount="800.00", monthDiscountReason="Half month only",
        )

        assert response.status_code == 201, response.json()
        bills = by_kind(response.json()["enrollment"]["bills"])
        assert bills["monthly"]["grossAmount"] == "5000.00"
        assert bills["monthly"]["discountAmount"] == "800.00"
        assert bills["monthly"]["discountReason"] == "Half month only"
        assert bills["monthly"]["amount"] == "4200.00"
        # The admit fee is untouched by the month's discount.
        assert bills["admission"]["amount"] == "3000.00"

    def test_the_two_discounts_are_independent(self, manager_client, patient, package):
        response = enroll(
            manager_client, patient, package,
            discount="500.00", discountReason="Sibling",
            monthDiscount="800.00", monthDiscountReason="Half month only",
        )

        bills = by_kind(response.json()["enrollment"]["bills"])
        assert bills["admission"]["amount"] == "2500.00"
        assert bills["monthly"]["amount"] == "4200.00"

    def test_a_month_discount_without_a_reason_is_refused(
        self, manager_client, patient, package
    ):
        response = enroll(manager_client, patient, package, monthDiscount="800.00")

        assert response.status_code == 400
        assert response.json()["code"] == "discount_reason_required"
        assert not MonthlyEnrollment.objects.exists()

    def test_a_month_discount_above_the_monthly_fee_is_refused(
        self, manager_client, patient, package
    ):
        response = enroll(
            manager_client, patient, package,
            monthDiscount="5000.01", monthDiscountReason="x",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "month_discount_exceeds_fee"
        assert not MonthlyEnrollment.objects.exists()
        assert not Payment.objects.exists()

    def test_the_discount_and_reason_are_in_the_audit_log(
        self, manager, branch, patient, package
    ):
        enrollment, _ = enroll_directly(
            manager, branch, patient, package,
            discount=Decimal("500.00"), discount_reason="Hardship",
        )

        entry = AuditLog.objects.get(
            target_type="MonthlyEnrollment", target_id=str(enrollment.pk),
            action=AuditLog.Action.CREATE,
        )
        assert entry.reason == "Hardship"
        assert entry.changes["admissionFee"] == "3000.00"
        assert entry.changes["discount"] == "500.00"
        assert entry.changes["charged"] == "2500.00"

    def test_a_discount_without_a_reason_is_refused(self, manager_client, patient, package):
        response = enroll(manager_client, patient, package, discount="500.00")

        assert response.status_code == 400
        assert response.json()["code"] == "discount_reason_required"
        assert not MonthlyEnrollment.objects.exists()

    def test_a_blank_reason_counts_as_none(self, manager_client, patient, package):
        response = enroll(
            manager_client, patient, package, discount="500.00", discountReason="   "
        )
        assert response.json()["code"] == "discount_reason_required"

    def test_a_discount_above_the_admit_fee_is_refused(self, manager_client, patient, package):
        response = enroll(
            manager_client, patient, package, discount="3000.01", discountReason="x"
        )

        assert response.status_code == 400
        assert response.json()["code"] == "discount_exceeds_fee"
        assert not MonthlyEnrollment.objects.exists()
        assert not Payment.objects.exists()

    def test_a_negative_discount_is_refused(self, manager_client, patient, package):
        response = enroll(
            manager_client, patient, package, discount="-100.00", discountReason="x"
        )
        assert response.status_code == 400
        assert not MonthlyEnrollment.objects.exists()

    def test_a_full_discount_settles_it_with_no_payment(
        self, manager_client, patient, package
    ):
        response = enroll(
            manager_client, patient, package,
            discount="3000.00", discountReason="Staff child",
        )

        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["payment"] is None
        bill = by_kind(body["enrollment"]["bills"])["admission"]
        assert bill["amount"] == "0.00"
        assert bill["status"] == "paid"
        assert bill["paidAt"] is not None
        assert not Payment.objects.exists()

    def test_a_package_with_no_admit_fee_is_refused(
        self, manager_client, patient, service_factory
    ):
        """
        It used to fall back to the monthly fee. Now that the month is billed
        separately, that fallback would charge the patient two months and
        call one of them an admission, so the enrollment is refused instead.
        """
        legacy = service_factory(fee=Decimal("4200.00"), admission_fee=None)

        response = enroll(manager_client, patient, legacy)

        assert response.status_code == 400
        assert response.json()["code"] == "admission_fee_missing"
        assert not MonthlyEnrollment.objects.exists()
        assert not Payment.objects.exists()

    def test_the_monthly_fee_starts_the_following_month(
        self, manager, branch, patient, package
    ):
        enrollment, _ = enroll_directly(manager, branch, patient, package)

        next_month = services.add_months(timezone.localdate(), 1)
        services.generate_due_bills(up_to=next_month)

        bill = enrollment.bills.get(month=services.month_key(next_month))
        assert bill.kind == MonthlyBill.Kind.MONTHLY
        assert bill.amount == Decimal("5000.00")
        assert bill.status == BillStatus.DUE

    def test_months_paid_ahead_are_charged_the_monthly_fee(
        self, manager, branch, patient, package
    ):
        enrollment, _ = enroll_directly(manager, branch, patient, package)
        pay_first_month(manager, branch, enrollment)
        ahead = services.month_key(services.add_months(timezone.localdate(), 1))

        payments, _ = services.collect_monthly_advance(
            actor=manager, branch=branch, enrollment=enrollment, months=[ahead],
            method="cash",
        )

        assert payments[0].amount == Decimal("5000.00")

    def test_advance_is_refused_while_the_first_month_is_skipped(
        self, manager, branch, patient, package
    ):
        """
        Skipping leaves an arrear, and arrears block advance months -- the
        oldest-first rule, unchanged. The wizard therefore ends at the
        receipt when the month is skipped.
        """
        enrollment, _ = enroll_directly(manager, branch, patient, package)
        ahead = services.month_key(services.add_months(timezone.localdate(), 1))

        with pytest.raises(services.EnrollmentError):
            services.collect_monthly_advance(
                actor=manager, branch=branch, enrollment=enrollment,
                months=[ahead], method="cash",
            )

    def test_a_non_monthly_package_is_refused(
        self, manager, branch, patient, service_factory
    ):
        daily = service_factory(category=Service.Category.DAILY, fee=Decimal("800.00"))

        with pytest.raises(services.EnrollmentError) as exc:
            enroll_directly(manager, branch, patient, daily)
        assert exc.value.code == "not_monthly"

    def test_outstanding_dues_still_block_it(
        self, manager, branch, manager_client, patient, package, service_factory
    ):
        other = service_factory(fee=Decimal("1000.00"))
        services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient, service=other
        )

        response = enroll(manager_client, patient, package)

        assert response.status_code == 400
        assert response.json()["code"] == "outstanding_dues"
        assert patient.monthly_enrollments.count() == 1


@pytest.mark.money
class TestOneAtomicStep:
    def test_a_request_without_a_payment_method_enrolls_nobody(
        self, manager_client, patient, package
    ):
        """The old enroll-now-pay-later request shape no longer enrolls."""
        response = manager_client.post(
            ENROLL_URL, {"patient": patient.pk, "service": package.pk}, format="json"
        )

        assert response.status_code == 400
        assert "method" in response.json()
        assert not MonthlyEnrollment.objects.exists()

    def test_a_payment_failure_leaves_no_enrollment_behind(
        self, manager, branch, patient, package, monkeypatch
    ):
        def refuse(**kwargs):
            raise payment_services.PaymentError("Card declined", code="declined")

        monkeypatch.setattr(payment_services, "record_payment", refuse)

        with pytest.raises(payment_services.PaymentError):
            enroll_directly(manager, branch, patient, package)

        assert not MonthlyEnrollment.objects.exists()
        assert not MonthlyBill.objects.exists()
        assert not AuditLog.objects.filter(target_type="MonthlyEnrollment").exists()

    def test_a_replayed_request_returns_the_original(self, manager_client, patient, package):
        first = enroll(manager_client, patient, package, idempotencyKey="enroll-key-1")
        second = enroll(manager_client, patient, package, idempotencyKey="enroll-key-1")

        assert first.status_code == 201
        assert second.status_code == 201
        assert second.json()["enrollment"]["id"] == first.json()["enrollment"]["id"]
        assert second.json()["payment"]["id"] == first.json()["payment"]["id"]
        assert MonthlyEnrollment.objects.count() == 1
        assert Payment.objects.count() == 1

    def test_a_replayed_full_discount_returns_the_original_too(
        self, manager_client, patient, package
    ):
        extra = {"discount": "3000.00", "discountReason": "Free", "idempotencyKey": "k-free"}
        first = enroll(manager_client, patient, package, **extra)
        second = enroll(manager_client, patient, package, **extra)

        assert second.json()["enrollment"]["id"] == first.json()["enrollment"]["id"]
        assert second.json()["payment"] is None
        assert MonthlyEnrollment.objects.count() == 1


@pytest.mark.money
class TestLaterLife:
    def test_refunding_the_admit_fee_reopens_what_was_paid(
        self, manager, admin_user, branch, patient, package
    ):
        enrollment, payment = enroll_directly(
            manager, branch, patient, package,
            discount=Decimal("500.00"), discount_reason="Hardship",
        )
        request = payment_services.request_refund(
            actor=manager, payment=payment, amount=Decimal("2500.00"), reason="Left town"
        )
        payment_services.approve_refund(
            actor=admin_user, request=request, bill_action=RefundRequest.BillAction.REOPEN
        )

        bill = enrollment.bills.get(kind=MonthlyBill.Kind.ADMISSION)
        # Owed again is what was charged after the discount, not the gross fee.
        assert bill.outstanding == Decimal("2500.00")

    def test_a_refund_cannot_exceed_what_was_charged(
        self, manager, branch, patient, package
    ):
        _, payment = enroll_directly(
            manager, branch, patient, package,
            discount=Decimal("500.00"), discount_reason="Hardship",
        )

        with pytest.raises(payment_services.PaymentError) as exc:
            payment_services.request_refund(
                actor=manager, payment=payment, amount=Decimal("3000.00"), reason="x"
            )
        assert exc.value.code == "exceeds_refundable"

    def test_reactivating_does_not_charge_the_admit_fee_again(
        self, manager, branch, patient, package
    ):
        enrollment, _ = enroll_directly(manager, branch, patient, package)
        pay_first_month(manager, branch, enrollment)
        services.stop_monthly_service(actor=manager, enrollment=enrollment, decisions={})

        services.resume_monthly_service(actor=manager, enrollment=enrollment)

        assert enrollment.bills.filter(kind=MonthlyBill.Kind.ADMISSION).count() == 1
        # One for the admission, one for the month. Neither is charged again.
        assert Payment.objects.count() == 2

    def test_stopping_asks_about_a_skipped_first_month(
        self, manager, branch, patient, package
    ):
        """
        The split gives the enrollment month a bill that can still be owed
        when the service stops, so it now needs a decision like any other
        arrear rather than disappearing with the service.
        """
        enrollment, _ = enroll_directly(manager, branch, patient, package)

        with pytest.raises(services.EnrollmentError):
            services.stop_monthly_service(
                actor=manager, enrollment=enrollment, decisions={}
            )

    def test_a_resumed_cycle_bills_the_monthly_fee(self, manager, branch, patient, package):
        enrollment, _ = enroll_directly(manager, branch, patient, package)
        later = services.add_months(timezone.localdate(), 2)

        services._open_cycle_from(enrollment, later)

        bill = enrollment.bills.get(month=services.month_key(later))
        assert bill.kind == MonthlyBill.Kind.MONTHLY
        assert bill.amount == Decimal("5000.00")

    def test_a_fresh_enrollment_charges_the_admit_fee_again(
        self, manager, branch, patient, package
    ):
        first, _ = enroll_directly(manager, branch, patient, package)
        pay_first_month(manager, branch, first)
        services.stop_monthly_service(actor=manager, enrollment=first, decisions={})

        second, payment = enroll_directly(manager, branch, patient, package)

        assert second.pk != first.pk
        assert payment.amount == Decimal("3000.00")

    def test_a_skipped_first_month_blocks_a_new_enrollment(
        self, manager, branch, patient, package
    ):
        """
        The existing "no new service while you owe" gate, now reachable from
        a skipped first month -- worth pinning because skipping is offered by
        the wizard rather than being an accident.
        """
        enroll_directly(manager, branch, patient, package)

        with pytest.raises(services.EnrollmentError):
            enroll_directly(manager, branch, patient, package)


# ---------------------------------------------------------------------------
# Who may, and where
# ---------------------------------------------------------------------------


@pytest.mark.isolation
class TestBranchIsolationAndRoles:
    def test_admin_cannot_enroll(self, admin_client, patient, package):
        response = enroll(admin_client, patient, package)
        assert response.status_code == 403
        assert not MonthlyEnrollment.objects.exists()

    def test_another_branchs_package_is_not_found(
        self, manager_client, patient, service_factory, other_branch
    ):
        foreign = service_factory(branch=other_branch, code="MON-FOR")

        response = enroll(manager_client, patient, foreign)

        assert response.status_code == 404
        assert not MonthlyEnrollment.objects.exists()

    def test_another_branchs_patient_is_not_found(
        self, manager_client, patient_factory, other_branch, package
    ):
        foreign = patient_factory(branch=other_branch)

        response = enroll(manager_client, foreign, package)

        assert response.status_code == 404

    def test_a_package_still_awaiting_approval_is_not_enrollable(
        self, manager_client, patient, service_factory
    ):
        proposal = service_factory(review_status=Service.ReviewStatus.PENDING)

        response = enroll(manager_client, patient, proposal)

        assert response.status_code == 404

    def test_an_inactive_package_is_not_enrollable(self, manager_client, patient, package):
        package.is_active = False
        package.save(update_fields=["is_active"])

        response = enroll(manager_client, patient, package)

        assert response.status_code == 404

    def test_the_bill_fields_are_visible_to_admin(
        self, admin_client, manager, branch, patient, package
    ):
        enrollment, _ = enroll_directly(
            manager, branch, patient, package,
            discount=Decimal("500.00"), discount_reason="Hardship",
        )

        body = admin_client.get(
            reverse("enrollments:monthly-enrollment-detail", args=[enrollment.pk])
        ).json()

        bill = by_kind(body["bills"])["admission"]
        assert bill["discountAmount"] == "500.00"
        assert bill["discountReason"] == "Hardship"
