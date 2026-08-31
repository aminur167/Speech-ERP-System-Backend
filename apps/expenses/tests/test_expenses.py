"""
Expense tests — follows the required-test list in docs/08.

The rules worth defending here are all ones that move money on a report:

  * the auto-approval threshold is computed server-side, so a posted
    `status: "approved"` on a large expense cannot bypass review;
  * totals count approved AND pending but not rejected, because this system
    records money already spent — the identical rule Net Revenue uses;
  * a decision can be reversed, but never silently.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.common.models import AuditLog
from apps.expenses import services
from apps.expenses.models import Expense

pytestmark = pytest.mark.django_db

LIST_URL = reverse("expenses:expense-list")
SUMMARY_URL = reverse("expenses:expense-summary")


def detail_url(expense):
    return reverse("expenses:expense-detail", args=[expense.pk])


def review_url(expense):
    return reverse("expenses:expense-review", args=[expense.pk])


def payload(**overrides):
    data = {
        "category": Expense.Category.SUPPLIES,
        "amount": "1200.00",
        "description": "Therapy room stationery",
        "paid_to": "Ali Stationers",
        "payment_method": "cash",
    }
    data.update(overrides)
    return data


@pytest.fixture
def expense_factory(db, branch, manager):
    """Expenses created through the service, so codes and status are real."""

    def _create(*, actor=None, target_branch=None, **overrides):
        return services.create_expense(
            actor=actor or manager,
            branch=target_branch or branch,
            data=payload(**overrides),
        )

    return _create


class TestAutoApprovalThreshold:
    """
    The one real business rule in this module. The boundary is `>=`: exactly
    the threshold needs review.
    """

    def test_below_threshold_is_approved_immediately(self, expense_factory):
        assert expense_factory(amount="4999.00").status == Expense.Status.APPROVED

    def test_exactly_the_threshold_needs_review(self, expense_factory):
        assert expense_factory(amount="5000.00").status == Expense.Status.PENDING

    def test_above_the_threshold_needs_review(self, expense_factory):
        assert expense_factory(amount="5001.00").status == Expense.Status.PENDING

    def test_auto_approved_expense_is_stamped_and_explained(self, expense_factory):
        expense = expense_factory(amount="100.00")

        assert expense.reviewed_at is not None
        assert expense.review_note  # says why it skipped review
        assert expense.reviewed_by is None  # no human decided it

    def test_pending_expense_has_no_review_stamp(self, expense_factory):
        expense = expense_factory(amount="9000.00")

        assert expense.reviewed_at is None
        assert expense.reviewed_by is None
        assert expense.review_note == ""

    @override_settings(EXPENSE_AUTO_APPROVE_THRESHOLD=500)
    def test_threshold_comes_from_configuration_not_a_constant(self, expense_factory):
        """
        Lowering the setting must change behaviour with no code change — an
        amount that was auto-approved at 5,000 now needs review.
        """
        assert expense_factory(amount="1200.00").status == Expense.Status.PENDING

    @override_settings(EXPENSE_AUTO_APPROVE_THRESHOLD=100000)
    def test_raising_the_threshold_auto_approves_more(self, expense_factory):
        assert expense_factory(amount="50000.00").status == Expense.Status.APPROVED


class TestPrivilegeEscalation:
    def test_posted_status_is_ignored_on_a_large_expense(self, manager_client):
        """
        The escalation attempt: claim `approved` on a 50,000 taka expense. The
        server computes status from the amount and never reads this field.
        """
        response = manager_client.post(
            LIST_URL, payload(amount="50000.00", status="approved"), format="json"
        )

        assert response.status_code == 201
        assert response.json()["status"] == Expense.Status.PENDING

    def test_posted_branch_is_ignored(self, manager_client, manager, other_branch):
        response = manager_client.post(
            LIST_URL, payload(branch=str(other_branch.id)), format="json"
        )

        assert response.status_code == 201
        assert response.json()["branchId"] == str(manager.branch_id)

    def test_submitted_by_comes_from_the_token(self, manager_client, manager, admin_user):
        response = manager_client.post(
            LIST_URL, payload(submitted_by=str(admin_user.id)), format="json"
        )

        assert response.json()["submittedBy"] == manager.name

    def test_posted_review_fields_are_ignored(self, manager_client):
        response = manager_client.post(
            LIST_URL,
            payload(amount="8000.00", review_note="approved by me", reviewNote="ok"),
            format="json",
        )

        assert response.json()["reviewNote"] == ""


class TestReviewPermissions:
    def test_manager_may_submit(self, manager_client):
        assert manager_client.post(LIST_URL, payload(), format="json").status_code == 201

    def test_admin_may_not_submit(self, admin_client):
        """
        Admin has no branch, so an expense from them has nowhere to land.
        Submission is a branch-floor action.
        """
        assert admin_client.post(LIST_URL, payload(), format="json").status_code == 403

    def test_manager_cannot_approve(self, manager_client, expense_factory):
        expense = expense_factory(amount="9000.00")

        response = manager_client.post(
            review_url(expense), {"approve": True}, format="json"
        )

        assert response.status_code == 403
        expense.refresh_from_db()
        assert expense.status == Expense.Status.PENDING

    def test_manager_cannot_reject(self, manager_client, expense_factory):
        expense = expense_factory(amount="9000.00")

        response = manager_client.post(
            review_url(expense), {"approve": False, "reviewNote": "no"}, format="json"
        )

        assert response.status_code == 403

    def test_admin_approves(self, admin_client, admin_user, expense_factory):
        expense = expense_factory(amount="9000.00")

        body = admin_client.post(review_url(expense), {"approve": True}, format="json").json()

        assert body["status"] == Expense.Status.APPROVED
        assert body["reviewedBy"] == admin_user.name
        assert body["reviewedAt"] is not None

    def test_admin_rejects_with_a_reason(self, admin_client, expense_factory):
        expense = expense_factory(amount="9000.00")

        body = admin_client.post(
            review_url(expense),
            {"approve": False, "reviewNote": "Duplicate of EXP-2026-00011"},
            format="json",
        ).json()

        assert body["status"] == Expense.Status.REJECTED
        assert body["reviewNote"] == "Duplicate of EXP-2026-00011"

    def test_expenses_cannot_be_edited_in_place(self, manager_client, expense_factory):
        """Money records are corrected by rejection, never by editing."""
        expense = expense_factory()

        assert manager_client.patch(
            detail_url(expense), {"amount": "1.00"}, format="json"
        ).status_code == 405

    def test_expenses_cannot_be_deleted(self, manager_client, admin_client, expense_factory):
        """
        A manager who could delete their own submission could spend money and
        remove the evidence.
        """
        expense = expense_factory()

        assert manager_client.delete(detail_url(expense)).status_code == 405
        assert admin_client.delete(detail_url(expense)).status_code == 405


class TestReviewNotes:
    def test_rejecting_without_a_reason_is_refused(self, admin_client, expense_factory):
        expense = expense_factory(amount="9000.00")

        response = admin_client.post(review_url(expense), {"approve": False}, format="json")

        assert response.status_code == 400
        assert response.json()["code"] == "note_required"
        expense.refresh_from_db()
        assert expense.status == Expense.Status.PENDING

    def test_a_whitespace_only_reason_does_not_count(self, admin_client, expense_factory):
        expense = expense_factory(amount="9000.00")

        response = admin_client.post(
            review_url(expense), {"approve": False, "reviewNote": "   "}, format="json"
        )

        assert response.status_code == 400

    def test_approving_without_a_note_is_allowed(self, admin_client, expense_factory):
        expense = expense_factory(amount="9000.00")

        assert admin_client.post(
            review_url(expense), {"approve": True}, format="json"
        ).status_code == 200

    def test_approving_with_a_note_stores_it(self, admin_client, expense_factory):
        expense = expense_factory(amount="9000.00")

        body = admin_client.post(
            review_url(expense),
            {"approve": True, "reviewNote": "Verified against the invoice"},
            format="json",
        ).json()

        assert body["reviewNote"] == "Verified against the invoice"

    def test_manager_remarks_and_admin_note_are_separate_fields(
        self, admin_client, expense_factory
    ):
        """One never overwrites the other — they answer different questions."""
        expense = expense_factory(amount="9000.00", remarks="Urgent AC repair")

        body = admin_client.post(
            review_url(expense), {"approve": True, "reviewNote": "Invoice seen"}, format="json"
        ).json()

        assert body["remarks"] == "Urgent AC repair"
        assert body["reviewNote"] == "Invoice seen"

    def test_approving_an_already_approved_expense_is_refused(
        self, admin_client, expense_factory
    ):
        expense = expense_factory(amount="1000.00")  # auto-approved

        response = admin_client.post(review_url(expense), {"approve": True}, format="json")

        assert response.status_code == 400
        assert response.json()["code"] == "no_change"


class TestReversingDecisions:
    def test_approved_to_rejected_with_a_reason(self, admin_client, expense_factory):
        expense = expense_factory(amount="9000.00")
        admin_client.post(review_url(expense), {"approve": True}, format="json")

        body = admin_client.post(
            review_url(expense),
            {"approve": False, "reviewNote": "Receipt turned out to be forged"},
            format="json",
        ).json()

        assert body["status"] == Expense.Status.REJECTED

    def test_rejected_to_approved_with_a_reason(self, admin_client, expense_factory):
        expense = expense_factory(amount="9000.00")
        admin_client.post(
            review_url(expense), {"approve": False, "reviewNote": "unclear"}, format="json"
        )

        body = admin_client.post(
            review_url(expense),
            {"approve": True, "reviewNote": "Manager produced the invoice"},
            format="json",
        ).json()

        assert body["status"] == Expense.Status.APPROVED

    def test_reversing_to_rejected_without_a_reason_is_refused(
        self, admin_client, expense_factory
    ):
        expense = expense_factory(amount="9000.00")
        admin_client.post(review_url(expense), {"approve": True}, format="json")

        assert admin_client.post(
            review_url(expense), {"approve": False}, format="json"
        ).status_code == 400

    def test_reversing_to_approved_without_a_reason_is_refused(
        self, admin_client, expense_factory
    ):
        """
        The case a naive rule misses: a note isn't normally required to
        approve, but reversing a rejection silently is exactly what an audit
        must be able to explain.
        """
        expense = expense_factory(amount="9000.00")
        admin_client.post(
            review_url(expense), {"approve": False, "reviewNote": "no receipt"}, format="json"
        )

        response = admin_client.post(review_url(expense), {"approve": True}, format="json")

        assert response.status_code == 400
        assert response.json()["code"] == "note_required"

    def test_each_decision_appends_an_audit_entry(self, admin_client, expense_factory):
        expense = expense_factory(amount="9000.00")

        def entries():
            return AuditLog.objects.filter(
                target_type="Expense", target_id=str(expense.pk)
            ).count()

        after_create = entries()
        admin_client.post(review_url(expense), {"approve": True}, format="json")
        after_approve = entries()
        admin_client.post(
            review_url(expense), {"approve": False, "reviewNote": "reversed"}, format="json"
        )
        after_reverse = entries()

        assert after_create == 1
        assert after_approve == 2
        assert after_reverse == 3  # nothing overwritten

    def test_audit_entry_names_the_acting_admin_and_the_transition(
        self, admin_client, admin_user, expense_factory
    ):
        expense = expense_factory(amount="9000.00")
        admin_client.post(
            review_url(expense), {"approve": False, "reviewNote": "duplicate"}, format="json"
        )

        entry = AuditLog.objects.filter(
            target_type="Expense", target_id=str(expense.pk), action=AuditLog.Action.REJECT
        ).get()

        assert entry.actor_id == admin_user.id
        assert entry.actor_email == admin_user.email
        assert entry.reason == "duplicate"
        assert entry.changes["status"] == {"from": "pending", "to": "rejected"}

    def test_review_stamp_reflects_the_latest_decision(
        self, admin_client, admin_user, expense_factory
    ):
        expense = expense_factory(amount="9000.00")
        admin_client.post(review_url(expense), {"approve": True}, format="json")
        expense.refresh_from_db()
        first_stamp = expense.reviewed_at

        admin_client.post(
            review_url(expense), {"approve": False, "reviewNote": "reversed"}, format="json"
        )
        expense.refresh_from_db()

        assert expense.reviewed_by_id == admin_user.id
        assert expense.reviewed_at >= first_stamp
        assert expense.review_note == "reversed"

    def test_manager_cannot_reverse(self, manager_client, admin_client, expense_factory):
        expense = expense_factory(amount="9000.00")
        admin_client.post(review_url(expense), {"approve": True}, format="json")

        assert manager_client.post(
            review_url(expense), {"approve": False, "reviewNote": "let me"}, format="json"
        ).status_code == 403


@pytest.mark.money
class TestSummaryStatusInclusion:
    """
    The rule reporting depends on. If this drifts from Net Revenue in docs/10,
    two screens stop reconciling.
    """

    def test_approved_counts(self, manager_client, expense_factory):
        expense_factory(amount="1000.00")  # auto-approved

        body = manager_client.get(SUMMARY_URL).json()
        assert Decimal(body["total"]) == Decimal("1000.00")

    def test_pending_counts_too(self, manager_client, expense_factory):
        """
        Pending is money that has already left the clinic — awaiting
        verification, not permission. Excluding it would overstate profit.
        """
        expense_factory(amount="9000.00")

        body = manager_client.get(SUMMARY_URL).json()
        assert Decimal(body["total"]) == Decimal("9000.00")

    def test_rejecting_removes_the_amount_from_the_total(
        self, manager_client, admin_client, expense_factory
    ):
        expense = expense_factory(amount="9000.00")
        before = Decimal(manager_client.get(SUMMARY_URL).json()["monthTotal"])

        admin_client.post(
            review_url(expense), {"approve": False, "reviewNote": "not ours"}, format="json"
        )
        after = Decimal(manager_client.get(SUMMARY_URL).json()["monthTotal"])

        assert before - after == Decimal("9000.00")

    def test_pending_amount_is_reported_separately(self, manager_client, expense_factory):
        expense_factory(amount="1000.00")  # approved
        expense_factory(amount="9000.00")  # pending

        body = manager_client.get(SUMMARY_URL).json()

        assert Decimal(body["total"]) == Decimal("10000.00")
        assert Decimal(body["pendingAmount"]) == Decimal("9000.00")
        assert Decimal(body["total"]) - Decimal(body["pendingAmount"]) == Decimal("1000.00")
        assert body["pendingCount"] == 1

    def test_voucher_count_includes_rejected(
        self, manager_client, admin_client, expense_factory
    ):
        """A rejected voucher still exists as a document, just not as a cost."""
        expense = expense_factory(amount="9000.00")
        admin_client.post(
            review_url(expense), {"approve": False, "reviewNote": "no"}, format="json"
        )

        body = manager_client.get(SUMMARY_URL).json()
        assert body["voucherCount"] == 1
        assert Decimal(body["total"]) == Decimal("0.00")


@pytest.mark.money
class TestSummaryPeriods:
    def test_todays_and_months_totals(self, manager_client, expense_factory):
        expense_factory(amount="1000.00")

        body = manager_client.get(SUMMARY_URL).json()
        assert Decimal(body["todayTotal"]) == Decimal("1000.00")
        assert Decimal(body["monthTotal"]) == Decimal("1000.00")

    def test_a_past_date_returns_that_days_total(self, manager_client, expense_factory):
        old = expense_factory(amount="2000.00")
        five_days_ago = timezone.now() - timedelta(days=5)
        Expense.objects.filter(pk=old.pk).update(created_at=five_days_ago)
        expense_factory(amount="700.00")  # today

        body = manager_client.get(
            SUMMARY_URL, {"date": timezone.localtime(five_days_ago).date().isoformat()}
        ).json()

        assert Decimal(body["todayTotal"]) == Decimal("2000.00")
        assert Decimal(body["total"]) == Decimal("2700.00")  # total is not date-bound

    def test_empty_branch_returns_zeros(self, other_manager_client):
        body = other_manager_client.get(SUMMARY_URL).json()

        assert Decimal(body["total"]) == Decimal("0.00")
        assert body["pendingCount"] == 0
        assert body["voucherCount"] == 0


class TestFilters:
    def test_filter_by_status(self, manager_client, expense_factory):
        expense_factory(amount="1000.00")  # approved
        expense_factory(amount="9000.00")  # pending

        assert manager_client.get(LIST_URL, {"status": "pending"}).json()["count"] == 1
        assert manager_client.get(LIST_URL, {"status": "approved"}).json()["count"] == 1

    def test_filter_by_category(self, manager_client, expense_factory):
        expense_factory(category=Expense.Category.RENT)
        expense_factory(category=Expense.Category.SUPPLIES)

        assert manager_client.get(LIST_URL, {"category": "rent"}).json()["count"] == 1

    def test_search_matches_code_description_and_payee(
        self, manager_client, expense_factory
    ):
        expense = expense_factory(description="Air conditioner service", paid_to="Cool Tech")

        assert manager_client.get(LIST_URL, {"search": "conditioner"}).json()["count"] == 1
        assert manager_client.get(LIST_URL, {"search": "Cool Tech"}).json()["count"] == 1
        assert manager_client.get(
            LIST_URL, {"search": expense.expense_code}
        ).json()["count"] == 1
        assert manager_client.get(LIST_URL, {"search": "nothing"}).json()["count"] == 0

    def test_filters_combine(self, manager_client, expense_factory):
        expense_factory(category=Expense.Category.RENT, amount="9000.00")  # pending
        expense_factory(category=Expense.Category.RENT, amount="900.00")  # approved

        body = manager_client.get(
            LIST_URL, {"category": "rent", "status": "pending"}
        ).json()
        assert body["count"] == 1

    def test_period_today_and_month(self, manager_client, expense_factory):
        old = expense_factory()
        Expense.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=40)
        )
        expense_factory()

        assert manager_client.get(LIST_URL, {"period": "today"}).json()["count"] == 1
        assert manager_client.get(LIST_URL, {"period": "month"}).json()["count"] == 1

    def test_date_overrides_period(self, manager_client, expense_factory):
        """
        The dashboard sends both. Honouring `period` would silently discard
        the user's explicit date choice.
        """
        old = expense_factory()
        three_days_ago = timezone.now() - timedelta(days=3)
        Expense.objects.filter(pk=old.pk).update(created_at=three_days_ago)
        expense_factory()

        body = manager_client.get(
            LIST_URL,
            {
                "period": "today",
                "date": timezone.localtime(three_days_ago).date().isoformat(),
            },
        ).json()

        assert body["count"] == 1
        assert body["results"][0]["id"] == old.pk


class TestValidationAndCodes:
    def test_code_format(self, expense_factory):
        code = expense_factory().expense_code
        year = timezone.localdate().year

        assert code.startswith(f"EXP-{year}-")
        assert len(code.rsplit("-", 1)[1]) == 5

    def test_codes_are_unique_across_many_creates(self, expense_factory):
        codes = {expense_factory().expense_code for _ in range(25)}
        assert len(codes) == 25

    def test_amounts_are_decimal_exact(self, manager_client):
        response = manager_client.post(LIST_URL, payload(amount="1234.56"), format="json")
        assert Decimal(response.json()["amount"]) == Decimal("1234.56")

    @pytest.mark.parametrize("amount", ["0", "0.00", "-1", "-500.00"])
    def test_non_positive_amounts_are_refused(self, manager_client, amount):
        assert manager_client.post(
            LIST_URL, payload(amount=amount), format="json"
        ).status_code == 400

    def test_invalid_category_is_refused(self, manager_client):
        assert manager_client.post(
            LIST_URL, payload(category="yacht"), format="json"
        ).status_code == 400

    def test_invalid_payment_method_is_refused(self, manager_client):
        assert manager_client.post(
            LIST_URL, payload(payment_method="barter"), format="json"
        ).status_code == 400

    @pytest.mark.parametrize("field", ["description", "paid_to", "category", "amount"])
    def test_required_fields(self, manager_client, field):
        data = payload()
        data.pop(field)

        assert manager_client.post(LIST_URL, data, format="json").status_code == 400

    def test_remarks_are_optional(self, manager_client):
        data = payload()
        data.pop("remarks", None)

        assert manager_client.post(LIST_URL, data, format="json").status_code == 201


class TestIdempotency:
    """Offline-first prerequisite (docs/00): a replayed submission must not double-record spend."""

    def test_replayed_idempotency_key_returns_the_same_expense(self, manager_client):
        data = payload(idempotency_key="exp-key-1")

        first = manager_client.post(LIST_URL, data, format="json")
        second = manager_client.post(LIST_URL, data, format="json")

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert Expense.objects.filter(description="Therapy room stationery").count() == 1


@pytest.mark.isolation
class TestExpenseBranchIsolation:
    def test_manager_list_excludes_other_branches(
        self, manager_client, expense_factory, other_manager, other_branch
    ):
        expense_factory()
        expense_factory(actor=other_manager, target_branch=other_branch)

        body = manager_client.get(LIST_URL).json()
        assert body["count"] == 1

    def test_manager_cannot_open_another_branchs_expense(
        self, manager_client, expense_factory, other_manager, other_branch
    ):
        foreign = expense_factory(actor=other_manager, target_branch=other_branch)

        assert manager_client.get(detail_url(foreign)).status_code == 404

    def test_manager_cannot_review_another_branchs_expense(
        self, manager_client, expense_factory, other_manager, other_branch
    ):
        foreign = expense_factory(
            actor=other_manager, target_branch=other_branch, amount="9000.00"
        )

        response = manager_client.post(review_url(foreign), {"approve": True}, format="json")

        assert response.status_code in {403, 404}
        foreign.refresh_from_db()
        assert foreign.status == Expense.Status.PENDING

    def test_manager_summary_excludes_other_branches(
        self, manager_client, expense_factory, other_manager, other_branch
    ):
        expense_factory(amount="1000.00")
        expense_factory(actor=other_manager, target_branch=other_branch, amount="9999.00")

        body = manager_client.get(SUMMARY_URL).json()
        assert Decimal(body["total"]) == Decimal("1000.00")

    def test_admin_sees_every_branch(
        self, admin_client, expense_factory, other_manager, other_branch
    ):
        expense_factory(amount="1000.00")
        expense_factory(actor=other_manager, target_branch=other_branch, amount="2000.00")

        assert admin_client.get(LIST_URL).json()["count"] == 2
        assert Decimal(admin_client.get(SUMMARY_URL).json()["total"]) == Decimal("3000.00")

    def test_admin_can_narrow_to_one_branch(
        self, admin_client, branch, expense_factory, other_manager, other_branch
    ):
        expense_factory(amount="1000.00")
        expense_factory(actor=other_manager, target_branch=other_branch, amount="2000.00")

        body = admin_client.get(SUMMARY_URL, {"branch": str(branch.id)}).json()
        assert Decimal(body["total"]) == Decimal("1000.00")
