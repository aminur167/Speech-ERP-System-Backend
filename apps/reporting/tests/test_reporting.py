"""
Reporting tests — follows the required-test list in docs/10.

The centre of gravity here is period attribution, because it is the rule that
looks right and is wrong:

    A payment collected in August and refunded in September has
    `status = "refunded"` today. A naive `status == "paid"` filter therefore
    removes it from AUGUST as well — silently rewriting a month that was
    already reconciled, closed and reported.

The confirmed rule is that a payment counts as revenue in its own month, and
the refund is a separate negative event dated to its approval. Void is the
deliberate exception: a voided payment never happened, so it leaves its
original day entirely — which is exactly why voids are same-day-only.

Those two behaviours are tested side by side on purpose, so the distinction
can't drift.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.expenses import services as expense_services
from apps.payments import services as payment_services
from apps.payments.models import Payment, PaymentCategory, PaymentStatus, RefundRequest

pytestmark = pytest.mark.django_db

TRANSACTIONS_URL = reverse("reporting:transaction-list")
SUMMARY_URL = reverse("reporting:summary")
TREND_URL = reverse("reporting:trend")
BY_METHOD_URL = reverse("reporting:by-method")
BY_CATEGORY_URL = reverse("reporting:by-category")
METRICS_URL = reverse("reporting:dashboard-metrics")
COLLECTION_URL = reverse("reporting:collection-for-date")
REFUNDS_VOIDS_URL = reverse("reporting:refunds-voids")
NET_REVENUE_URL = reverse("reporting:net-revenue")


@pytest.fixture
def pay(db, manager, branch, patient_factory):
    def _pay(
        amount, *, method="cash", category="", actor=None, target_branch=None,
        patient=None, when=None,
    ):
        payment, _ = payment_services.create_payment(
            actor=actor or manager,
            branch=target_branch or branch,
            patient=patient or patient_factory(branch=target_branch or branch),
            amount=Decimal(amount),
            method=method,
            category=category,
        )
        if when is not None:
            Payment.all_objects.filter(pk=payment.pk).update(created_at=when)
            payment.refresh_from_db()
        return payment

    return _pay


def refund_fully(payment, *, requester, approver, approved_at=None, reason="Cancelled"):
    """
    The full two-step control: manager requests, admin approves.

    `approved_at` backdates the approval, which is what makes period
    attribution testable — the refund event's date is what decides which
    month absorbs it.
    """
    request = payment_services.request_refund(
        actor=requester, payment=payment, amount=payment.amount, reason=reason
    )
    payment_services.approve_refund(
        actor=approver, request=request, bill_action=RefundRequest.BillAction.REOPEN
    )
    if approved_at is not None:
        RefundRequest.objects.filter(pk=request.pk).update(reviewed_at=approved_at)
        request.refresh_from_db()
    payment.refresh_from_db()
    return request


def local_midnight(day: date):
    return timezone.make_aware(datetime.combine(day, time.min))


@pytest.mark.money
class TestRevenueExclusionRules:
    def test_a_void_payment_leaves_every_revenue_figure(
        self, manager_client, manager, pay
    ):
        pay("1000.00", method="cash", category=PaymentCategory.MONTHLY)
        voided = pay("400.00", method="bkash", category=PaymentCategory.DAILY)
        payment_services.void_payment(actor=manager, payment=voided, reason="Wrong patient")

        summary = manager_client.get(SUMMARY_URL).json()
        by_method = {r["method"]: Decimal(r["amount"]) for r in manager_client.get(BY_METHOD_URL).json()}
        by_category = {
            r["category"]: Decimal(r["amount"])
            for r in manager_client.get(BY_CATEGORY_URL).json()
        }
        trend_today = Decimal(manager_client.get(TREND_URL).json()[-1]["amount"])

        assert Decimal(summary["totalCollected"]) == Decimal("1000.00")
        assert Decimal(summary["todayCollected"]) == Decimal("1000.00")
        assert Decimal(summary["monthCollected"]) == Decimal("1000.00")
        assert "bkash" not in by_method
        assert "daily" not in by_category
        assert trend_today == Decimal("1000.00")

    def test_refunded_and_void_payments_still_appear_in_the_record(
        self, manager_client, manager, admin_user, pay
    ):
        """
        Excluded from revenue is not the same as deleted. The transaction list
        is the record of what happened.
        """
        voided = pay("400.00")
        payment_services.void_payment(actor=manager, payment=voided, reason="duplicate")
        refunded = pay("3000.00")
        refund_fully(refunded, requester=manager, approver=admin_user)

        listed = {
            row["receiptNumber"]
            for row in manager_client.get(TRANSACTIONS_URL).json()["results"]
        }
        flagged = {row["receiptNumber"] for row in manager_client.get(REFUNDS_VOIDS_URL).json()}

        assert {voided.receipt_number, refunded.receipt_number} <= listed
        assert {voided.receipt_number, refunded.receipt_number} == flagged

    def test_a_seeded_mix_produces_the_exact_expected_revenue(
        self, manager_client, manager, admin_user, pay
    ):
        pay("1000.00")
        pay("2500.00")
        refunded = pay("3000.00")
        refund_fully(refunded, requester=manager, approver=admin_user)
        voided = pay("400.00")
        payment_services.void_payment(actor=manager, payment=voided, reason="duplicate")

        summary = manager_client.get(SUMMARY_URL).json()

        # Paid 3,500 + the refunded 3,000, which stays in its own period.
        # Only the 400 void is gone.
        assert Decimal(summary["totalCollected"]) == Decimal("6500.00")
        assert summary["transactionCount"] == 3


@pytest.mark.money
class TestRefundPeriodAttribution:
    """
    The rule that is easy to get wrong. Every test here fails against a naive
    `status == "paid"` filter.
    """

    @pytest.fixture
    def august_payment_refunded_in_september(self, manager, admin_user, pay):
        august = pay("5000.00", when=local_midnight(date(2026, 8, 12)) + timedelta(hours=11))
        refund_fully(
            august,
            requester=manager,
            approver=admin_user,
            approved_at=local_midnight(date(2026, 9, 9)) + timedelta(hours=15),
        )
        return august

    def test_august_still_counts_a_payment_refunded_in_september(
        self, manager_client, august_payment_refunded_in_september
    ):
        body = manager_client.get(SUMMARY_URL, {"date": "2026-08-31"}).json()

        assert Decimal(body["monthCollected"]) == Decimal("5000.00")

    def test_the_refund_lands_in_september(
        self, manager_client, august_payment_refunded_in_september
    ):
        body = manager_client.get(SUMMARY_URL, {"date": "2026-09-30"}).json()

        assert Decimal(body["monthRefunded"]) == Decimal("5000.00")

    def test_august_reports_no_refund(
        self, manager_client, august_payment_refunded_in_september
    ):
        body = manager_client.get(SUMMARY_URL, {"date": "2026-08-31"}).json()

        assert Decimal(body["monthRefunded"]) == Decimal("0.00")

    def test_septembers_net_revenue_absorbs_the_refund(
        self, manager_client, august_payment_refunded_in_september
    ):
        body = manager_client.get(NET_REVENUE_URL, {"date": "2026-09-30"}).json()

        assert Decimal(body["refunded"]) == Decimal("5000.00")
        assert Decimal(body["netRevenue"]) == Decimal("-5000.00")

    def test_augusts_net_revenue_is_untouched_by_the_september_refund(
        self, manager_client, august_payment_refunded_in_september
    ):
        body = manager_client.get(NET_REVENUE_URL, {"date": "2026-08-31"}).json()

        assert Decimal(body["grossCollected"]) == Decimal("5000.00")
        assert Decimal(body["refunded"]) == Decimal("0.00")
        assert Decimal(body["netRevenue"]) == Decimal("5000.00")

    def test_a_closed_period_returns_the_identical_figure_afterwards(
        self, manager_client, manager, admin_user, pay
    ):
        """
        The closed-period stability guarantee, asserted as a before/after on
        the same query rather than against a hardcoded number.
        """
        august = pay("5000.00", when=local_midnight(date(2026, 8, 12)) + timedelta(hours=11))
        before = manager_client.get(NET_REVENUE_URL, {"date": "2026-08-31"}).json()

        refund_fully(
            august,
            requester=manager,
            approver=admin_user,
            approved_at=local_midnight(date(2026, 9, 9)) + timedelta(hours=15),
        )
        after = manager_client.get(NET_REVENUE_URL, {"date": "2026-08-31"}).json()

        assert before == after

    def test_a_same_month_collect_and_refund_nets_to_zero(
        self, manager_client, manager, admin_user, pay
    ):
        payment = pay("5000.00", when=local_midnight(date(2026, 8, 12)) + timedelta(hours=11))
        refund_fully(
            payment,
            requester=manager,
            approver=admin_user,
            approved_at=local_midnight(date(2026, 8, 20)) + timedelta(hours=11),
        )

        body = manager_client.get(NET_REVENUE_URL, {"date": "2026-08-31"}).json()

        assert Decimal(body["grossCollected"]) == Decimal("5000.00")
        assert Decimal(body["refunded"]) == Decimal("5000.00")
        assert Decimal(body["netRevenue"]) == Decimal("0.00")

    def test_a_void_by_contrast_leaves_its_own_period_entirely(
        self, manager_client, manager, pay
    ):
        """
        The contrast that pins the distinction down. A refund keeps the
        original revenue and books a negative event later; a void erases the
        transaction from the day it was taken. Voids are same-day-only
        precisely so this can never disturb a closed period.
        """
        payment = pay("5000.00")
        payment_services.void_payment(actor=manager, payment=payment, reason="never happened")

        body = manager_client.get(NET_REVENUE_URL).json()

        assert Decimal(body["grossCollected"]) == Decimal("0.00")
        assert Decimal(body["refunded"]) == Decimal("0.00")


@pytest.mark.money
class TestSummary:
    def test_today_and_month_are_scoped_correctly(self, manager_client, pay):
        today = timezone.localdate()
        pay("1000.00")
        pay("700.00", when=local_midnight(today.replace(day=1)) - timedelta(days=5))

        body = manager_client.get(SUMMARY_URL).json()

        assert Decimal(body["todayCollected"]) == Decimal("1000.00")
        assert Decimal(body["monthCollected"]) == Decimal("1000.00")
        assert Decimal(body["totalCollected"]) == Decimal("1700.00")

    def test_a_past_date_shifts_both_figures(self, manager_client, pay):
        target = timezone.localdate() - timedelta(days=40)
        pay("700.00", when=local_midnight(target) + timedelta(hours=10))
        pay("1000.00")

        body = manager_client.get(SUMMARY_URL, {"date": target.isoformat()}).json()

        assert Decimal(body["todayCollected"]) == Decimal("700.00")
        assert Decimal(body["monthCollected"]) == Decimal("700.00")

    def test_by_method_sums_to_the_total_and_is_sorted_descending(
        self, manager_client, pay
    ):
        pay("1000.00", method="cash")
        pay("500.00", method="cash")
        pay("2000.00", method="bkash")

        rows = manager_client.get(SUMMARY_URL).json()["byMethod"]
        amounts = [Decimal(r["amount"]) for r in rows]

        assert amounts == sorted(amounts, reverse=True)
        assert sum(amounts) == Decimal("3500.00")

    def test_totals_are_decimal_exact(self, manager_client, pay):
        for _ in range(30):
            pay("0.10")

        assert Decimal(manager_client.get(SUMMARY_URL).json()["totalCollected"]) == Decimal(
            "3.00"
        )

    def test_an_empty_branch_returns_zeros_not_errors(self, other_manager_client):
        body = other_manager_client.get(SUMMARY_URL).json()

        assert Decimal(body["totalCollected"]) == Decimal("0.00")
        assert body["transactionCount"] == 0
        assert body["byMethod"] == []


class TestTrend:
    def test_returns_exactly_the_requested_number_of_buckets_oldest_first(
        self, manager_client
    ):
        rows = manager_client.get(TREND_URL, {"days": "7"}).json()

        assert len(rows) == 7
        assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
        assert rows[-1]["date"] == timezone.localdate().isoformat()

    def test_a_day_with_no_collection_is_zero_rather_than_missing(
        self, manager_client, pay
    ):
        """
        Omitting empty days would make a chart with gaps read as a shorter
        period rather than a quiet one.
        """
        pay("1000.00")

        rows = manager_client.get(TREND_URL, {"days": "7"}).json()

        assert len(rows) == 7
        assert Decimal(rows[0]["amount"]) == Decimal("0.00")
        assert Decimal(rows[-1]["amount"]) == Decimal("1000.00")

    def test_buckets_split_at_local_midnight(self, manager_client, pay):
        yesterday = timezone.localdate() - timedelta(days=1)
        pay("800.00", when=local_midnight(yesterday) + timedelta(hours=23, minutes=59))
        pay("1200.00", when=local_midnight(timezone.localdate()) + timedelta(minutes=1))

        rows = {r["date"]: Decimal(r["amount"]) for r in manager_client.get(TREND_URL).json()}

        assert rows[yesterday.isoformat()] == Decimal("800.00")
        assert rows[timezone.localdate().isoformat()] == Decimal("1200.00")

    def test_void_payments_are_excluded_from_buckets(self, manager_client, manager, pay):
        pay("1000.00")
        voided = pay("400.00")
        payment_services.void_payment(actor=manager, payment=voided, reason="duplicate")

        rows = manager_client.get(TREND_URL).json()
        assert Decimal(rows[-1]["amount"]) == Decimal("1000.00")

    def test_the_bucket_count_is_bounded(self, manager_client):
        """A client asking for a year of daily buckets gets 90, not 365."""
        assert len(manager_client.get(TREND_URL, {"days": "365"}).json()) == 90


class TestByCategory:
    def test_material_sales_are_excluded(self, manager_client, pay):
        """
        Materials are goods, not services. Mixing them into the service
        breakdown would make the therapy mix unreadable.
        """
        pay("1000.00", category=PaymentCategory.MONTHLY)
        pay("2500.00", category=PaymentCategory.MATERIAL_SALE)

        categories = {r["category"] for r in manager_client.get(BY_CATEGORY_URL).json()}

        assert "material_sale" not in categories
        assert "monthly" in categories

    def test_each_category_totals_correctly(self, manager_client, pay):
        pay("1000.00", category=PaymentCategory.MONTHLY)
        pay("500.00", category=PaymentCategory.MONTHLY)
        pay("2000.00", category=PaymentCategory.DAILY)

        rows = {r["category"]: Decimal(r["amount"]) for r in manager_client.get(BY_CATEGORY_URL).json()}

        assert rows["monthly"] == Decimal("1500.00")
        assert rows["daily"] == Decimal("2000.00")

    def test_only_the_reference_month_is_included(self, manager_client, pay):
        old = timezone.localdate() - timedelta(days=45)
        pay("9000.00", category=PaymentCategory.MONTHLY, when=local_midnight(old))
        pay("1000.00", category=PaymentCategory.MONTHLY)

        rows = {r["category"]: Decimal(r["amount"]) for r in manager_client.get(BY_CATEGORY_URL).json()}

        assert rows["monthly"] == Decimal("1000.00")

    def test_uncategorised_payments_are_omitted(self, manager_client, pay):
        pay("1000.00", category="")

        assert manager_client.get(BY_CATEGORY_URL).json() == []


class TestDashboardMetrics:
    def test_patients_seen_counts_people_not_payments(
        self, manager_client, patient_factory, pay
    ):
        """
        A patient paying three times today is one patient seen. Counting rows
        would inflate the headline number by however many times someone
        happened to pay.
        """
        patient = patient_factory(name="Repeat Visitor")
        pay("100.00", patient=patient)
        pay("200.00", patient=patient)
        pay("300.00", patient=patient)
        pay("400.00")

        body = manager_client.get(METRICS_URL).json()
        assert body["todayPatientsSeen"] == 2

    def test_due_collected_counts_only_monthly_and_installment(
        self, manager_client, pay
    ):
        pay("1000.00", category=PaymentCategory.MONTHLY)
        pay("2000.00", category=PaymentCategory.INSTALLMENT)
        pay("500.00", category=PaymentCategory.DAILY)
        pay("700.00", category=PaymentCategory.MATERIAL_SALE)

        body = manager_client.get(METRICS_URL).json()
        assert Decimal(body["todayDueCollected"]) == Decimal("3000.00")

    def test_a_date_shifts_both_metrics(self, manager_client, patient_factory, pay):
        target = timezone.localdate() - timedelta(days=10)
        pay(
            "1500.00",
            category=PaymentCategory.MONTHLY,
            when=local_midnight(target) + timedelta(hours=12),
        )
        pay("9000.00", category=PaymentCategory.MONTHLY)

        body = manager_client.get(METRICS_URL, {"date": target.isoformat()}).json()

        assert body["todayPatientsSeen"] == 1
        assert Decimal(body["todayDueCollected"]) == Decimal("1500.00")

    def test_collection_for_date_returns_that_days_figure(self, manager_client, pay):
        target = timezone.localdate() - timedelta(days=3)
        pay("2200.00", when=local_midnight(target) + timedelta(hours=9))
        pay("9000.00")

        body = manager_client.get(COLLECTION_URL, {"date": target.isoformat()}).json()

        assert body["date"] == target.isoformat()
        assert Decimal(body["amount"]) == Decimal("2200.00")


class TestTransactionList:
    def test_newest_first(self, manager_client, pay):
        pay("100.00", when=timezone.now() - timedelta(hours=3))
        pay("200.00", when=timezone.now() - timedelta(hours=1))

        rows = manager_client.get(TRANSACTIONS_URL).json()["results"]
        assert [Decimal(r["amount"]) for r in rows] == [Decimal("200.00"), Decimal("100.00")]

    def test_search_matches_patient_name(self, manager_client, patient_factory, pay):
        pay("100.00", patient=patient_factory(name="Rakib Hasan"))
        pay("200.00")

        body = manager_client.get(TRANSACTIONS_URL, {"search": "rakib"}).json()
        assert body["count"] == 1

    def test_search_matches_patient_code_receipt_and_transaction_id(
        self, manager_client, patient_factory, pay
    ):
        patient = patient_factory(patient_code="PT-DHK-2026-09999")
        payment = pay("100.00", patient=patient)
        pay("200.00")

        for term in (
            patient.patient_code,
            payment.receipt_number,
            payment.transaction_id,
        ):
            body = manager_client.get(TRANSACTIONS_URL, {"search": term}).json()
            assert body["count"] == 1, term

    def test_method_status_and_patient_filters_each_narrow(
        self, manager_client, manager, patient_factory, pay
    ):
        patient = patient_factory()
        pay("100.00", method="cash", patient=patient)
        voided = pay("200.00", method="bkash")
        payment_services.void_payment(actor=manager, payment=voided, reason="duplicate")

        assert manager_client.get(TRANSACTIONS_URL, {"method": "cash"}).json()["count"] == 1
        assert manager_client.get(
            TRANSACTIONS_URL, {"status": PaymentStatus.VOID}
        ).json()["count"] == 1
        assert manager_client.get(
            TRANSACTIONS_URL, {"patientId": str(patient.id)}
        ).json()["count"] == 1

    def test_filters_combine(self, manager_client, patient_factory, pay):
        patient = patient_factory()
        pay("100.00", method="cash", patient=patient)
        pay("200.00", method="bkash", patient=patient)

        body = manager_client.get(
            TRANSACTIONS_URL, {"patientId": str(patient.id), "method": "bkash"}
        ).json()
        assert body["count"] == 1

    def test_date_overrides_period(self, manager_client, pay):
        target = timezone.localdate() - timedelta(days=4)
        pay("700.00", when=local_midnight(target) + timedelta(hours=10))
        pay("1000.00")

        body = manager_client.get(
            TRANSACTIONS_URL, {"period": "today", "date": target.isoformat()}
        ).json()

        assert body["count"] == 1
        assert Decimal(body["results"][0]["amount"]) == Decimal("700.00")

    def test_patient_name_and_code_are_joined_onto_each_row(
        self, manager_client, patient_factory, pay
    ):
        patient = patient_factory(name="Sadia Islam", patient_code="PT-DHK-2026-00777")
        pay("100.00", patient=patient)

        row = manager_client.get(TRANSACTIONS_URL).json()["results"][0]

        assert row["patientName"] == "Sadia Islam"
        assert row["patientCode"] == "PT-DHK-2026-00777"

    def test_a_soft_deleted_patient_still_renders(
        self, manager_client, patient_factory, pay
    ):
        """
        A payment outlives the patient record being retired. The history must
        still render rather than 500 — the money moved either way.
        """
        patient = patient_factory(name="Departed Patient")
        pay("100.00", patient=patient)
        patient.delete()  # soft

        response = manager_client.get(TRANSACTIONS_URL)

        assert response.status_code == 200
        assert response.json()["results"][0]["patientName"] == "Departed Patient"

    def test_pagination_count_reflects_the_filter_not_the_table(
        self, manager_client, patient_factory, pay
    ):
        patient = patient_factory()
        pay("100.00", patient=patient)
        for _ in range(15):
            pay("50.00")

        body = manager_client.get(
            TRANSACTIONS_URL, {"patientId": str(patient.id)}
        ).json()

        assert body["count"] == 1
        assert len(body["results"]) == 1

    def test_page_size_is_honoured(self, manager_client, pay):
        for _ in range(8):
            pay("50.00")

        body = manager_client.get(TRANSACTIONS_URL, {"pageSize": "3"}).json()

        assert body["count"] == 8
        assert len(body["results"]) == 3


@pytest.mark.money
class TestNetRevenue:
    def test_collected_minus_refunds_minus_expenses(
        self, manager_client, manager, branch, pay
    ):
        pay("10000.00")
        expense_services.create_expense(
            actor=manager, branch=branch,
            data={
                "category": "rent", "amount": Decimal("3000.00"),
                "description": "Rent", "paid_to": "Landlord", "payment_method": "cash",
            },
        )

        body = manager_client.get(NET_REVENUE_URL).json()

        assert Decimal(body["grossCollected"]) == Decimal("10000.00")
        assert Decimal(body["expenses"]) == Decimal("3000.00")
        assert Decimal(body["netRevenue"]) == Decimal("7000.00")

    def test_pending_expenses_count_the_same_as_approved(
        self, manager_client, manager, branch, pay
    ):
        """
        The identical rule the expenses module uses. If these two diverge, the
        Reports page and the Expenses page stop agreeing on the same month.
        """
        pay("10000.00")
        expense_services.create_expense(
            actor=manager, branch=branch,
            data={
                "category": "equipment", "amount": Decimal("9000.00"),  # over threshold
                "description": "Audiometer", "paid_to": "MedTech",
                "payment_method": "bank_transfer",
            },
        )

        reported = Decimal(manager_client.get(NET_REVENUE_URL).json()["expenses"])
        from_expenses_screen = Decimal(
            manager_client.get(reverse("expenses:expense-summary")).json()["monthTotal"]
        )

        assert reported == Decimal("9000.00")
        assert reported == from_expenses_screen

    def test_a_rejected_expense_is_excluded_from_both_screens(
        self, manager_client, admin_client, manager, branch, pay
    ):
        pay("10000.00")
        expense = expense_services.create_expense(
            actor=manager, branch=branch,
            data={
                "category": "other", "amount": Decimal("9000.00"),
                "description": "Disputed", "paid_to": "Vendor", "payment_method": "cash",
            },
        )
        admin_client.post(
            reverse("expenses:expense-review", args=[expense.pk]),
            {"approve": False, "reviewNote": "not a clinic cost"},
            format="json",
        )

        body = manager_client.get(NET_REVENUE_URL).json()
        assert Decimal(body["expenses"]) == Decimal("0.00")
        assert Decimal(body["netRevenue"]) == Decimal("10000.00")

    def test_a_negative_net_is_returned_not_clamped(
        self, manager_client, manager, branch, pay
    ):
        """
        A loss-making month is information, not an error state. Clamping to
        zero would hide exactly the situation the report exists to surface.
        """
        pay("1000.00")
        expense_services.create_expense(
            actor=manager, branch=branch,
            data={
                "category": "salaries", "amount": Decimal("40000.00"),
                "description": "Payroll", "paid_to": "Staff",
                "payment_method": "bank_transfer",
            },
        )

        body = manager_client.get(NET_REVENUE_URL).json()
        assert Decimal(body["netRevenue"]) == Decimal("-39000.00")


@pytest.mark.isolation
class TestReportingBranchIsolation:
    """
    Asserted on every endpoint in the module, not only the list — a single
    unscoped aggregate leaks one branch's takings to another's manager just as
    effectively as an unscoped list.
    """

    @pytest.fixture
    def seeded(self, manager, other_manager, other_branch, branch, pay):
        pay("1000.00", category=PaymentCategory.MONTHLY)
        pay(
            "9999.00",
            category=PaymentCategory.MONTHLY,
            actor=other_manager,
            target_branch=other_branch,
        )

    def test_transaction_list(self, manager_client, seeded):
        assert manager_client.get(TRANSACTIONS_URL).json()["count"] == 1

    def test_summary(self, manager_client, seeded):
        body = manager_client.get(SUMMARY_URL).json()
        assert Decimal(body["totalCollected"]) == Decimal("1000.00")

    def test_trend(self, manager_client, seeded):
        rows = manager_client.get(TREND_URL).json()
        assert Decimal(rows[-1]["amount"]) == Decimal("1000.00")

    def test_by_method(self, manager_client, seeded):
        rows = manager_client.get(BY_METHOD_URL).json()
        assert sum(Decimal(r["amount"]) for r in rows) == Decimal("1000.00")

    def test_by_category(self, manager_client, seeded):
        rows = manager_client.get(BY_CATEGORY_URL).json()
        assert sum(Decimal(r["amount"]) for r in rows) == Decimal("1000.00")

    def test_dashboard_metrics(self, manager_client, seeded):
        body = manager_client.get(METRICS_URL).json()
        assert body["todayPatientsSeen"] == 1
        assert Decimal(body["todayDueCollected"]) == Decimal("1000.00")

    def test_collection_for_date(self, manager_client, seeded):
        body = manager_client.get(COLLECTION_URL).json()
        assert Decimal(body["amount"]) == Decimal("1000.00")

    def test_refunds_and_voids(
        self, manager_client, other_manager, other_branch, pay
    ):
        foreign = pay("700.00", actor=other_manager, target_branch=other_branch)
        payment_services.void_payment(actor=other_manager, payment=foreign, reason="wrong")

        assert manager_client.get(REFUNDS_VOIDS_URL).json() == []

    def test_net_revenue(self, manager_client, seeded):
        body = manager_client.get(NET_REVENUE_URL).json()
        assert Decimal(body["grossCollected"]) == Decimal("1000.00")

    def test_admin_sees_every_branch(self, admin_client, seeded):
        body = admin_client.get(SUMMARY_URL).json()
        assert Decimal(body["totalCollected"]) == Decimal("10999.00")

    def test_admin_can_narrow_to_one_branch(self, admin_client, branch, seeded):
        body = admin_client.get(SUMMARY_URL, {"branch": str(branch.id)}).json()
        assert Decimal(body["totalCollected"]) == Decimal("1000.00")

    def test_admin_narrowing_applies_to_every_endpoint(
        self, admin_client, other_branch, seeded
    ):
        params = {"branch": str(other_branch.id)}

        assert admin_client.get(TRANSACTIONS_URL, params).json()["count"] == 1
        assert Decimal(
            admin_client.get(NET_REVENUE_URL, params).json()["grossCollected"]
        ) == Decimal("9999.00")
        assert Decimal(
            admin_client.get(COLLECTION_URL, params).json()["amount"]
        ) == Decimal("9999.00")


class TestQueryBudgets:
    """
    Guards against N+1 regressions. The bounds are deliberately loose — the
    point is that adding a row must not add a query, not that the current
    count is optimal.
    """

    def test_transaction_list_does_not_scale_queries_with_rows(
        self, manager_client, django_assert_max_num_queries, patient_factory, pay
    ):
        for _ in range(10):
            pay("100.00", patient=patient_factory())

        with django_assert_max_num_queries(8):
            manager_client.get(TRANSACTIONS_URL, {"pageSize": "10"})

    def test_refunds_and_voids_joins_patients_in_one_pass(
        self, manager_client, django_assert_max_num_queries, manager, pay
    ):
        for _ in range(10):
            voided = pay("100.00")
            payment_services.void_payment(actor=manager, payment=voided, reason="test")

        with django_assert_max_num_queries(6):
            manager_client.get(REFUNDS_VOIDS_URL)

    @pytest.mark.parametrize(
        "url", [SUMMARY_URL, TREND_URL, BY_METHOD_URL, BY_CATEGORY_URL, METRICS_URL]
    )
    def test_each_dashboard_aggregate_is_a_bounded_number_of_queries(
        self, manager_client, django_assert_max_num_queries, pay, url
    ):
        for _ in range(10):
            pay("100.00", category=PaymentCategory.MONTHLY)

        with django_assert_max_num_queries(10):
            manager_client.get(url)
