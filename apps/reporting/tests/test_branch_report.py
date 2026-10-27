"""
The branch report — the payload behind the PDF a Manager hands to the Admin.

What these guard:

1. **It agrees with the Summary screen.** The money half is `branch_summary`
   unchanged; a test pins that, because the whole reason for composing rather
   than re-deriving is that a report and a screen must never disagree about a
   figure someone is about to be asked to explain.
2. **The adjustments are there.** Refunds and voids are what make the takings
   differ from the sum of receipts. Without them an Admin adding the list up
   gets a different number and has no way to find out why.
3. **Every receipt is listed**, and only the ones that count — a voided
   payment belongs in the adjustments, not in the day's collection.
4. **Branch isolation**, as everywhere.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.payments import services as payment_services
from apps.reporting import services

pytestmark = pytest.mark.django_db

URL = reverse("reporting:branch-report")


@pytest.fixture
def paid(manager, branch, patient_factory):
    """Three collections today, two methods."""
    def _pay(amount, method="cash", category="daily", patient=None):
        payment, _ = payment_services.create_payment(
            actor=manager, branch=branch, patient=patient or patient_factory(),
            amount=Decimal(amount), method=method, category=category,
        )
        return payment

    return _pay


def today():
    return timezone.localdate().isoformat()


class TestItAgreesWithTheSummaryScreen:
    def test_the_money_figures_match_branch_summary(
        self, manager_client, manager, branch, paid
    ):
        paid("800.00")
        paid("1200.00", method="bkash")

        report = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()
        summary = manager_client.get(
            reverse("reporting:branch-summary"), {"dateFrom": today(), "dateTo": today()}
        ).json()

        # Compared as values, not as text. This endpoint stringifies its
        # decimals (money is never a float here); the older reporting views
        # return the service dict unserialized, so the same figure arrives
        # from them as a JSON float. The numbers must agree -- the wire
        # format of the older endpoints is a separate matter.
        for field in (
            "grossCollected", "refunded", "expenses", "netRevenue", "outstandingDue",
        ):
            assert Decimal(str(report[field])) == Decimal(str(summary[field])), field
        for field in ("paymentCount", "newPatients"):
            assert report[field] == summary[field], field
        assert [row["method"] for row in report["byMethod"]] == [
            row["method"] for row in summary["byMethod"]
        ]

    def test_it_carries_every_section(self, manager_client, paid):
        paid("800.00")

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        for section in (
            "expensesByCategory", "refunds", "voids", "attendance", "services",
            "materialsSold", "staffAttendance", "enquiries", "payments",
        ):
            assert section in body, section


class TestTheReceiptList:
    def test_every_collection_is_listed_with_its_receipt(self, manager_client, paid):
        one = paid("800.00")
        two = paid("1200.00", method="bkash")

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        receipts = [row["receiptNumber"] for row in body["payments"]]
        assert receipts == [one.receipt_number, two.receipt_number]
        assert body["payments"][0]["amount"] == "800.00"
        assert body["payments"][0]["collectedBy"]

    def test_the_list_sums_to_the_collected_total(self, manager_client, paid):
        """An Admin adding the list up must land on the headline figure."""
        paid("800.00")
        paid("1200.00", method="bkash")
        paid("500.00")

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        listed = sum(Decimal(row["amount"]) for row in body["payments"])
        assert listed == Decimal(body["grossCollected"])

    def test_a_voided_payment_leaves_the_list_and_appears_as_an_adjustment(
        self, manager_client, manager, paid
    ):
        kept = paid("800.00")
        scrapped = paid("1200.00")
        payment_services.void_payment(
            actor=manager, payment=scrapped, reason="rang up twice"
        )

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert [row["receiptNumber"] for row in body["payments"]] == [kept.receipt_number]
        assert [row["receiptNumber"] for row in body["voids"]] == [
            scrapped.receipt_number
        ]
        # And the headline no longer counts it.
        assert Decimal(body["grossCollected"]) == Decimal("800.00")


class TestTheOtherSections:
    def test_expenses_split_by_category_and_approval(
        self, manager_client, manager, branch
    ):
        from apps.expenses import services as expense_services

        expense_services.create_expense(
            actor=manager, branch=branch,
            data={
                "category": "supplies", "amount": Decimal("9000.00"),
                "description": "Cards", "paid_to": "Shop", "payment_method": "cash",
            },
        )

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert body["expensesByCategory"][0]["category"] == "supplies"
        assert Decimal(body["expensesByCategory"][0]["amount"]) == Decimal("9000.00")
        # Large enough to need review, so it lands in pending, not approved.
        assert Decimal(body["expensesPending"]) == Decimal("9000.00")

    def test_enquiries_are_counted_by_source(self, manager_client, manager, branch):
        from apps.enquiries import services as enquiry_services

        enquiry_services.create_enquiry(
            actor=manager, branch=branch,
            data={"caller_name": "Shirin", "caller_phone": "01712345678",
                  "source": "facebook"},
        )

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert body["enquiries"]["total"] == 1
        assert body["enquiries"]["bySource"] == [{"source": "facebook", "count": 1}]

    def test_materials_sold_are_listed(
        self, manager_client, manager, branch, patient_factory, material_factory
    ):
        from apps.materials import services as material_services

        item = material_factory(name="Flashcards", quantity=10, selling_price=Decimal("350.00"))
        material_services.sell_materials(
            actor=manager, branch=branch, patient=patient_factory(),
            items=[{"material_id": item.id, "quantity": 2}], method="cash",
        )

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert body["materialsSold"] == [
            {"name": "Flashcards", "quantity": 2, "amount": "700.00"}
        ]


class TestRangeAndScope:
    def test_yesterday_is_excluded_from_todays_report(self, manager_client, paid):
        old = paid("800.00")
        from apps.payments.models import Payment

        Payment.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        paid("1200.00")

        body = manager_client.get(URL, {"dateFrom": today(), "dateTo": today()}).json()

        assert len(body["payments"]) == 1
        assert Decimal(body["grossCollected"]) == Decimal("1200.00")

    def test_a_backwards_range_is_refused(self, manager_client):
        response = manager_client.get(
            URL, {"dateFrom": today(), "dateTo": "2020-01-01"}
        )

        assert response.status_code == 400

    def test_it_defaults_to_today(self, manager_client, paid):
        paid("800.00")

        body = manager_client.get(URL).json()

        assert body["dateFrom"] == today()
        assert body["dateTo"] == today()


@pytest.mark.isolation
class TestBranchIsolation:
    def test_a_manager_sees_only_their_own_branch(
        self, other_manager_client, paid
    ):
        paid("800.00")

        body = other_manager_client.get(
            URL, {"dateFrom": today(), "dateTo": today()}
        ).json()

        assert body["payments"] == []
        assert Decimal(body["grossCollected"]) == Decimal("0.00")

    def test_admin_can_narrow_to_one_branch(
        self, admin_client, branch, other_branch, paid
    ):
        paid("800.00")

        mine = admin_client.get(
            URL, {"branch": branch.id, "dateFrom": today(), "dateTo": today()}
        ).json()
        theirs = admin_client.get(
            URL, {"branch": other_branch.id, "dateFrom": today(), "dateTo": today()}
        ).json()

        assert Decimal(mine["grossCollected"]) == Decimal("800.00")
        assert Decimal(theirs["grossCollected"]) == Decimal("0.00")
