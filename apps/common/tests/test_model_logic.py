"""
Model property tests — still no database.

These exercise the balance and status logic on unsaved instances. It's where
the partial-refund rules actually live, and where an off-by-one on a due date
or a wrong comparison silently misstates what a patient owes.

Kept DB-free so they run before the database exists and stay fast forever.
"""

from datetime import date
from decimal import Decimal

import pytest

from apps.branches.models import Branch
from apps.enrollments.models import BillStatus, MonthlyBill
from apps.expenses.models import Expense
from apps.materials.models import Material
from apps.payments.models import Payment, PaymentStatus
from apps.services.models import Service


def bill(amount="5000.00", paid="0.00", status=BillStatus.DUE, due=date(2026, 8, 5), paid_at=None):
    """An unsaved bill — enough for the balance logic, no database needed."""
    return MonthlyBill(
        amount=Decimal(amount),
        amount_paid=Decimal(paid),
        status=status,
        due_date=due,
        paid_at=paid_at,
        month="2026-08",
        label="August 2026",
    )


class TestOutstandingBalance:
    """
    Outstanding is `amount − amount_paid`, never `amount`.

    Partial refunds mean a bill can hold a partial balance. Summing `amount`
    for unpaid bills overstates what the patient owes — the exact
    overstatement bug the docs call out.
    """

    def test_nothing_paid(self):
        assert bill(amount="5000.00", paid="0.00").outstanding == Decimal("5000.00")

    def test_partially_paid(self):
        assert bill(amount="5000.00", paid="3000.00").outstanding == Decimal("2000.00")

    def test_fully_paid(self):
        assert bill(amount="5000.00", paid="5000.00").outstanding == Decimal("0.00")

    def test_partial_refund_scenario(self):
        """
        A 5,000 bill paid in full, then 2,000 refunded: amount_paid drops to
        3,000 and 2,000 is owed again — not the original 5,000.
        """
        b = bill(amount="5000.00", paid="3000.00", status=BillStatus.DUE)
        assert b.outstanding == Decimal("2000.00")

    def test_overpayment_never_goes_negative(self):
        """A negative balance would subtract from other patients' dues."""
        assert bill(amount="5000.00", paid="6000.00").outstanding == Decimal("0.00")

    def test_written_off_owes_nothing(self):
        """
        Write-off is a deliberate, audited decision to forgive the money, so
        it must drop out of Outstanding Due entirely.
        """
        b = bill(amount="5000.00", paid="0.00", status=BillStatus.WRITTEN_OFF)
        assert b.outstanding == Decimal("0.00")

    def test_is_settled_thresholds(self):
        assert bill(paid="5000.00").is_settled is True
        assert bill(paid="4999.99").is_settled is False
        assert bill(paid="5000.01").is_settled is True


class TestOverdueDetection:
    """
    Overdue is derived from the due date, never stored — a stored flag goes
    stale the moment a date passes with nothing writing to the row.
    """

    def test_unpaid_after_due_date_is_overdue(self):
        assert bill(due=date(2026, 8, 5)).is_overdue(on=date(2026, 8, 6)) is True

    def test_unpaid_before_due_date_is_not(self):
        assert bill(due=date(2026, 8, 5)).is_overdue(on=date(2026, 8, 4)) is False

    def test_on_the_due_date_itself_is_not_overdue(self):
        """The 5th is the deadline — you have that whole day to pay."""
        assert bill(due=date(2026, 8, 5)).is_overdue(on=date(2026, 8, 5)) is False

    def test_paid_bill_is_never_overdue(self):
        b = bill(paid="5000.00", status=BillStatus.PAID)
        assert b.is_overdue(on=date(2027, 1, 1)) is False

    def test_settled_by_amount_is_not_overdue_even_if_status_lags(self):
        """Balance is the truth; a stale status field must not create a false overdue."""
        b = bill(paid="5000.00", status=BillStatus.DUE)
        assert b.is_overdue(on=date(2027, 1, 1)) is False

    def test_written_off_is_not_overdue(self):
        b = bill(status=BillStatus.WRITTEN_OFF)
        assert b.is_overdue(on=date(2027, 1, 1)) is False

    def test_partially_paid_still_overdue(self):
        b = bill(paid="3000.00", due=date(2026, 8, 5))
        assert b.is_overdue(on=date(2026, 9, 1)) is True


class TestEffectiveStatus:
    """What the UI shows, with overdue applied on read."""

    def test_due_before_deadline(self):
        assert bill(due=date(2026, 8, 5)).effective_status(on=date(2026, 8, 1)) == BillStatus.DUE

    def test_due_becomes_overdue_after_deadline(self):
        assert (
            bill(due=date(2026, 8, 5)).effective_status(on=date(2026, 8, 10))
            == BillStatus.OVERDUE
        )

    def test_upcoming_stays_upcoming_before_its_date(self):
        b = bill(status=BillStatus.UPCOMING, due=date(2026, 10, 5))
        assert b.effective_status(on=date(2026, 8, 10)) == BillStatus.UPCOMING

    def test_paid_stays_paid(self):
        b = bill(paid="5000.00", status=BillStatus.PAID)
        assert b.effective_status(on=date(2027, 1, 1)) == BillStatus.PAID

    def test_written_off_stays_written_off(self):
        b = bill(status=BillStatus.WRITTEN_OFF)
        assert b.effective_status(on=date(2027, 1, 1)) == BillStatus.WRITTEN_OFF

    def test_settled_by_balance_reports_paid(self):
        b = bill(paid="5000.00", status=BillStatus.DUE)
        assert b.effective_status(on=date(2026, 8, 1)) == BillStatus.PAID


class TestPaymentRevenueRule:
    """Only `paid` is revenue — getting this wrong inflates every figure."""

    @pytest.mark.parametrize(
        "status,counts",
        [
            (PaymentStatus.PAID, True),
            (PaymentStatus.REFUNDED, False),
            (PaymentStatus.VOID, False),
            (PaymentStatus.PARTIAL, False),
            (PaymentStatus.CANCELLED, False),
            (PaymentStatus.DUE, False),
        ],
    )
    def test_revenue_inclusion(self, status, counts):
        assert Payment(amount=Decimal("100.00"), status=status).counts_as_revenue is counts


class TestExpenseTotalsRule:
    """
    Approved and pending both count; rejected does not.

    Pending is money that has already left the clinic awaiting verification,
    not permission — this records spending after the fact. Excluding it would
    understate costs and overstate profit.
    """

    @pytest.mark.parametrize(
        "status,counts",
        [
            (Expense.Status.APPROVED, True),
            (Expense.Status.PENDING, True),
            (Expense.Status.REJECTED, False),
        ],
    )
    def test_totals_inclusion(self, status, counts):
        assert Expense(amount=Decimal("100.00"), status=status).counts_toward_totals is counts


class TestServiceDiscount:
    def test_discounted_when_original_exceeds_fee(self):
        service = Service(fee=Decimal("12600.00"), original_fee=Decimal("14000.00"))
        assert service.is_discounted is True

    def test_not_discounted_without_an_original_fee(self):
        assert Service(fee=Decimal("12600.00"), original_fee=None).is_discounted is False

    def test_not_discounted_when_equal(self):
        service = Service(fee=Decimal("14000.00"), original_fee=Decimal("14000.00"))
        assert service.is_discounted is False

    def test_not_discounted_when_original_is_lower(self):
        """A lower "original" is a data error, not a discount to advertise."""
        service = Service(fee=Decimal("14000.00"), original_fee=Decimal("12000.00"))
        assert service.is_discounted is False


class TestMaterialStock:
    def test_low_stock_at_reorder_level(self):
        assert Material(quantity=5, reorder_level=5, unit_cost=Decimal("1")).is_low_stock is True

    def test_low_stock_below_reorder_level(self):
        assert Material(quantity=2, reorder_level=5, unit_cost=Decimal("1")).is_low_stock is True

    def test_not_low_above_reorder_level(self):
        assert Material(quantity=6, reorder_level=5, unit_cost=Decimal("1")).is_low_stock is False

    def test_zero_stock_is_low(self):
        assert Material(quantity=0, reorder_level=0, unit_cost=Decimal("1")).is_low_stock is True

    def test_stock_value_is_exact(self):
        material = Material(quantity=20, unit_cost=Decimal("250.00"))
        assert material.stock_value == Decimal("5000.00")

    def test_stock_value_with_paisa(self):
        material = Material(quantity=3, unit_cost=Decimal("333.33"))
        assert material.stock_value == Decimal("999.99")


class TestBranchShortCode:
    """
    Drives per-branch receipt and patient series (RCPT-DHK-2026-00001), which
    is what makes offline issuance collision-free.
    """

    def test_extracts_middle_segment(self):
        assert Branch(code="BR-DHK-001").short_code == "DHK"
        assert Branch(code="BR-CTG-001").short_code == "CTG"

    def test_falls_back_to_whole_code_when_unusual(self):
        assert Branch(code="SINGLE").short_code == "SINGLE"

    def test_handles_extra_segments(self):
        assert Branch(code="BR-SYL-001-X").short_code == "SYL"

    def test_branch_active_flag(self):
        assert Branch(status=Branch.Status.ACTIVE).is_active is True
        assert Branch(status=Branch.Status.INACTIVE).is_active is False
