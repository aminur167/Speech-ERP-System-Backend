"""
Payment tests — follows the required-test list in docs/04.

The most correctness-critical suite in the project. The groups that carry the
most weight:

  * separation of duties on refunds — a manager must never be able to approve
    their own request, because that's the control that makes pocketing cash
    detectable;
  * idempotency — a retry or a replayed offline mutation must never
    double-charge;
  * atomicity — a failure partway must leave no orphan payment or half-applied
    refund;
  * Decimal exactness — the classic float bug, guarded at the API boundary.
"""

import threading
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone

from apps.common.models import AuditLog
from apps.payments import services
from apps.payments.models import Payment, PaymentCategory, PaymentStatus, RefundRequest

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient(patient_factory):
    return patient_factory(name="Rafiul Islam")


@pytest.fixture
def payment(manager, branch, patient):
    made, _ = services.create_payment(
        actor=manager,
        branch=branch,
        patient=patient,
        amount=Decimal("800.00"),
        method="cash",
        category=PaymentCategory.DAILY,
        description="Consultation Fee",
    )
    return made


class TestPaymentCreation:
    def test_receipt_and_transaction_ids_are_branch_prefixed(self, payment, branch):
        year = timezone.localdate().year
        assert payment.receipt_number == f"RCPT-{branch.short_code}-{year}-00001"
        assert payment.transaction_id == f"TXN-{branch.short_code}-{year}-00001"

    def test_receipt_numbers_increment_without_gaps(self, manager, branch, patient_factory):
        numbers = []
        for _ in range(5):
            made, _ = services.create_payment(
                actor=manager, branch=branch, patient=patient_factory(),
                amount=Decimal("100.00"), method="cash",
            )
            numbers.append(int(made.receipt_number.split("-")[-1]))

        assert numbers == [1, 2, 3, 4, 5]

    @pytest.mark.isolation
    def test_each_branch_has_its_own_receipt_series(
        self, manager, other_manager, branch, other_branch, patient_factory
    ):
        """Per-branch series is what makes offline issuance collision-free."""
        mine, _ = services.create_payment(
            actor=manager, branch=branch, patient=patient_factory(),
            amount=Decimal("100.00"), method="cash",
        )
        theirs, _ = services.create_payment(
            actor=other_manager, branch=other_branch,
            patient=patient_factory(branch=other_branch),
            amount=Decimal("100.00"), method="cash",
        )

        assert mine.receipt_number.endswith("00001")
        assert theirs.receipt_number.endswith("00001")
        assert "DHK" in mine.receipt_number
        assert "CTG" in theirs.receipt_number

    def test_collected_by_comes_from_the_actor(self, payment, manager):
        assert payment.collected_by_id == manager.id

    def test_branch_is_the_managers_own_even_if_another_is_posted(
        self, manager_client, patient, other_branch
    ):
        response = manager_client.post(
            reverse("payments:payment-list"),
            {
                "patient": str(patient.id),
                "amount": "500.00",
                "method": "cash",
                "branch": str(other_branch.id),
            },
        )

        assert response.status_code == 201
        assert Payment.objects.get(pk=response.json()["id"]).branch_id != other_branch.id

    def test_cannot_pay_against_another_branchs_patient(
        self, manager_client, patient_factory, other_branch
    ):
        foreign = patient_factory(branch=other_branch)

        response = manager_client.post(
            reverse("payments:payment-list"),
            {"patient": str(foreign.id), "amount": "500.00", "method": "cash"},
        )
        assert response.status_code == 404


@pytest.mark.money
class TestDecimalCorrectness:
    def test_amount_round_trips_exactly(self, manager, branch, patient):
        made, _ = services.create_payment(
            actor=manager, branch=branch, patient=patient,
            amount=Decimal("1234.56"), method="cash",
        )
        made.refresh_from_db()
        assert made.amount == Decimal("1234.56")

    def test_summing_many_payments_has_no_drift(self, manager, branch, patient_factory):
        for _ in range(100):
            services.create_payment(
                actor=manager, branch=branch, patient=patient_factory(),
                amount=Decimal("1234.56"), method="cash",
            )

        total = sum(p.amount for p in Payment.objects.all())
        assert total == Decimal("123456.00")

    @pytest.mark.parametrize("bad", ["0.00", "-100.00"])
    def test_non_positive_amounts_rejected(self, manager_client, patient, bad):
        response = manager_client.post(
            reverse("payments:payment-list"),
            {"patient": str(patient.id), "amount": bad, "method": "cash"},
        )
        assert response.status_code == 400

    def test_service_layer_rejects_zero(self, manager, branch, patient):
        with pytest.raises(services.PaymentError):
            services.create_payment(
                actor=manager, branch=branch, patient=patient,
                amount=Decimal("0.00"), method="cash",
            )


class TestIdempotency:
    """Without this, a flaky connection during a payment double-charges."""

    def test_same_key_creates_one_payment(self, manager_client, patient):
        body = {
            "patient": str(patient.id), "amount": "500.00",
            "method": "cash", "idempotencyKey": "key-abc-123",
        }

        first = manager_client.post(reverse("payments:payment-list"), body)
        second = manager_client.post(reverse("payments:payment-list"), body)

        assert first.status_code == 201
        assert second.status_code == 200  # replayed, not newly created
        assert first.json()["receiptNumber"] == second.json()["receiptNumber"]
        assert Payment.objects.count() == 1

    def test_different_keys_create_distinct_payments(self, manager_client, patient):
        for key in ("key-1", "key-2"):
            manager_client.post(
                reverse("payments:payment-list"),
                {
                    "patient": str(patient.id), "amount": "500.00",
                    "method": "cash", "idempotencyKey": key,
                },
            )

        assert Payment.objects.count() == 2

    def test_replay_after_other_activity_still_returns_the_original(
        self, manager_client, patient
    ):
        """The offline-queue case: the replay may arrive much later."""
        body = {
            "patient": str(patient.id), "amount": "500.00",
            "method": "cash", "idempotencyKey": "queued-key",
        }
        original = manager_client.post(reverse("payments:payment-list"), body).json()

        manager_client.post(
            reverse("payments:payment-list"),
            {"patient": str(patient.id), "amount": "700.00", "method": "bkash"},
        )

        replay = manager_client.post(reverse("payments:payment-list"), body)
        assert replay.json()["receiptNumber"] == original["receiptNumber"]
        assert Payment.objects.count() == 2

    def test_no_key_means_no_deduplication(self, manager_client, patient):
        for _ in range(2):
            manager_client.post(
                reverse("payments:payment-list"),
                {"patient": str(patient.id), "amount": "500.00", "method": "cash"},
            )
        assert Payment.objects.count() == 2


@pytest.mark.slow
@pytest.mark.money
class TestConcurrentReceiptNumbers:
    def test_concurrent_payments_never_share_a_receipt_number(
        self, manager, branch, patient_factory, django_db_blocker
    ):
        """
        The mock's in-memory counter passes every sequential test and still
        collides across Gunicorn workers. Only a threaded test proves the row
        lock holds.
        """
        patients = [patient_factory() for _ in range(8)]
        receipts, errors = [], []

        def worker(p):
            try:
                with django_db_blocker.unblock():
                    made, _ = services.create_payment(
                        actor=manager, branch=branch, patient=p,
                        amount=Decimal("100.00"), method="cash",
                    )
                    receipts.append(made.receipt_number)
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(p,)) for p in patients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"workers raised: {errors}"
        assert len(set(receipts)) == len(receipts), f"duplicates: {sorted(receipts)}"


class TestVoid:
    def test_manager_voids_a_same_day_payment(self, manager_client, payment):
        response = manager_client.post(
            reverse("payments:payment-void", args=[payment.id]),
            {"reason": "Wrong amount entered"},
        )

        assert response.status_code == 200
        payment.refresh_from_db()
        assert payment.status == PaymentStatus.VOID

    def test_void_leaves_the_original_amount_intact(self, manager_client, payment):
        manager_client.post(
            reverse("payments:payment-void", args=[payment.id]), {"reason": "Mistake"}
        )
        payment.refresh_from_db()
        assert payment.amount == Decimal("800.00")

    def test_void_requires_a_reason(self, manager_client, payment):
        response = manager_client.post(
            reverse("payments:payment-void", args=[payment.id]), {"reason": "   "}
        )
        assert response.status_code == 400

    def test_manager_cannot_void_a_previous_day_payment(self, manager_client, payment):
        from datetime import timedelta

        Payment.objects.filter(pk=payment.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )

        response = manager_client.post(
            reverse("payments:payment-void", args=[payment.id]), {"reason": "Late catch"}
        )

        assert response.status_code == 403
        assert response.json()["code"] == "not_same_day"

    def test_manager_cannot_void_after_the_closing_is_submitted(
        self, manager_client, manager, branch, payment
    ):
        """
        The reconciliation cutoff: once the day is signed off, changing it
        would invalidate the count that was signed off.
        """
        from apps.dailyclosing.services import submit_closing

        submit_closing(actor=manager, branch=branch, actual_total=Decimal("800.00"))

        response = manager_client.post(
            reverse("payments:payment-void", args=[payment.id]), {"reason": "Too late"}
        )

        assert response.status_code == 403
        assert response.json()["code"] == "closing_submitted"

    def test_admin_can_void_a_previous_day_payment(self, admin_client, payment):
        from datetime import timedelta

        Payment.objects.filter(pk=payment.pk).update(
            created_at=timezone.now() - timedelta(days=3)
        )

        response = admin_client.post(
            reverse("payments:payment-void", args=[payment.id]), {"reason": "Correction"}
        )
        assert response.status_code == 200

    def test_cannot_void_twice(self, manager_client, payment):
        manager_client.post(
            reverse("payments:payment-void", args=[payment.id]), {"reason": "First"}
        )
        response = manager_client.post(
            reverse("payments:payment-void", args=[payment.id]), {"reason": "Second"}
        )
        assert response.status_code == 400

    def test_void_is_audited(self, manager_client, payment, manager):
        manager_client.post(
            reverse("payments:payment-void", args=[payment.id]), {"reason": "Duplicate entry"}
        )

        entry = AuditLog.objects.filter(action=AuditLog.Action.VOID).first()
        assert entry is not None
        assert entry.actor_id == manager.id
        assert entry.reason == "Duplicate entry"


class TestRefundRequestFlow:
    """Separation of duties — the highest-value group in this file."""

    def test_manager_opens_a_request_and_the_payment_is_unchanged(
        self, manager_client, payment
    ):
        response = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Patient cancelled"},
        )

        assert response.status_code == 201
        assert response.json()["status"] == "pending"

        payment.refresh_from_db()
        assert payment.status == PaymentStatus.PAID  # nothing moves until approval

    def test_manager_cannot_approve_any_request(self, manager_client, payment):
        """
        The control that makes the whole thing meaningful. Without it,
        pocketing cash and recording a refund is undetectable.
        """
        created = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Cancelled"},
        ).json()

        response = manager_client.post(
            reverse("payments:refund-request-approve", args=[created["id"]]),
            {"billAction": "reopen"},
        )
        assert response.status_code == 403

    def test_manager_cannot_reject_either(self, manager_client, payment):
        created = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Cancelled"},
        ).json()

        response = manager_client.post(
            reverse("payments:refund-request-reject", args=[created["id"]]),
            {"reviewNote": "No"},
        )
        assert response.status_code == 403

    def test_admin_cannot_open_a_request(self, admin_client, payment):
        """Requests originate at the branch; an admin doing both defeats the split."""
        response = admin_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Cancelled"},
        )
        assert response.status_code == 403

    def test_admin_approves_and_the_payment_becomes_refunded(
        self, manager_client, admin_client, payment
    ):
        created = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Cancelled"},
        ).json()

        response = admin_client.post(
            reverse("payments:refund-request-approve", args=[created["id"]]),
            {"billAction": "reopen"},
        )

        assert response.status_code == 200
        payment.refresh_from_db()
        assert payment.status == PaymentStatus.REFUNDED

    def test_reject_requires_a_note(self, manager_client, admin_client, payment):
        created = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Cancelled"},
        ).json()

        response = admin_client.post(
            reverse("payments:refund-request-reject", args=[created["id"]]), {}
        )
        assert response.status_code == 400

    def test_rejection_leaves_the_payment_paid(self, manager_client, admin_client, payment):
        created = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Cancelled"},
        ).json()

        admin_client.post(
            reverse("payments:refund-request-reject", args=[created["id"]]),
            {"reviewNote": "Service was delivered"},
        )

        payment.refresh_from_db()
        assert payment.status == PaymentStatus.PAID
        assert RefundRequest.objects.get(pk=created["id"]).status == "rejected"

    def test_request_requires_a_reason(self, manager_client, payment):
        response = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "  "},
        )
        assert response.status_code == 400

    def test_cannot_open_a_second_request_while_one_is_pending(
        self, manager_client, payment
    ):
        body = {"amount": "400.00", "reason": "Partial"}
        manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]), body
        )
        response = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]), body
        )

        assert response.status_code == 400
        assert response.json()["code"] == "already_pending"

    def test_cannot_refund_an_already_refunded_payment(
        self, manager_client, admin_client, payment
    ):
        created = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Cancelled"},
        ).json()
        admin_client.post(
            reverse("payments:refund-request-approve", args=[created["id"]]),
            {"billAction": "reopen"},
        )

        response = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Again"},
        )
        assert response.status_code == 400

    def test_cannot_refund_a_voided_payment(self, manager_client, payment):
        manager_client.post(
            reverse("payments:payment-void", args=[payment.id]), {"reason": "Void"}
        )

        response = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Refund too"},
        )
        assert response.status_code == 400

    def test_refund_cannot_exceed_the_payment(self, manager_client, payment):
        response = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "5000.00", "reason": "Too much"},
        )

        assert response.status_code == 400
        assert response.json()["code"] == "exceeds_refundable"

    @pytest.mark.isolation
    def test_manager_cannot_refund_another_branchs_payment(
        self, other_manager_client, payment
    ):
        response = other_manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Not mine"},
        )
        assert response.status_code == 404

    def test_pending_request_has_no_reviewer(self, manager_client, payment):
        created = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Cancelled"},
        ).json()

        request = RefundRequest.objects.get(pk=created["id"])
        assert request.reviewed_by is None
        assert request.reviewed_at is None

    def test_approval_records_the_reviewer(
        self, manager_client, admin_client, admin_user, payment
    ):
        created = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Cancelled"},
        ).json()
        admin_client.post(
            reverse("payments:refund-request-approve", args=[created["id"]]),
            {"billAction": "reopen"},
        )

        request = RefundRequest.objects.get(pk=created["id"])
        assert request.reviewed_by_id == admin_user.id
        assert request.reviewed_at is not None

    def test_each_step_is_audited(self, manager_client, admin_client, payment):
        created = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Cancelled"},
        ).json()
        admin_client.post(
            reverse("payments:refund-request-approve", args=[created["id"]]),
            {"billAction": "reopen", "reviewNote": "Approved"},
        )

        actions = set(AuditLog.objects.values_list("action", flat=True))
        assert AuditLog.Action.REFUND_REQUEST in actions
        assert AuditLog.Action.REFUND_APPROVE in actions


class TestRevenueExclusion:
    def test_refunded_payment_stops_counting_as_revenue(
        self, manager_client, admin_client, payment
    ):
        created = manager_client.post(
            reverse("payments:payment-request-refund", args=[payment.id]),
            {"amount": "800.00", "reason": "Cancelled"},
        ).json()
        admin_client.post(
            reverse("payments:refund-request-approve", args=[created["id"]]),
            {"billAction": "reopen"},
        )

        payment.refresh_from_db()
        assert payment.counts_as_revenue is False

    def test_voided_payment_is_not_revenue(self, manager_client, payment):
        manager_client.post(
            reverse("payments:payment-void", args=[payment.id]), {"reason": "Void"}
        )
        payment.refresh_from_db()
        assert payment.counts_as_revenue is False


class TestReceiptData:
    def test_every_field_the_receipt_renders_is_present(self, manager_client, payment):
        body = manager_client.get(
            reverse("payments:payment-detail", args=[payment.id])
        ).json()

        for field in (
            "receiptNumber", "transactionId", "createdAt", "amount", "method",
            "status", "patientName", "description", "collectedBy", "branchName",
        ):
            assert field in body, f"receipt needs {field}"
            assert body[field] not in (None, ""), f"{field} is empty"

    @pytest.mark.isolation
    def test_branch_name_is_the_real_branch_not_a_default(
        self, other_manager, other_manager_client, other_branch, patient_factory
    ):
        """
        A hardcoded "Main Branch" was a real bug in the frontend. Tested with a
        non-Dhaka branch specifically, since a Dhaka-only test would pass even
        with the bug present.
        """
        foreign_payment, _ = services.create_payment(
            actor=other_manager, branch=other_branch,
            patient=patient_factory(branch=other_branch),
            amount=Decimal("500.00"), method="cash", description="Consultation",
        )

        body = other_manager_client.get(
            reverse("payments:payment-detail", args=[foreign_payment.id])
        ).json()

        assert body["branchName"] == "Chattogram Branch"

    def test_collecting_user_name_is_correct(self, manager_client, payment, manager):
        body = manager_client.get(
            reverse("payments:payment-detail", args=[payment.id])
        ).json()
        assert body["collectedBy"] == manager.name


@pytest.mark.isolation
class TestPaymentBranchIsolation:
    def test_manager_cannot_read_another_branchs_payment(
        self, other_manager_client, payment
    ):
        response = other_manager_client.get(
            reverse("payments:payment-detail", args=[payment.id])
        )
        assert response.status_code == 404

    def test_manager_cannot_void_another_branchs_payment(
        self, other_manager_client, payment
    ):
        response = other_manager_client.post(
            reverse("payments:payment-void", args=[payment.id]), {"reason": "Nope"}
        )
        assert response.status_code == 404

    def test_admin_sees_every_branch(self, admin_client, payment):
        response = admin_client.get(reverse("payments:payment-detail", args=[payment.id]))
        assert response.status_code == 200


class TestNoDestructiveEndpoints:
    """Money rows are never edited or deleted through the API."""

    def test_put_is_not_allowed(self, manager_client, payment):
        response = manager_client.put(
            reverse("payments:payment-detail", args=[payment.id]), {"amount": "1.00"}
        )
        assert response.status_code == 405

    def test_delete_is_not_allowed(self, manager_client, payment):
        response = manager_client.delete(
            reverse("payments:payment-detail", args=[payment.id])
        )
        assert response.status_code == 405
