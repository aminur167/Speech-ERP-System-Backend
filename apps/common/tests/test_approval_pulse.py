"""
Approval queues: waiting requests on top, and the pulse that tells screens
something changed.

Each ordering test builds the case the old newest-first order got wrong: a
pending request that is *older* than one already decided. Newest-first put
the decided one on top; pending-first must not.
"""

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.approvals import bump
from apps.common.models import ApprovalActivity
from apps.expenses import services as expense_services
from apps.payments import services as payment_services
from apps.services import services as catalog_services
from apps.services.models import PackageActionRequest, Service
from apps.staff import services as staff_services

pytestmark = pytest.mark.django_db

PULSE_URL = reverse("common:approval-pulse")


def expense_data(amount):
    return {
        "category": "supplies",
        "amount": Decimal(amount),
        "description": "Stationery",
        "paid_to": "Ali Stationers",
        "payment_method": "cash",
    }


def results(response):
    body = response.json()
    return body["results"] if isinstance(body, dict) and "results" in body else body


# ---------------------------------------------------------------------------
# Pending first
# ---------------------------------------------------------------------------


class TestPendingFirst:
    def test_expenses(self, admin_client, manager, branch):
        waiting = expense_services.create_expense(
            actor=manager, branch=branch, data=expense_data("9000.00")
        )
        expense_services.create_expense(  # auto-approved, and newer
            actor=manager, branch=branch, data=expense_data("100.00")
        )

        rows = results(admin_client.get(reverse("expenses:expense-list")))

        assert rows[0]["id"] == waiting.pk
        assert rows[0]["status"] == "pending"

    def test_expenses_even_when_filtered_to_one_period(self, admin_client, manager, branch):
        waiting = expense_services.create_expense(
            actor=manager, branch=branch, data=expense_data("9000.00")
        )
        expense_services.create_expense(
            actor=manager, branch=branch, data=expense_data("100.00")
        )

        rows = results(admin_client.get(reverse("expenses:expense-list"), {"period": "today"}))

        assert rows[0]["id"] == waiting.pk

    def test_refund_requests(
        self, admin_client, admin_user, manager, branch, patient_factory
    ):
        patient = patient_factory()

        def paid():
            payment, _ = payment_services.create_payment(
                actor=manager, branch=branch, patient=patient,
                amount=Decimal("1000.00"), method="cash",
            )
            return payment

        waiting = payment_services.request_refund(
            actor=manager, payment=paid(), amount=Decimal("100.00"), reason="Overcharged"
        )
        decided = payment_services.request_refund(
            actor=manager, payment=paid(), amount=Decimal("100.00"), reason="Overcharged"
        )
        payment_services.reject_refund(actor=admin_user, request=decided, review_note="No")

        rows = results(admin_client.get(reverse("payments:refund-request-list")))

        assert rows[0]["id"] == waiting.pk
        assert rows[0]["status"] == "pending"

    def test_salary_requests(self, admin_client, admin_user, manager, staff_member_factory):
        month = timezone.localdate().strftime("%Y-%m")
        waiting = staff_services.request_salary_payment(
            actor=manager, staff=staff_member_factory(name="Farhana"), month=month
        )
        decided = staff_services.request_salary_payment(
            actor=manager, staff=staff_member_factory(name="Rakib"), month=month
        )
        staff_services.review_salary_payment(actor=admin_user, payment=decided, approve=True)

        rows = results(admin_client.get(reverse("staff:salarypayment-list")))

        assert rows[0]["id"] == waiting.pk
        assert rows[0]["status"] == "pending_approval"

    def test_package_change_requests(self, admin_client, admin_user, manager, service_factory):
        waiting = catalog_services.request_package_action(
            actor=manager, service=service_factory(), action="edit", reason="Fee change"
        )
        decided = catalog_services.request_package_action(
            actor=manager, service=service_factory(), action="edit", reason="Fee change"
        )
        catalog_services.review_package_action(
            actor=admin_user, request=decided, approve=False, review_note="No"
        )

        rows = results(admin_client.get("/api/services/action-requests/"))

        assert str(rows[0]["id"]) == str(waiting.pk)
        assert rows[0]["status"] == PackageActionRequest.Status.PENDING

    def test_proposed_packages_top_the_catalog(self, admin_client, branch, service_factory):
        # Category order alone would put "daily" above "online".
        service_factory(category=Service.Category.DAILY, fee=Decimal("500.00"))
        proposal = service_factory(
            category=Service.Category.ONLINE, fee=Decimal("900.00"),
            review_status=Service.ReviewStatus.PENDING,
        )

        rows = results(
            admin_client.get(reverse("services:service-list"), {"includePending": "true"})
        )

        assert rows[0]["id"] == proposal.pk

    def test_an_explicit_ordering_still_wins(self, admin_client, manager, branch):
        expense_services.create_expense(
            actor=manager, branch=branch, data=expense_data("9000.00")
        )
        cheap = expense_services.create_expense(
            actor=manager, branch=branch, data=expense_data("100.00")
        )

        rows = results(admin_client.get(reverse("expenses:expense-list"), {"ordering": "amount"}))

        assert rows[0]["id"] == cheap.pk


# ---------------------------------------------------------------------------
# The pulse
# ---------------------------------------------------------------------------


def pulse(client) -> str:
    response = client.get(PULSE_URL)
    assert response.status_code == 200
    return response.json()["version"]


class TestApprovalPulse:
    def test_it_needs_a_login(self, api_client):
        assert api_client.get(PULSE_URL).status_code == 401

    def test_a_new_request_moves_admins_and_the_branchs_version(
        self, admin_client, manager_client, manager, branch, django_capture_on_commit_callbacks
    ):
        admin_before, manager_before = pulse(admin_client), pulse(manager_client)

        with django_capture_on_commit_callbacks(execute=True):
            expense_services.create_expense(
                actor=manager, branch=branch, data=expense_data("9000.00")
            )

        assert pulse(admin_client) != admin_before
        assert pulse(manager_client) != manager_before

    def test_admins_decision_reaches_the_manager(
        self, manager_client, admin_user, manager, branch, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            expense = expense_services.create_expense(
                actor=manager, branch=branch, data=expense_data("9000.00")
            )
        before = pulse(manager_client)

        with django_capture_on_commit_callbacks(execute=True):
            expense_services.review_expense(actor=admin_user, expense=expense, approve=True)

        assert pulse(manager_client) != before

    def test_another_branchs_activity_does_not_move_it(
        self, manager_client, other_manager, other_branch, django_capture_on_commit_callbacks
    ):
        before = pulse(manager_client)

        with django_capture_on_commit_callbacks(execute=True):
            expense_services.create_expense(
                actor=other_manager, branch=other_branch, data=expense_data("9000.00")
            )

        assert pulse(manager_client) == before

    def test_nothing_moves_until_the_change_commits(
        self, manager_client, manager, branch, django_capture_on_commit_callbacks
    ):
        before = pulse(manager_client)

        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            expense_services.create_expense(
                actor=manager, branch=branch, data=expense_data("9000.00")
            )

        assert callbacks, "the bump must be deferred to commit"
        assert pulse(manager_client) == before

    def test_a_rolled_back_change_never_moves_it(
        self, manager, branch, django_capture_on_commit_callbacks
    ):
        from django.db import transaction

        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            with pytest.raises(RuntimeError):
                with transaction.atomic():
                    expense_services.create_expense(
                        actor=manager, branch=branch, data=expense_data("9000.00")
                    )
                    raise RuntimeError("rolled back")

        # The deferred bump was discarded with the transaction it belonged to.
        assert callbacks == []
        assert not ApprovalActivity.objects.filter(branch_id=branch.pk).exists()

    def test_the_counter_upsert_counts(self, branch):
        bump(branch.pk)
        bump(branch.pk)

        assert ApprovalActivity.objects.get(branch_id=branch.pk).version == 2
