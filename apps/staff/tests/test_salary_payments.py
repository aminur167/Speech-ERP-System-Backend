"""
Salary payment approval workflow: Manager requests → Admin approves/rejects →
Manager disburses → an Expense is created automatically.
"""

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.expenses.models import Expense
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
