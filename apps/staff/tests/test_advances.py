"""
Salary advances — money paid early, recovered from a later month.

The approval chain is SalaryPayment's, deliberately: Manager asks, Admin
approves, Manager disburses, and the Expense is written at the moment the
money actually leaves. What is new here is a balance that outlives the
request, and that is where the interesting rules are:

1. **Recovery is written at disbursement, not at request.** The deduction is
   frozen onto the salary request so Admin approves a figure that cannot
   move — but a request that is rejected or still pending has handed over
   nothing, and recording a repayment against it would write off a debt the
   clinic never collected.
2. **Two open salary requests cannot deduct the same advance twice.** They
   both read the same untouched `recovered_amount`, because recovery has not
   happened yet; `_recoverable_now` subtracts what other open requests have
   already promised.
3. **A month can only give back what it holds.** The rest stays outstanding.
   Expense cannot be negative and nobody can be asked to hand salary back.
"""

from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.expenses.models import Expense
from apps.staff import services
from apps.staff.models import SalaryPayment, StaffAdvance

pytestmark = pytest.mark.django_db


@pytest.fixture
def farhana(staff_member_factory):
    return staff_member_factory(name="Farhana Rahman", monthly_salary=Decimal("30000.00"))


def this_month():
    today = timezone.localdate()
    return f"{today.year}-{today.month:02d}"


def request_url(staff):
    return reverse("staff:staffmember-request-advance", args=[staff.id])


def advance_url(advance, action):
    return reverse(f"staff:staffadvance-{action}", args=[advance.id])


def salary_request_url(staff):
    return reverse("staff:staffmember-request-salary-payment", args=[staff.id])


def paid_advance(manager, admin, farhana, amount="10000.00"):
    """Walk one advance all the way through to PAID."""
    advance = services.request_advance(
        actor=manager, staff=farhana, amount=Decimal(amount), reason="Medical emergency"
    )
    services.review_advance(actor=admin, advance=advance, approve=True)
    services.disburse_advance(actor=manager, advance=advance, payment_method="cash")
    advance.refresh_from_db()
    return advance


@pytest.mark.money
class TestRequestingOne:
    def test_a_manager_can_request_one(self, manager_client, farhana):
        response = manager_client.post(
            request_url(farhana),
            {"amount": "5000.00", "reason": "Medical emergency"},
            format="json",
        )

        assert response.status_code == 201
        body = response.json()
        assert Decimal(body["amount"]) == Decimal("5000.00")
        assert body["status"] == StaffAdvance.Status.PENDING_APPROVAL
        assert Decimal(body["outstanding"]) == Decimal("5000.00")

    def test_a_reason_is_required(self, manager_client, farhana):
        """Unlike a salary amount, somebody *chose* this — an audit will ask why."""
        response = manager_client.post(
            request_url(farhana), {"amount": "5000.00", "reason": "  "}, format="json"
        )

        assert response.status_code == 400
        assert StaffAdvance.objects.count() == 0

    def test_several_can_be_open_at_once(self, manager, farhana):
        services.request_advance(
            actor=manager, staff=farhana, amount=Decimal("5000.00"), reason="One"
        )
        services.request_advance(
            actor=manager, staff=farhana, amount=Decimal("4000.00"), reason="Two"
        )

        assert farhana.advances.count() == 2

    def test_more_than_a_months_salary_is_refused(self, manager, farhana):
        """
        The ceiling. Recovery comes out of one month, so an advance past that
        quietly takes several months of somebody's entire pay to clear —
        a decision for a person, not something a form waves through.
        """
        with pytest.raises(services.AdvanceError) as caught:
            services.request_advance(
                actor=manager, staff=farhana,
                amount=Decimal("35000.00"), reason="Too much",
            )

        assert caught.value.code == "exceeds_recoverable"

    def test_the_ceiling_counts_what_is_already_open(self, manager, farhana):
        services.request_advance(
            actor=manager, staff=farhana, amount=Decimal("25000.00"), reason="First"
        )

        with pytest.raises(services.AdvanceError) as caught:
            services.request_advance(
                actor=manager, staff=farhana, amount=Decimal("6000.00"), reason="Second"
            )

        assert caught.value.code == "exceeds_recoverable"

    @pytest.mark.isolation
    def test_an_admin_cannot_request_one(self, admin_client, farhana):
        """Admin has no branch to file it under — the same rule as salary."""
        response = admin_client.post(
            request_url(farhana),
            {"amount": "5000.00", "reason": "Medical"},
            format="json",
        )

        assert response.status_code == 403


@pytest.mark.money
class TestApprovalAndDisbursement:
    def test_only_admin_reviews(self, manager_client, manager, farhana):
        advance = services.request_advance(
            actor=manager, staff=farhana, amount=Decimal("5000.00"), reason="Medical"
        )

        response = manager_client.post(
            advance_url(advance, "review"), {"approve": True}, format="json"
        )

        assert response.status_code == 403

    def test_rejecting_requires_a_reason(self, admin_user, manager, farhana):
        advance = services.request_advance(
            actor=manager, staff=farhana, amount=Decimal("5000.00"), reason="Medical"
        )

        with pytest.raises(services.AdvanceError) as caught:
            services.review_advance(
                actor=admin_user, advance=advance, approve=False, review_note=""
            )

        assert caught.value.code == "note_required"

    def test_it_cannot_be_paid_before_it_is_approved(self, manager, farhana):
        advance = services.request_advance(
            actor=manager, staff=farhana, amount=Decimal("5000.00"), reason="Medical"
        )

        with pytest.raises(services.AdvanceError) as caught:
            services.disburse_advance(
                actor=manager, advance=advance, payment_method="cash"
            )

        assert caught.value.code == "not_approved"
        assert Expense.objects.count() == 0

    def test_disbursing_writes_the_expense(self, manager, admin_user, farhana):
        """
        Filed under salaries, not a category of its own: it *is* salary, early.
        Splitting it off would scatter one month's payroll across two lines.
        """
        advance = paid_advance(manager, admin_user, farhana, "8000.00")

        expense = Expense.objects.get(pk=advance.expense_id)
        assert expense.category == Expense.Category.SALARIES
        assert expense.amount == Decimal("8000.00")
        assert expense.status == Expense.Status.APPROVED
        assert "Farhana Rahman" in expense.description

    def test_an_unpaid_advance_is_not_yet_a_debt(self, manager, farhana):
        """
        Approved but not handed over means the person does not have the money,
        so taking it out of their salary would collect a debt that does not
        exist.
        """
        services.request_advance(
            actor=manager, staff=farhana, amount=Decimal("5000.00"), reason="Medical"
        )

        assert services.outstanding_advance_total(farhana) == Decimal("0.00")


@pytest.mark.money
class TestRecoveryFromSalary:
    def test_the_salary_request_shows_the_subtraction(
        self, manager, admin_user, farhana
    ):
        paid_advance(manager, admin_user, farhana, "10000.00")

        payment = services.request_salary_payment(
            actor=manager, staff=farhana, month=this_month()
        )

        assert payment.gross_amount == Decimal("30000.00")
        assert payment.advance_deduction == Decimal("10000.00")
        assert payment.amount == Decimal("20000.00")

    def test_nothing_is_recovered_until_the_salary_is_paid(
        self, manager, admin_user, farhana
    ):
        advance = paid_advance(manager, admin_user, farhana, "10000.00")

        services.request_salary_payment(
            actor=manager, staff=farhana, month=this_month()
        )

        advance.refresh_from_db()
        assert advance.recovered_amount == Decimal("0.00")
        assert advance.outstanding == Decimal("10000.00")

    def test_a_rejected_salary_leaves_the_debt_alone(
        self, manager, admin_user, farhana
    ):
        """The reason recovery waits for disbursement: no money, no repayment."""
        advance = paid_advance(manager, admin_user, farhana, "10000.00")
        payment = services.request_salary_payment(
            actor=manager, staff=farhana, month=this_month()
        )

        services.review_salary_payment(
            actor=admin_user, payment=payment, approve=False, review_note="Not yet"
        )

        advance.refresh_from_db()
        assert advance.recovered_amount == Decimal("0.00")

    def test_paying_the_salary_recovers_it(self, manager, admin_user, farhana):
        advance = paid_advance(manager, admin_user, farhana, "10000.00")
        payment = services.request_salary_payment(
            actor=manager, staff=farhana, month=this_month()
        )
        services.review_salary_payment(
            actor=admin_user, payment=payment, approve=True
        )

        services.disburse_salary_payment(
            actor=manager, payment=payment, payment_method="cash"
        )

        advance.refresh_from_db()
        assert advance.recovered_amount == Decimal("10000.00")
        assert advance.outstanding == Decimal("0.00")
        assert services.outstanding_advance_total(farhana) == Decimal("0.00")

    def test_the_expense_is_the_net_not_the_gross(
        self, manager, admin_user, farhana
    ):
        """What left the till this time is 20,000 — the other 10,000 left earlier."""
        paid_advance(manager, admin_user, farhana, "10000.00")
        payment = services.request_salary_payment(
            actor=manager, staff=farhana, month=this_month()
        )
        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)
        services.disburse_salary_payment(
            actor=manager, payment=payment, payment_method="cash"
        )

        payment.refresh_from_db()
        assert payment.expense.amount == Decimal("20000.00")

    def test_oldest_advance_is_recovered_first(self, manager, admin_user, farhana):
        first = paid_advance(manager, admin_user, farhana, "8000.00")
        second = paid_advance(manager, admin_user, farhana, "7000.00")
        # 15,000 outstanding against a 12,000 month: the first clears, the
        # second takes what is left and carries the remainder.
        farhana.monthly_salary = Decimal("12000.00")
        farhana.save(update_fields=["monthly_salary"])

        payment = services.request_salary_payment(
            actor=manager, staff=farhana, month=this_month()
        )
        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)
        services.disburse_salary_payment(
            actor=manager, payment=payment, payment_method="cash"
        )

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.recovered_amount == Decimal("8000.00")
        assert first.outstanding == Decimal("0.00")
        assert second.recovered_amount == Decimal("4000.00")
        assert second.outstanding == Decimal("3000.00")

    def test_two_open_requests_do_not_deduct_the_same_advance_twice(
        self, manager, admin_user, farhana
    ):
        paid_advance(manager, admin_user, farhana, "10000.00")

        october = services.request_salary_payment(
            actor=manager, staff=farhana, month="2026-10"
        )
        november = services.request_salary_payment(
            actor=manager, staff=farhana, month="2026-11"
        )

        assert october.advance_deduction == Decimal("10000.00")
        assert november.advance_deduction == Decimal("0.00")
        assert november.amount == Decimal("30000.00")

    def test_an_advance_bigger_than_the_month_carries_over(
        self, manager, admin_user, farhana
    ):
        """
        A month can only give back what it holds. Expense cannot be negative
        and nobody can be asked to hand salary back.
        """
        advance = paid_advance(manager, admin_user, farhana, "30000.00")
        # Shrink the salary so one month cannot clear it. Only reachable
        # because the pay changed after the advance was granted -- the
        # ceiling at request time keeps an advance inside one month's salary.
        farhana.monthly_salary = Decimal("12000.00")
        farhana.save(update_fields=["monthly_salary"])

        payment = services.request_salary_payment(
            actor=manager, staff=farhana, month=this_month()
        )
        services.review_salary_payment(actor=admin_user, payment=payment, approve=True)
        services.disburse_salary_payment(
            actor=manager, payment=payment, payment_method="cash"
        )

        advance.refresh_from_db()
        payment.refresh_from_db()
        assert advance.recovered_amount == Decimal("12000.00")
        assert advance.outstanding == Decimal("18000.00")
        # The whole month went to the debt, so nothing was handed over and
        # no expense was written -- that money already left as the advance.
        assert payment.amount == Decimal("0.00")
        assert payment.expense_id is None

    def test_a_month_swallowed_whole_is_recorded_not_refused(
        self, manager, admin_user, farhana
    ):
        """
        A payroll event that moved no money, the same shape a fully
        discounted bill has. Refusing it would deadlock: the salary could
        never be requested, so the advance could never be recovered.
        """
        advance = paid_advance(manager, admin_user, farhana, "30000.00")
        assert advance.outstanding == Decimal("30000.00")

        payment = services.request_salary_payment(
            actor=manager, staff=farhana, month=this_month()
        )

        assert payment.gross_amount == Decimal("30000.00")
        assert payment.advance_deduction == Decimal("30000.00")
        assert payment.amount == Decimal("0.00")
        assert SalaryPayment.objects.count() == 1


@pytest.mark.isolation
class TestScope:
    def test_a_manager_sees_only_their_branch(
        self, other_manager_client, manager, farhana
    ):
        services.request_advance(
            actor=manager, staff=farhana, amount=Decimal("5000.00"), reason="Medical"
        )

        body = other_manager_client.get(reverse("staff:staffadvance-list")).json()

        results = body["results"] if isinstance(body, dict) else body
        assert results == []

    def test_admin_sees_every_branch(self, admin_client, manager, farhana):
        services.request_advance(
            actor=manager, staff=farhana, amount=Decimal("5000.00"), reason="Medical"
        )

        body = admin_client.get(reverse("staff:staffadvance-list")).json()

        results = body["results"] if isinstance(body, dict) else body
        assert len(results) == 1
