"""
Salary payment approval workflow: Manager requests → Admin approves/rejects →
Manager disburses → an Expense is created automatically.
"""

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.expenses.models import Expense
from apps.notifications.models import Notification
from apps.staff import services

pytestmark = pytest.mark.django_db


@pytest.fixture
def farhana(staff_member_factory):
    return staff_member_factory(name="Farhana Akter", monthly_salary=Decimal("42000.00"))


@pytest.fixture
def current_month():
    return timezone.localdate().strftime("%Y-%m")


class TestRequestSalaryPayment:
    def test_manager_can_request(self, manager_client, farhana, current_month):
        response = manager_client.post(
            reverse("staff:staffmember-request-salary-payment", args=[farhana.id]),
            {"month": current_month},
            format="json",
        )
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "pending_approval"
        assert body["amount"] == "42000.00"
        assert body["staffId"] == str(farhana.id)

    def test_amount_includes_that_months_bonus(self, manager, manager_client, farhana, current_month):
        services.add_bonus(actor=manager, staff=farhana, amount=Decimal("1500.00"), reason="x")

        response = manager_client.post(
            reverse("staff:staffmember-request-salary-payment", args=[farhana.id]),
            {"month": current_month},
            format="json",
        )
        assert response.json()["amount"] == "43500.00"

    def test_admin_cannot_request(self, admin_client, farhana, current_month):
        response = admin_client.post(
            reverse("staff:staffmember-request-salary-payment", args=[farhana.id]),
            {"month": current_month},
            format="json",
        )
        assert response.status_code == 403

    def test_duplicate_pending_request_is_blocked(self, manager, farhana, current_month):
        services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        with pytest.raises(services.SalaryPaymentError):
            services.request_salary_payment(actor=manager, staff=farhana, month=current_month)

    def test_new_request_allowed_after_rejection(self, manager, admin_user, farhana, current_month):
        first = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.review_salary_payment(
            actor=admin_user, payment=first, approve=False, review_note="Wrong amount"
        )
        # Should not raise now that the only prior request was rejected.
        second = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        assert second.id != first.id

    def test_manager_cannot_request_for_other_branch_staff(self, other_manager_client, farhana, current_month):
        response = other_manager_client.post(
            reverse("staff:staffmember-request-salary-payment", args=[farhana.id]),
            {"month": current_month},
            format="json",
        )
        assert response.status_code == 404


@pytest.mark.money
class TestReviewSalaryPayment:
    def test_admin_can_approve(self, admin_client, manager, farhana, current_month):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)

        response = admin_client.post(
            reverse("staff:salarypayment-review", args=[payment.id]), {"approve": True}, format="json"
        )
        assert response.status_code == 200
        assert response.json()["status"] == "approved"

    def test_reject_requires_a_reason(self, admin_client, manager, farhana, current_month):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)

        response = admin_client.post(
            reverse("staff:salarypayment-review", args=[payment.id]), {"approve": False}, format="json"
        )
        assert response.status_code == 400

    def test_reject_with_reason_succeeds(self, admin_client, manager, farhana, current_month):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)

        response = admin_client.post(
            reverse("staff:salarypayment-review", args=[payment.id]),
            {"approve": False, "reviewNote": "Attendance doesn't match."},
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["status"] == "rejected"

    def test_manager_cannot_review(self, manager_client, manager, farhana, current_month):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)

        response = manager_client.post(
            reverse("staff:salarypayment-review", args=[payment.id]), {"approve": True}, format="json"
        )
        assert response.status_code == 403

    def test_cannot_review_an_already_reviewed_request(self, admin_user, manager, farhana, current_month):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)

        with pytest.raises(services.SalaryPaymentError):
            services.review_salary_payment(actor=admin_user, payment=payment, approve=True)


@pytest.mark.money
class TestDisburseSalaryPayment:
    def test_manager_can_disburse_after_approval(
        self, manager_client, manager, admin_user, farhana, current_month
    ):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)

        response = manager_client.post(
            reverse("staff:salarypayment-disburse", args=[payment.id]),
            {"paymentMethod": "cash"},
            format="json",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "paid"
        assert body["expenseCode"]

    def test_disburse_creates_an_already_approved_expense(
        self, manager, admin_user, farhana, current_month
    ):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)
        payment = services.disburse_salary_payment(actor=manager, payment=payment, payment_method="cash")

        expense = Expense.objects.get(pk=payment.expense_id)
        assert expense.category == Expense.Category.SALARIES
        assert expense.amount == payment.amount
        assert expense.status == Expense.Status.APPROVED
        assert expense.paid_to == farhana.name
        assert expense.branch_id == farhana.branch_id

    def test_cannot_disburse_without_approval(self, manager, farhana, current_month):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        with pytest.raises(services.SalaryPaymentError):
            services.disburse_salary_payment(actor=manager, payment=payment, payment_method="cash")

    def test_cannot_disburse_twice(self, manager, admin_user, farhana, current_month):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)
        payment = services.disburse_salary_payment(actor=manager, payment=payment, payment_method="cash")

        with pytest.raises(services.SalaryPaymentError):
            services.disburse_salary_payment(actor=manager, payment=payment, payment_method="cash")

    def test_admin_cannot_disburse(self, admin_client, manager, admin_user, farhana, current_month):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)

        response = admin_client.post(
            reverse("staff:salarypayment-disburse", args=[payment.id]),
            {"paymentMethod": "cash"},
            format="json",
        )
        assert response.status_code == 403

    def test_amount_is_frozen_at_request_time_even_if_a_bonus_is_added_later(
        self, manager, admin_user, farhana, current_month
    ):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.add_bonus(actor=manager, staff=farhana, amount=Decimal("5000.00"), reason="late bonus")
        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)
        payment = services.disburse_salary_payment(actor=manager, payment=payment, payment_method="cash")

        assert payment.amount == Decimal("42000.00")
        assert Expense.objects.get(pk=payment.expense_id).amount == Decimal("42000.00")


class TestSalaryPaymentBranchIsolation:
    def test_manager_only_sees_own_branch_requests(
        self, manager_client, other_manager, manager, farhana, staff_member_factory, other_branch, current_month
    ):
        other_staff = staff_member_factory(name="Other", branch=other_branch)
        services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.request_salary_payment(actor=other_manager, staff=other_staff, month=current_month)

        results = manager_client.get(reverse("staff:salarypayment-list")).json()["results"]
        assert len(results) == 1
        assert results[0]["staffId"] == str(farhana.id)

    def test_admin_sees_every_branch(
        self, admin_client, manager, other_manager, farhana, staff_member_factory, other_branch, current_month
    ):
        other_staff = staff_member_factory(name="Other", branch=other_branch)
        services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.request_salary_payment(actor=other_manager, staff=other_staff, month=current_month)

        results = admin_client.get(reverse("staff:salarypayment-list")).json()["results"]
        assert len(results) == 2

    def test_other_manager_cannot_disburse_across_branches(
        self, other_manager_client, manager, admin_user, farhana, current_month
    ):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)

        response = other_manager_client.post(
            reverse("staff:salarypayment-disburse", args=[payment.id]),
            {"paymentMethod": "cash"},
            format="json",
        )
        assert response.status_code == 404


class TestBranchSummary:
    def test_splits_approved_from_paid(self, admin_client, manager, admin_user, farhana, current_month):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)
        services.disburse_salary_payment(actor=manager, payment=payment, payment_method="cash")

        response = admin_client.get(reverse("staff:salarypayment-branch-summary"))
        assert response.status_code == 200
        row = response.json()[0]
        assert row["branchId"] == str(farhana.branch_id)
        assert row["approvedAmount"] == "0.00"
        assert row["paidAmount"] == "42000.00"
        assert row["totalApprovedAmount"] == "42000.00"
        assert row["paymentCount"] == 1

    def test_pending_and_rejected_are_excluded(self, admin_client, manager, admin_user, farhana, current_month):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.review_salary_payment(
            actor=admin_user, payment=payment, approve=False, review_note="no"
        )

        response = admin_client.get(reverse("staff:salarypayment-branch-summary"))
        assert response.json() == []

    def test_admin_sees_every_branch_broken_out_separately(
        self, admin_client, manager, other_manager, admin_user, farhana, staff_member_factory,
        other_branch, current_month,
    ):
        other_staff = staff_member_factory(name="Other", branch=other_branch, monthly_salary=Decimal("20000.00"))

        first = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.review_salary_payment(actor=admin_user, payment=first, approve=True)

        second = services.request_salary_payment(actor=other_manager, staff=other_staff, month=current_month)
        services.review_salary_payment(actor=admin_user, payment=second, approve=True)

        response = admin_client.get(reverse("staff:salarypayment-branch-summary"))
        rows = {row["branchId"]: row for row in response.json()}
        assert rows[str(farhana.branch_id)]["approvedAmount"] == "42000.00"
        assert rows[str(other_staff.branch_id)]["approvedAmount"] == "20000.00"

    def test_manager_only_sees_own_branch(
        self, manager_client, manager, other_manager, admin_user, farhana, staff_member_factory,
        other_branch, current_month,
    ):
        other_staff = staff_member_factory(name="Other", branch=other_branch)
        first = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.review_salary_payment(actor=admin_user, payment=first, approve=True)
        second = services.request_salary_payment(actor=other_manager, staff=other_staff, month=current_month)
        services.review_salary_payment(actor=admin_user, payment=second, approve=True)

        response = manager_client.get(reverse("staff:salarypayment-branch-summary"))
        rows = response.json()
        assert len(rows) == 1
        assert rows[0]["branchId"] == str(farhana.branch_id)

    def test_month_filter_narrows_the_summary(self, admin_client, manager, admin_user, farhana):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month="2026-01")
        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)

        response = admin_client.get(
            reverse("staff:salarypayment-branch-summary"), {"month": "2026-02"}
        )
        assert response.json() == []


class TestNotifications:
    def test_requesting_notifies_every_admin(
        self, manager, admin_user, farhana, current_month, staff_member_factory
    ):
        from apps.accounts.models import User

        other_admin = User.objects.create_user(
            email="other-admin@speechlab.test", password="x", name="Other Admin",
            role=User.Role.ADMIN,
        )

        services.request_salary_payment(actor=manager, staff=farhana, month=current_month)

        assert Notification.objects.filter(recipient=admin_user, title="New salary payment request").exists()
        assert Notification.objects.filter(recipient=other_admin, title="New salary payment request").exists()
        # The manager who asked isn't an admin and shouldn't get the admin-facing copy.
        assert not Notification.objects.filter(recipient=manager).exists()

    def test_requesting_manager_is_not_notified_of_their_own_request(
        self, manager, farhana, current_month
    ):
        services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        assert Notification.objects.filter(recipient=manager).count() == 0

    def test_approval_notifies_the_requesting_manager(self, manager, admin_user, farhana, current_month):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        Notification.objects.all().delete()  # clear the admin-facing one from the request itself

        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)

        notification = Notification.objects.get(recipient=manager)
        assert notification.title == "Salary payment approved"
        assert payment.staff.name in notification.message

    def test_rejection_notifies_the_requesting_manager_with_the_reason(
        self, manager, admin_user, farhana, current_month
    ):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        Notification.objects.all().delete()

        services.review_salary_payment(
            actor=admin_user, payment=payment, approve=False, review_note="Wrong month"
        )

        notification = Notification.objects.get(recipient=manager)
        assert notification.title == "Salary payment rejected"
        assert "Wrong month" in notification.message


class TestPendingCount:
    def test_counts_only_pending_approval(self, admin_client, manager, admin_user, farhana, current_month):
        payment = services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        response = admin_client.get(reverse("staff:salarypayment-pending-count"))
        assert response.json()["count"] == 1

        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)
        response = admin_client.get(reverse("staff:salarypayment-pending-count"))
        assert response.json()["count"] == 0

    def test_manager_cannot_read_the_pending_count(self, manager_client):
        response = manager_client.get(reverse("staff:salarypayment-pending-count"))
        assert response.status_code == 403

    def test_admin_sees_pending_count_across_every_branch(
        self, admin_client, manager, other_manager, farhana, staff_member_factory,
        other_branch, current_month,
    ):
        other_staff = staff_member_factory(name="Other", branch=other_branch)
        services.request_salary_payment(actor=manager, staff=farhana, month=current_month)
        services.request_salary_payment(actor=other_manager, staff=other_staff, month=current_month)

        response = admin_client.get(reverse("staff:salarypayment-pending-count"))
        assert response.json()["count"] == 2
