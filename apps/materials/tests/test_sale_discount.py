"""
The counter sale's discount — one figure off the whole cart.

Per line was the alternative and was rejected: a counter sale is settled as
a total, and `MaterialSaleItem.unit_price` is what the refund path, the sale
history and the stock movements all read, so a per-line discount would have
had to be reconciled against that on every partial return.

Two things here are easy to get wrong and are pinned because of it:

1. **A discounted cart is returned at its discounted worth.** The refund was
   derived from `unit_price` alone, so returning a cart sold for 450 off a
   500 shelf price asked for 500 back. The refundable ceiling in
   `request_refund` stopped the money going out — by refusing the return
   outright ("only 450 remains refundable"), which is a refusal a manager
   cannot act on, not a correct figure.
2. **The whole cart cannot be discounted away.** Every sale line and stock
   movement hangs off the payment, and a Payment must be at least 0.01 — so
   a free cart would take stock out of the branch with no record of where it
   went.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.materials.models import Material, MaterialSaleItem
from apps.payments.models import Payment, RefundRequest

pytestmark = [pytest.mark.django_db, pytest.mark.money]

SELL_URL = reverse("materials:material-sell")


@pytest.fixture
def patient(patient_factory):
    return patient_factory()


@pytest.fixture
def flashcards(material_factory):
    return material_factory(
        name="Flashcards Set", quantity=20,
        unit_cost=Decimal("250.00"), selling_price=Decimal("350.00"),
    )


def cart(patient, *lines, **overrides):
    data = {
        "patient": str(patient.id),
        "method": "cash",
        "items": [
            {"material": str(material.id), "quantity": quantity}
            for material, quantity in lines
        ],
    }
    data.update(overrides)
    return data


class TestTheCartDiscount:
    def test_it_is_recorded_beside_the_total_it_came_off(
        self, manager_client, patient, flashcards
    ):
        response = manager_client.post(
            SELL_URL,
            cart(
                patient, (flashcards, 2),
                discount="100.00", discountReason="Regular customer",
            ),
            format="json",
        )

        assert response.status_code == 201
        body = response.json()["payment"]
        assert Decimal(body["amount"]) == Decimal("600.00")
        assert Decimal(body["grossAmount"]) == Decimal("700.00")
        assert Decimal(body["discountAmount"]) == Decimal("100.00")
        assert body["discountReason"] == "Regular customer"

    def test_the_lines_keep_the_shelf_price_they_were_sold_at(
        self, manager_client, patient, flashcards
    ):
        """
        The discount is on the cart, not the item. Rewriting `unit_price`
        would make the sale history and the stock movements disagree with the
        catalogue about what the product costs.
        """
        response = manager_client.post(
            SELL_URL,
            cart(patient, (flashcards, 2), discount="100.00", discountReason="Regular"),
            format="json",
        )

        line = MaterialSaleItem.objects.get(payment_id=response.json()["payment"]["id"])
        assert line.unit_price == Decimal("350.00")
        assert line.quantity == 2

    def test_stock_leaves_by_the_quantity_sold_not_the_money_taken(
        self, manager_client, patient, flashcards
    ):
        manager_client.post(
            SELL_URL,
            cart(patient, (flashcards, 2), discount="100.00", discountReason="Regular"),
            format="json",
        )

        flashcards.refresh_from_db()
        assert flashcards.quantity == 18

    def test_no_discount_leaves_the_gross_null(
        self, manager_client, patient, flashcards
    ):
        response = manager_client.post(
            SELL_URL, cart(patient, (flashcards, 1)), format="json"
        )

        body = response.json()["payment"]
        assert Decimal(body["amount"]) == Decimal("350.00")
        assert body["grossAmount"] is None
        assert Decimal(body["discountAmount"]) == Decimal("0.00")

    def test_a_discount_without_a_reason_is_refused(
        self, manager_client, patient, flashcards
    ):
        response = manager_client.post(
            SELL_URL, cart(patient, (flashcards, 1), discount="50.00"), format="json"
        )

        assert response.status_code == 400
        assert response.json()["code"] == "discount_reason_required"
        # Nothing moved: no payment, and the stock is untouched.
        assert Payment.objects.count() == 0
        assert Material.objects.get(pk=flashcards.pk).quantity == 20

    def test_the_whole_cart_cannot_be_discounted_away(
        self, manager_client, patient, flashcards
    ):
        response = manager_client.post(
            SELL_URL,
            cart(patient, (flashcards, 1), discount="350.00", discountReason="Gift"),
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "discount_exceeds_total"
        assert response.json()["cartTotal"] == "349.99"
        assert Material.objects.get(pk=flashcards.pk).quantity == 20

    def test_a_discount_over_the_total_is_refused_with_the_limit(
        self, manager_client, patient, flashcards
    ):
        response = manager_client.post(
            SELL_URL,
            cart(patient, (flashcards, 2), discount="900.00", discountReason="No"),
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "discount_exceeds_total"
        assert response.json()["cartTotal"] == "699.99"

    def test_the_discount_is_checked_against_the_servers_own_total(
        self, manager_client, patient, flashcards
    ):
        """
        Not against anything the client sends. A cart claiming a bigger total
        to justify a bigger discount gets the catalogue's total, so the
        discount is measured against 700 either way.
        """
        response = manager_client.post(
            SELL_URL,
            cart(
                patient, (flashcards, 2),
                total="99999.00", discount="900.00", discountReason="Tampered",
            ),
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "discount_exceeds_total"


class TestReturningADiscountedCart:
    @pytest.fixture
    def discounted_sale(self, manager_client, patient, flashcards):
        """Two sets at 350 = 700, discounted by 100, so 600 was collected."""
        response = manager_client.post(
            SELL_URL,
            cart(patient, (flashcards, 2), discount="100.00", discountReason="Regular"),
            format="json",
        )
        return Payment.objects.get(pk=response.json()["payment"]["id"])

    def test_the_whole_cart_comes_back_at_what_was_collected(
        self, manager_client, discounted_sale, flashcards
    ):
        response = manager_client.post(
            reverse("payments:payment-request-refund", args=[discounted_sale.id]),
            {
                "reason": "Both sets returned unopened",
                "items": [{"material": str(flashcards.id), "quantity": 2}],
            },
            format="json",
        )

        assert response.status_code == 201, response.json()
        # 600, not the 700 shelf price -- and not a refusal.
        assert Decimal(response.json()["amount"]) == Decimal("600.00")

    def test_half_the_cart_comes_back_at_half_of_what_was_collected(
        self, manager_client, discounted_sale, flashcards
    ):
        """
        The discount applied to the cart, so it is shared out by what each
        returned line was worth: 350/700 of 600.
        """
        response = manager_client.post(
            reverse("payments:payment-request-refund", args=[discounted_sale.id]),
            {
                "reason": "One set returned",
                "items": [{"material": str(flashcards.id), "quantity": 1}],
            },
            format="json",
        )

        assert response.status_code == 201, response.json()
        assert Decimal(response.json()["amount"]) == Decimal("300.00")

    def test_an_undiscounted_cart_is_unaffected(
        self, manager_client, patient, flashcards
    ):
        sale = manager_client.post(
            SELL_URL, cart(patient, (flashcards, 2)), format="json"
        ).json()["payment"]

        response = manager_client.post(
            reverse("payments:payment-request-refund", args=[sale["id"]]),
            {
                "reason": "Returned",
                "items": [{"material": str(flashcards.id), "quantity": 2}],
            },
            format="json",
        )

        assert response.status_code == 201
        assert Decimal(response.json()["amount"]) == Decimal("700.00")

    def test_the_refund_never_exceeds_what_was_collected(
        self, manager_client, discounted_sale, flashcards
    ):
        """
        The ceiling that was carrying this before, still in place behind the
        pro-rating: whatever the shares round to, the clinic cannot pay out
        more than it took.
        """
        manager_client.post(
            reverse("payments:payment-request-refund", args=[discounted_sale.id]),
            {
                "reason": "All returned",
                "items": [{"material": str(flashcards.id), "quantity": 2}],
            },
            format="json",
        )

        total = sum(
            request.amount
            for request in RefundRequest.objects.filter(payment=discounted_sale)
        )
        assert total <= discounted_sale.amount
