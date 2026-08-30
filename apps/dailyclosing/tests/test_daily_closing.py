"""
Daily closing tests — follows the required-test list in docs/09.

This module is a control, not a feature. The tests that matter most are the
ones proving a manager cannot make a short drawer disappear:

  * `system_total` is derived from Payments, never accepted from the body;
  * a closing is append-only — no PUT, PATCH or DELETE, for anyone;
  * amendment is Admin-only and preserves the original figure.

If any of those three fail, the whole reconciliation is theatre.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.models import AuditLog
from apps.dailyclosing import services
from apps.dailyclosing.models import DailyClosing
from apps.payments import services as payment_services
from apps.payments.models import Payment, PaymentStatus, RefundRequest

pytestmark = pytest.mark.django_db

LIST_URL = reverse("dailyclosing:daily-closing-list")
SUMMARY_URL = reverse("dailyclosing:daily-closing-today-summary")
HISTORY_URL = reverse("dailyclosing:daily-closing-history")


def amend_url(closing):
    return reverse("dailyclosing:daily-closing-amend", args=[closing.pk])


def detail_url(closing):
    return reverse("dailyclosing:daily-closing-detail", args=[closing.pk])


@pytest.fixture
def pay(db, manager, branch, patient_factory):
    """Records a real payment through the service, so codes and status are real."""

    def _pay(amount, *, method="cash", actor=None, target_branch=None, patient=None, **kwargs):
        payment, _ = payment_services.create_payment(
            actor=actor or manager,
            branch=target_branch or branch,
            patient=patient or patient_factory(branch=target_branch or branch),
            amount=Decimal(amount),
            method=method,
            **kwargs,
        )
        return payment

    return _pay


def move_to(payment, when):
    """Backdate a payment without going through save() hooks."""
    Payment.all_objects.filter(pk=payment.pk).update(created_at=when)
    payment.refresh_from_db()
    return payment


def refund_fully(payment, *, requester, approver, reason="Session cancelled"):
    """
    Put a payment through the full two-step refund control.

    A refund is never a single call by design — a manager requests, an admin
    approves — so tests that need a refunded payment have to go through both
    steps rather than flipping the status directly.
    """
    request = payment_services.request_refund(
        actor=requester, payment=payment, amount=payment.amount, reason=reason
    )
    payment_services.approve_refund(
        actor=approver, request=request, bill_action=RefundRequest.BillAction.REOPEN
    )
    payment.refresh_from_db()
    return request


@pytest.mark.money
class TestSystemTotal:
    def test_sums_only_the_target_days_payments(self, manager_client, pay):
        move_to(pay("1000.00"), timezone.now() - timedelta(days=1))
        pay("2500.00")

        body = manager_client.get(SUMMARY_URL).json()
        assert Decimal(body["total"]) == Decimal("2500.00")

    def test_a_payment_just_before_local_midnight_belongs_to_the_previous_day(
        self, manager_client, pay
    ):
        """
        Day boundaries are Asia/Dhaka boundaries. Comparing in UTC would pull
        late-evening payments into the wrong day — six hours of every day's
        takings landing on tomorrow's closing.
        """
        today = timezone.localdate()
        last_night = timezone.make_aware(
            datetime.combine(today - timedelta(days=1), time.min)
        ) + timedelta(hours=23, minutes=59)
        move_to(pay("800.00"), last_night)
        pay("1200.00")

        today_total = Decimal(manager_client.get(SUMMARY_URL).json()["total"])
        yesterday_total = Decimal(
            manager_client.get(
                SUMMARY_URL, {"date": (today - timedelta(days=1)).isoformat()}
            ).json()["total"]
        )

        assert today_total == Decimal("1200.00")
        assert yesterday_total == Decimal("800.00")

    def test_a_payment_just_after_local_midnight_belongs_to_today(
        self, manager_client, pay
    ):
        today = timezone.localdate()
        just_after_midnight = timezone.make_aware(
            datetime.combine(today, time.min)
        ) + timedelta(minutes=1)
        move_to(pay("650.00"), just_after_midnight)

        assert Decimal(manager_client.get(SUMMARY_URL).json()["total"]) == Decimal("650.00")

    def test_transaction_count_matches_what_was_summed(self, manager_client, pay):
        pay("100.00")
        pay("200.00")
        pay("300.00")

        body = manager_client.get(SUMMARY_URL).json()
        assert body["transactionCount"] == 3
        assert Decimal(body["total"]) == Decimal("600.00")

    def test_by_method_groups_and_sums_to_the_total(self, manager_client, pay):
        pay("1000.00", method="cash")
        pay("500.00", method="cash")
        pay("2000.00", method="bkash")

        body = manager_client.get(SUMMARY_URL).json()
        by_method = {row["method"]: Decimal(row["amount"]) for row in body["byMethod"]}

        assert by_method == {"cash": Decimal("1500.00"), "bkash": Decimal("2000.00")}
        assert sum(by_method.values()) == Decimal(body["total"])

    def test_a_day_with_no_payments_is_zero_not_an_error(self, manager_client):
        body = manager_client.get(SUMMARY_URL).json()

        assert Decimal(body["total"]) == Decimal("0.00")
        assert body["transactionCount"] == 0
        assert body["byMethod"] == []

    def test_totals_are_decimal_exact_across_many_payments(self, manager_client, pay):
        """
        Thirty payments of 0.10 must be exactly 3.00. In float this is 2.9999…
        and the drawer looks short by a hundredth for no reason.
        """
        for _ in range(30):
            pay("0.10")

        assert Decimal(manager_client.get(SUMMARY_URL).json()["total"]) == Decimal("3.00")

    @pytest.mark.isolation
    def test_another_branchs_payments_never_contribute(
        self, manager_client, pay, other_manager, other_branch
    ):
        pay("1000.00")
        pay("9999.00", actor=other_manager, target_branch=other_branch)

        assert Decimal(manager_client.get(SUMMARY_URL).json()["total"]) == Decimal("1000.00")


@pytest.mark.money
class TestAdjustments:
    """
    Refunds and voids are excluded from the reconciliation figure but shown
    beside it. A manager staring at a short drawer needs to see why.
    """

    def test_a_refunded_payment_is_excluded_from_the_total(
        self, manager_client, manager, admin_user, branch, pay
    ):
        pay("1000.00")
        refunded = pay("3000.00")
        refund_fully(refunded, requester=manager, approver=admin_user, reason="Session cancelled")

        body = manager_client.get(SUMMARY_URL).json()

        assert Decimal(body["total"]) == Decimal("1000.00")
        assert body["transactionCount"] == 1

    def test_a_refunded_payment_is_absent_from_by_method(
        self, manager_client, manager, admin_user, pay
    ):
        refunded = pay("3000.00", method="bkash")
        refund_fully(refunded, requester=manager, approver=admin_user, reason="cancelled")

        body = manager_client.get(SUMMARY_URL).json()
        assert all(row["method"] != "bkash" for row in body["byMethod"])

    def test_a_void_payment_is_excluded_from_all_three(self, manager_client, manager, pay):
        pay("1000.00")
        voided = pay("500.00")
        payment_services.void_payment(actor=manager, payment=voided, reason="Wrong patient")

        body = manager_client.get(SUMMARY_URL).json()

        assert Decimal(body["total"]) == Decimal("1000.00")
        assert body["transactionCount"] == 1
        assert sum(Decimal(row["amount"]) for row in body["byMethod"]) == Decimal("1000.00")

    def test_refunds_and_voids_are_itemised_with_enough_to_identify_them(
        self, manager_client, manager, admin_user, pay
    ):
        voided = pay("500.00")
        payment_services.void_payment(actor=manager, payment=voided, reason="Wrong patient")
        refunded = pay("3000.00", method="bkash")
        refund_fully(refunded, requester=manager, approver=admin_user, reason="cancelled")

        items = manager_client.get(SUMMARY_URL).json()["adjustments"]["items"]
        by_receipt = {item["receiptNumber"]: item for item in items}

        assert len(items) == 2
        assert by_receipt[voided.receipt_number]["status"] == PaymentStatus.VOID
        assert by_receipt[refunded.receipt_number]["status"] == PaymentStatus.REFUNDED
        assert by_receipt[refunded.receipt_number]["method"] == "bkash"
        assert Decimal(by_receipt[refunded.receipt_number]["amount"]) == Decimal("3000.00")
        assert by_receipt[refunded.receipt_number]["patientName"]

    def test_refunded_and_void_totals_are_independent(
        self, manager_client, manager, admin_user, pay
    ):
        voided = pay("500.00")
        payment_services.void_payment(actor=manager, payment=voided, reason="wrong")
        refunded = pay("3000.00")
        refund_fully(refunded, requester=manager, approver=admin_user, reason="cancelled")

        adjustments = manager_client.get(SUMMARY_URL).json()["adjustments"]

        assert Decimal(adjustments["voidTotal"]) == Decimal("500.00")
        assert Decimal(adjustments["refundedTotal"]) == Decimal("3000.00")

    def test_a_clean_day_reports_empty_adjustments_not_an_error(self, manager_client, pay):
        pay("1000.00")

        adjustments = manager_client.get(SUMMARY_URL).json()["adjustments"]

        assert adjustments["items"] == []
        assert Decimal(adjustments["refundedTotal"]) == Decimal("0.00")
        assert Decimal(adjustments["voidTotal"]) == Decimal("0.00")

    @pytest.mark.isolation
    def test_adjustments_are_branch_scoped(
        self, manager_client, other_manager, other_branch, pay
    ):
        foreign = pay("700.00", actor=other_manager, target_branch=other_branch)
        payment_services.void_payment(actor=other_manager, payment=foreign, reason="wrong")

        adjustments = manager_client.get(SUMMARY_URL).json()["adjustments"]
        assert adjustments["items"] == []

    def test_closing_system_total_matches_the_transactions_summary_on_a_clean_day(
        self, manager_client, pay
    ):
        """
        Two screens, one number -- on a day with nothing to reconcile away.

        The two totals answer different questions in general: closing is cash
        in the drawer (excludes a same-day refund entirely, since no net cash
        arrived), reporting is period revenue (keeps that same payment in its
        collection month and books the refund separately by approval date).
        They are only guaranteed to agree when there is no refund or void to
        tell them apart, which is exactly what this test seeds.
        """
        pay("1000.00")
        pay("2500.00", method="bkash")

        closing_total = Decimal(manager_client.get(SUMMARY_URL).json()["total"])
        reporting_total = Decimal(
            manager_client.get(reverse("reporting:summary")).json()["todayCollected"]
        )

        assert closing_total == reporting_total == Decimal("3500.00")


@pytest.mark.money
class TestStatusCalculation:
    def test_matching_count_is_matched(self, manager_client, pay):
        pay("2500.00")

        body = manager_client.post(LIST_URL, {"actualTotal": "2500.00"}, format="json").json()

        assert body["status"] == DailyClosing.Status.MATCHED
        assert Decimal(body["difference"]) == Decimal("0.00")

    def test_extra_cash_is_over(self, manager_client, pay):
        pay("2500.00")

        body = manager_client.post(LIST_URL, {"actualTotal": "2600.00"}, format="json").json()

        assert body["status"] == DailyClosing.Status.OVER
        assert Decimal(body["difference"]) == Decimal("100.00")

    def test_missing_cash_is_short(self, manager_client, pay):
        pay("2500.00")

        body = manager_client.post(LIST_URL, {"actualTotal": "2300.00"}, format="json").json()

        assert body["status"] == DailyClosing.Status.SHORT
        assert Decimal(body["difference"]) == Decimal("-200.00")

    def test_the_difference_is_decimal_exact(self, manager_client, pay):
        """2300.50 counted as 2300.00 is exactly −0.50, not −0.4999999."""
        pay("2300.50")

        body = manager_client.post(LIST_URL, {"actualTotal": "2300.00"}, format="json").json()

        assert Decimal(body["difference"]) == Decimal("-0.50")

    def test_a_zero_collection_day_is_matched_at_zero(self, manager_client):
        body = manager_client.post(LIST_URL, {"actualTotal": "0"}, format="json").json()

        assert body["status"] == DailyClosing.Status.MATCHED
        assert Decimal(body["systemTotal"]) == Decimal("0.00")


class TestServerSideComputation:
    """
    Everything except the counted cash is derived. Accepting any of it from
    the body would let the screen declare a day balanced when it didn't.
    """

    def test_a_posted_system_total_is_ignored(self, manager_client, pay):
        pay("2500.00")

        body = manager_client.post(
            LIST_URL, {"actualTotal": "2500.00", "systemTotal": "999999.00"}, format="json"
        ).json()

        assert Decimal(body["systemTotal"]) == Decimal("2500.00")

    def test_a_posted_difference_and_status_are_ignored(self, manager_client, pay):
        """The attack: count 2,000 against 2,500 and claim it matched."""
        pay("2500.00")

        body = manager_client.post(
            LIST_URL,
            {"actualTotal": "2000.00", "difference": "0.00", "status": "matched"},
            format="json",
        ).json()

        assert body["status"] == DailyClosing.Status.SHORT
        assert Decimal(body["difference"]) == Decimal("-500.00")

    def test_zero_counted_against_a_positive_system_total_is_short(
        self, manager_client, pay
    ):
        pay("1500.00")

        body = manager_client.post(LIST_URL, {"actualTotal": "0"}, format="json").json()

        assert body["status"] == DailyClosing.Status.SHORT
        assert Decimal(body["difference"]) == Decimal("-1500.00")

    @pytest.mark.parametrize("value", [None, "", "abc", "-100.00"])
    def test_a_missing_or_invalid_actual_total_is_refused(self, manager_client, value):
        data = {} if value is None else {"actualTotal": value}

        assert manager_client.post(LIST_URL, data, format="json").status_code == 400

    def test_submitted_by_comes_from_the_token(self, manager_client, manager):
        body = manager_client.post(LIST_URL, {"actualTotal": "0"}, format="json").json()
        assert body["submittedBy"] == manager.name


class TestOnePerDay:
    def test_a_second_submission_for_the_same_day_is_refused(self, manager_client):
        manager_client.post(LIST_URL, {"actualTotal": "100.00"}, format="json")

        response = manager_client.post(LIST_URL, {"actualTotal": "500.00"}, format="json")

        assert response.status_code == 400
        assert response.json()["code"] == "already_submitted"

    def test_the_second_submission_does_not_overwrite_the_first(self, manager_client):
        manager_client.post(LIST_URL, {"actualTotal": "100.00"}, format="json")
        manager_client.post(LIST_URL, {"actualTotal": "500.00"}, format="json")

        closing = DailyClosing.objects.get()
        assert closing.actual_total == Decimal("100.00")

    def test_another_branch_may_close_the_same_day(
        self, manager_client, other_manager_client
    ):
        manager_client.post(LIST_URL, {"actualTotal": "100.00"}, format="json")

        response = other_manager_client.post(LIST_URL, {"actualTotal": "200.00"}, format="json")

        assert response.status_code == 201

    def test_the_same_branch_may_close_a_different_day(self, manager_client):
        manager_client.post(LIST_URL, {"actualTotal": "100.00"}, format="json")

        response = manager_client.post(
            LIST_URL,
            {
                "actualTotal": "200.00",
                "date": (timezone.localdate() - timedelta(days=1)).isoformat(),
            },
            format="json",
        )

        assert response.status_code == 201


class TestSubmissionPermissions:
    def test_admin_cannot_submit_a_closing(self, admin_client):
        """
        Admin didn't handle the cash. The frontend renders the screen
        read-only for them; that has to hold server-side too.
        """
        assert admin_client.post(
            LIST_URL, {"actualTotal": "100.00"}, format="json"
        ).status_code == 403

    def test_admin_must_name_a_branch_for_the_summary(self, admin_client):
        assert admin_client.get(SUMMARY_URL).status_code == 400

    def test_admin_can_read_any_branchs_summary(self, admin_client, branch, pay):
        pay("1000.00")

        body = admin_client.get(SUMMARY_URL, {"branch": str(branch.id)}).json()
        assert Decimal(body["total"]) == Decimal("1000.00")

    @pytest.mark.isolation
    def test_the_branch_comes_from_the_token_not_the_body(
        self, manager_client, manager, other_branch, pay
    ):
        pay("1000.00")

        body = manager_client.post(
            LIST_URL, {"actualTotal": "1000.00", "branch": str(other_branch.id)}, format="json"
        ).json()

        assert body["branchId"] == str(manager.branch_id)


class TestHistory:
    def test_newest_first(self, manager_client, manager, branch):
        for offset in (2, 0, 1):
            services.submit_closing(
                actor=manager, branch=branch, actual_total=Decimal("100.00"),
                day=timezone.localdate() - timedelta(days=offset),
            )

        dates = [row["date"] for row in manager_client.get(HISTORY_URL).json()]
        assert dates == sorted(dates, reverse=True)

    def test_every_rendered_field_is_present(self, manager_client, pay):
        pay("2500.00")
        manager_client.post(LIST_URL, {"actualTotal": "2400.00"}, format="json")

        row = manager_client.get(HISTORY_URL).json()[0]

        assert Decimal(row["systemTotal"]) == Decimal("2500.00")
        assert Decimal(row["actualTotal"]) == Decimal("2400.00")
        assert Decimal(row["difference"]) == Decimal("-100.00")
        assert row["status"] == DailyClosing.Status.SHORT
        assert row["date"] and row["submittedBy"] and row["submittedAt"]

    def test_empty_history_is_an_empty_list(self, manager_client):
        assert manager_client.get(HISTORY_URL).json() == []

    @pytest.mark.isolation
    def test_history_excludes_other_branches(
        self, manager_client, other_manager_client
    ):
        other_manager_client.post(LIST_URL, {"actualTotal": "200.00"}, format="json")
        manager_client.post(LIST_URL, {"actualTotal": "100.00"}, format="json")

        rows = manager_client.get(HISTORY_URL).json()
        assert len(rows) == 1
        assert Decimal(rows[0]["actualTotal"]) == Decimal("100.00")

    def test_admin_sees_every_branch_and_can_narrow(
        self, admin_client, manager_client, other_manager_client, branch
    ):
        manager_client.post(LIST_URL, {"actualTotal": "100.00"}, format="json")
        other_manager_client.post(LIST_URL, {"actualTotal": "200.00"}, format="json")

        assert len(admin_client.get(HISTORY_URL).json()) == 2
        assert len(admin_client.get(HISTORY_URL, {"branch": str(branch.id)}).json()) == 1


class TestImmutability:
    """
    The control itself. A closing that can be edited is not a closing.
    """

    @pytest.fixture
    def closing(self, manager_client, pay):
        pay("2500.00")
        manager_client.post(LIST_URL, {"actualTotal": "2300.00"}, format="json")
        return DailyClosing.objects.get()

    def test_manager_cannot_edit_in_place(self, manager_client, closing):
        assert manager_client.patch(
            detail_url(closing), {"actualTotal": "2500.00"}, format="json"
        ).status_code == 405

    def test_admin_cannot_edit_in_place_either(self, admin_client, closing):
        assert admin_client.patch(
            detail_url(closing), {"actualTotal": "2500.00"}, format="json"
        ).status_code == 405
        assert admin_client.put(
            detail_url(closing), {"actualTotal": "2500.00"}, format="json"
        ).status_code == 405

    def test_nobody_can_delete_a_closing(self, manager_client, admin_client, closing):
        assert manager_client.delete(detail_url(closing)).status_code == 405
        assert admin_client.delete(detail_url(closing)).status_code == 405


class TestAmendment:
    @pytest.fixture
    def closing(self, manager_client, pay):
        pay("2500.00")
        manager_client.post(LIST_URL, {"actualTotal": "2300.00"}, format="json")
        return DailyClosing.objects.get()

    def test_manager_cannot_amend(self, manager_client, closing):
        """
        The single control that makes daily closing meaningful. A manager who
        could amend could count short, then correct it themselves.
        """
        response = manager_client.post(
            amend_url(closing),
            {"correctedActualTotal": "2500.00", "reason": "recounted"},
            format="json",
        )

        assert response.status_code == 403
        closing.refresh_from_db()
        assert closing.actual_total == Decimal("2300.00")

    def test_admin_amends_and_the_status_is_recomputed(self, admin_client, closing):
        body = admin_client.post(
            amend_url(closing),
            {"correctedActualTotal": "2500.00", "reason": "Note found under the drawer"},
            format="json",
        ).json()

        assert Decimal(body["actualTotal"]) == Decimal("2500.00")
        assert Decimal(body["difference"]) == Decimal("0.00")
        assert body["status"] == DailyClosing.Status.MATCHED

    def test_a_posted_status_or_difference_is_ignored_on_amendment(
        self, admin_client, closing
    ):
        body = admin_client.post(
            amend_url(closing),
            {
                "correctedActualTotal": "2000.00",
                "reason": "recount",
                "status": "matched",
                "difference": "0.00",
            },
            format="json",
        ).json()

        assert body["status"] == DailyClosing.Status.SHORT
        assert Decimal(body["difference"]) == Decimal("-500.00")

    @pytest.mark.parametrize("reason", ["", "   "])
    def test_amending_without_a_reason_is_refused(self, admin_client, closing, reason):
        response = admin_client.post(
            amend_url(closing),
            {"correctedActualTotal": "2500.00", "reason": reason},
            format="json",
        )

        assert response.status_code == 400
        closing.refresh_from_db()
        assert closing.actual_total == Decimal("2300.00")

    def test_the_original_figure_survives_on_the_amendment_row(
        self, admin_client, closing
    ):
        """
        The point of the whole design: both numbers are recoverable. An
        eraser would leave no evidence a correction ever happened.
        """
        body = admin_client.post(
            amend_url(closing),
            {"correctedActualTotal": "2500.00", "reason": "recounted"},
            format="json",
        ).json()

        amendment = body["amendments"][0]
        assert Decimal(amendment["previousActualTotal"]) == Decimal("2300.00")
        assert Decimal(amendment["correctedActualTotal"]) == Decimal("2500.00")
        assert amendment["reason"] == "recounted"

    def test_system_total_is_not_amendable(self, admin_client, closing):
        """
        Derived from Payments. Changing it would rewrite history rather than
        correct a miscount.
        """
        body = admin_client.post(
            amend_url(closing),
            {
                "correctedActualTotal": "2500.00",
                "reason": "recounted",
                "systemTotal": "1.00",
            },
            format="json",
        ).json()

        assert Decimal(body["systemTotal"]) == Decimal("2500.00")

    def test_is_amended_flips_only_after_an_amendment(self, admin_client, closing):
        before = admin_client.get(detail_url(closing)).json()
        assert before["isAmended"] is False
        assert before["amendments"] == []

        admin_client.post(
            amend_url(closing),
            {"correctedActualTotal": "2500.00", "reason": "recounted"},
            format="json",
        )

        assert admin_client.get(detail_url(closing)).json()["isAmended"] is True

    def test_two_amendments_are_both_kept_in_order(self, admin_client, closing):
        admin_client.post(
            amend_url(closing),
            {"correctedActualTotal": "2400.00", "reason": "first recount"},
            format="json",
        )
        body = admin_client.post(
            amend_url(closing),
            {"correctedActualTotal": "2500.00", "reason": "second recount"},
            format="json",
        ).json()

        amendments = body["amendments"]
        assert len(amendments) == 2
        assert [a["reason"] for a in amendments] == ["first recount", "second recount"]
        assert Decimal(amendments[0]["previousActualTotal"]) == Decimal("2300.00")
        assert Decimal(amendments[1]["previousActualTotal"]) == Decimal("2400.00")
        assert Decimal(body["actualTotal"]) == Decimal("2500.00")

    def test_each_amendment_names_the_acting_admin(
        self, admin_client, admin_user, closing
    ):
        body = admin_client.post(
            amend_url(closing),
            {"correctedActualTotal": "2500.00", "reason": "recounted"},
            format="json",
        ).json()

        assert body["amendments"][0]["amendedBy"] == admin_user.name
        assert body["amendments"][0]["amendedAt"]

    def test_an_amendment_writes_an_audit_entry(self, admin_client, admin_user, closing):
        admin_client.post(
            amend_url(closing),
            {"correctedActualTotal": "2500.00", "reason": "Note found under the drawer"},
            format="json",
        )

        entry = AuditLog.objects.filter(
            target_type="DailyClosing",
            target_id=str(closing.pk),
            action=AuditLog.Action.AMEND,
        ).get()

        assert entry.actor_id == admin_user.id
        assert entry.reason == "Note found under the drawer"
        assert entry.changes["actual_total"] == {"from": "2300.00", "to": "2500.00"}

    def test_amendments_appear_in_history(self, admin_client, manager_client, closing):
        admin_client.post(
            amend_url(closing),
            {"correctedActualTotal": "2500.00", "reason": "recounted"},
            format="json",
        )

        row = manager_client.get(HISTORY_URL).json()[0]
        assert row["isAmended"] is True
        assert len(row["amendments"]) == 1

    @pytest.mark.isolation
    def test_manager_cannot_reach_another_branchs_closing(
        self, manager_client, other_manager_client
    ):
        other_manager_client.post(LIST_URL, {"actualTotal": "200.00"}, format="json")
        foreign = DailyClosing.objects.get()

        assert manager_client.get(detail_url(foreign)).status_code == 404
