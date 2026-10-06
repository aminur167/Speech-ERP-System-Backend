"""
The POS sale writes stock, sale lines and stock movements in single
statements however many items are in the cart. These pin that the batched
writes land exactly what the per-line ones did, and the one case the old
per-line loop got wrong: the same material twice in one cart.
"""

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.materials.models import Material, MaterialMovement, MaterialSaleItem
from apps.payments.models import Payment

pytestmark = [pytest.mark.django_db, pytest.mark.money]

SELL = reverse("materials:material-sell")


def sell(client, patient, items, **extra):
    return client.post(
        SELL,
        {"patient": patient.pk, "items": items, "method": "cash", **extra},
        format="json",
    )


@pytest.fixture
def patient(patient_factory):
    return patient_factory()


class TestBatchedWrites:
    def test_every_line_lands_stock_movement_and_sale_item(
        self, manager_client, patient, material_factory
    ):
        a = material_factory(quantity=10, selling_price=Decimal("100.00"))
        b = material_factory(quantity=6, selling_price=Decimal("250.00"))
        c = material_factory(quantity=3, selling_price=Decimal("40.00"))

        response = sell(
            manager_client, patient,
            [{"material": a.pk, "quantity": 2}, {"material": b.pk, "quantity": 6},
             {"material": c.pk, "quantity": 1}],
        )

        assert response.status_code == 201, response.json()
        assert Payment.objects.get().amount == Decimal("1740.00")
        for material, left in ((a, 8), (b, 0), (c, 2)):
            material.refresh_from_db()
            assert material.quantity == left
        movements = MaterialMovement.objects.filter(type=MaterialMovement.Type.OUT)
        assert sorted(movements.values_list("quantity", flat=True)) == [1, 2, 6]
        assert MaterialSaleItem.objects.count() == 3
        assert {m.note for m in movements} == {f"Sold — {Payment.objects.get().receipt_number}"}

    def test_the_number_of_queries_does_not_grow_with_the_cart(
        self, manager_client, patient, material_factory
    ):
        materials = [material_factory(quantity=50) for _ in range(8)]

        def queries_for(count):
            items = [{"material": m.pk, "quantity": 1} for m in materials[:count]]
            with CaptureQueriesContext(connection) as ctx:
                assert sell(manager_client, patient, items).status_code == 201
            return len(ctx.captured_queries)

        assert queries_for(8) == queries_for(1)


class TestTheSameMaterialTwice:
    def test_it_becomes_one_line_for_the_total_quantity(
        self, manager_client, patient, material_factory
    ):
        item = material_factory(quantity=10, selling_price=Decimal("100.00"))

        response = sell(
            manager_client, patient,
            [{"material": item.pk, "quantity": 3}, {"material": item.pk, "quantity": 2}],
        )

        assert response.status_code == 201, response.json()
        item.refresh_from_db()
        assert item.quantity == 5
        assert Payment.objects.get().amount == Decimal("500.00")
        assert MaterialSaleItem.objects.get().quantity == 5

    def test_it_cannot_sell_more_than_is_in_stock_across_the_lines(
        self, manager_client, patient, material_factory
    ):
        """5 in stock, 3 + 3 asked: each line alone fits, together they do not."""
        item = material_factory(quantity=5)

        response = sell(
            manager_client, patient,
            [{"material": item.pk, "quantity": 3}, {"material": item.pk, "quantity": 3}],
        )

        assert response.status_code == 400
        assert response.json()["code"] == "insufficient_stock"
        item.refresh_from_db()
        assert item.quantity == 5
        assert not Payment.objects.exists()


class TestFailuresLeaveNothing:
    def test_a_missing_item_rolls_the_whole_sale_back(
        self, manager_client, patient, material_factory
    ):
        real = material_factory(quantity=5)

        response = sell(
            manager_client, patient,
            [{"material": real.pk, "quantity": 1}, {"material": 99999999, "quantity": 1}],
        )

        assert response.status_code in (400, 404)
        real.refresh_from_db()
        assert real.quantity == 5
        assert not Payment.objects.exists()
        assert not MaterialMovement.objects.exists()

    def test_another_branchs_material_is_not_sellable(
        self, manager_client, patient, material_factory, other_branch
    ):
        foreign = material_factory(branch=other_branch, quantity=5)

        response = sell(manager_client, patient, [{"material": foreign.pk, "quantity": 1}])

        assert response.status_code in (400, 404)
        foreign.refresh_from_db()
        assert foreign.quantity == 5
        assert not Payment.objects.exists()

    def test_a_replayed_sale_does_not_deduct_twice(
        self, manager_client, patient, material_factory
    ):
        item = material_factory(quantity=10)
        items = [{"material": item.pk, "quantity": 4}]

        first = sell(manager_client, patient, items, idempotencyKey="sale-1")
        second = sell(manager_client, patient, items, idempotencyKey="sale-1")

        assert first.status_code == 201 and second.status_code == 200
        item.refresh_from_db()
        assert item.quantity == 6
        assert Material.objects.get(pk=item.pk).quantity == 6
        assert MaterialMovement.objects.filter(type=MaterialMovement.Type.OUT).count() == 1
