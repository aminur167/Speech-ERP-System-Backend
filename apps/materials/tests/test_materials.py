"""
Material tests — follows the required-test list in docs/06.

The group that matters most is server-side pricing. The frontend mock sends
`unitPrice` from the browser and trusts it, so a tampered request could buy a
2,500 kit for 1. Several tests here attack that directly.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.materials import services
from apps.materials.models import Material, MaterialMovement, MaterialSaleItem
from apps.payments.models import PaymentCategory

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient(patient_factory):
    return patient_factory()


@pytest.fixture
def flashcards(material_factory):
    return material_factory(
        name="Flashcards Set", quantity=20,
        unit_cost=Decimal("250.00"), selling_price=Decimal("350.00"),
    )


@pytest.fixture
def kit(material_factory):
    return material_factory(
        name="Assessment Kit", quantity=5,
        unit_cost=Decimal("2000.00"), selling_price=Decimal("2500.00"),
    )


@pytest.mark.money
class TestServerSidePricing:
    """The security hole this module exists to close."""

    def test_sale_total_comes_from_the_database(
        self, manager, branch, patient, flashcards
    ):
        payment, items, _ = services.sell_materials(
            actor=manager, branch=branch, patient=patient,
            items=[{"material_id": flashcards.id, "quantity": 2}], method="cash",
        )

        assert payment.amount == Decimal("700.00")  # 2 × 350, the stored price

    def test_api_payload_has_no_price_field_to_tamper_with(
        self, manager_client, patient, kit
    ):
        """
        Posting a price is simply ignored — the serializer has no such field,
        so the 2,500 kit cannot be bought for 1.
        """
        response = manager_client.post(
            reverse("materials:material-sell"),
            {
                "patient": str(patient.id),
                "method": "cash",
                "items": [
                    {"material": str(kit.id), "quantity": 1, "unitPrice": "1.00", "price": "1.00"}
                ],
            },
            format="json",
        )

        assert response.status_code == 201
        assert response.json()["payment"]["amount"] == "2500.00"

    def test_line_price_is_frozen_at_sale_time(
        self, manager, branch, patient, flashcards
    ):
        """
        A later price change must not retroactively alter what a past sale — or
        its refund — was worth.
        """
        payment, _, _ = services.sell_materials(
            actor=manager, branch=branch, patient=patient,
            items=[{"material_id": flashcards.id, "quantity": 1}], method="cash",
        )

        flashcards.selling_price = Decimal("500.00")
        flashcards.save(update_fields=["selling_price"])

        line = MaterialSaleItem.objects.get(payment=payment)
        assert line.unit_price == Decimal("350.00")

    def test_multi_line_total_is_exact(
        self, manager, branch, patient, flashcards, kit
    ):
        payment, _, _ = services.sell_materials(
            actor=manager, branch=branch, patient=patient,
            items=[
                {"material_id": flashcards.id, "quantity": 3},
                {"material_id": kit.id, "quantity": 2},
            ],
            method="cash",
        )

        assert payment.amount == Decimal("6050.00")  # 3×350 + 2×2500


class TestSaleMechanics:
    def test_stock_is_deducted(self, manager, branch, patient, flashcards):
        services.sell_materials(
            actor=manager, branch=branch, patient=patient,
            items=[{"material_id": flashcards.id, "quantity": 3}], method="cash",
        )

        flashcards.refresh_from_db()
        assert flashcards.quantity == 17

    def test_sale_records_an_out_movement(self, manager, branch, patient, flashcards):
        payment, _, _ = services.sell_materials(
            actor=manager, branch=branch, patient=patient,
            items=[{"material_id": flashcards.id, "quantity": 2}], method="cash",
        )

        movement = MaterialMovement.objects.get(
            material=flashcards, type=MaterialMovement.Type.OUT
        )
        assert movement.quantity == 2
        assert payment.receipt_number in movement.note

    def test_payment_is_categorised_as_a_material_sale(
        self, manager, branch, patient, flashcards
    ):
        payment, _, _ = services.sell_materials(
            actor=manager, branch=branch, patient=patient,
            items=[{"material_id": flashcards.id, "quantity": 1}], method="cash",
        )
        assert payment.category == PaymentCategory.MATERIAL_SALE

    def test_insufficient_stock_is_refused(self, manager, branch, patient, kit):
        with pytest.raises(services.MaterialError) as exc:
            services.sell_materials(
                actor=manager, branch=branch, patient=patient,
                items=[{"material_id": kit.id, "quantity": 99}], method="cash",
            )

        assert exc.value.code == "insufficient_stock"
        assert exc.value.extra["available"] == 5

    def test_a_failed_sale_leaves_no_trace(
        self, manager, branch, patient, flashcards, kit
    ):
        """
        Atomicity: the first line would succeed and the second fail, so both
        the payment and the first deduction must roll back.
        """
        from apps.payments.models import Payment

        before_payments = Payment.objects.count()

        with pytest.raises(services.MaterialError):
            services.sell_materials(
                actor=manager, branch=branch, patient=patient,
                items=[
                    {"material_id": flashcards.id, "quantity": 1},
                    {"material_id": kit.id, "quantity": 999},
                ],
                method="cash",
            )

        flashcards.refresh_from_db()
        assert flashcards.quantity == 20, "stock must roll back"
        assert Payment.objects.count() == before_payments, "no orphan payment"

    def test_empty_cart_refused(self, manager, branch, patient):
        with pytest.raises(services.MaterialError) as exc:
            services.sell_materials(
                actor=manager, branch=branch, patient=patient, items=[], method="cash"
            )
        assert exc.value.code == "empty_cart"

    def test_replayed_sale_does_not_deduct_stock_twice(
        self, manager, branch, patient, flashcards
    ):
        item = [{"material_id": flashcards.id, "quantity": 2}]

        services.sell_materials(
            actor=manager, branch=branch, patient=patient, items=item,
            method="cash", idempotency_key="sale-key-1",
        )
        _, _, created = services.sell_materials(
            actor=manager, branch=branch, patient=patient, items=item,
            method="cash", idempotency_key="sale-key-1",
        )

        flashcards.refresh_from_db()
        assert created is False
        assert flashcards.quantity == 18, "replay must not deduct again"

    @pytest.mark.isolation
    def test_cannot_sell_another_branchs_material(
        self, manager, branch, patient, material_factory, other_branch
    ):
        foreign = material_factory(branch=other_branch)

        with pytest.raises(services.MaterialError) as exc:
            services.sell_materials(
                actor=manager, branch=branch, patient=patient,
                items=[{"material_id": foreign.id, "quantity": 1}], method="cash",
            )
        assert exc.value.code == "material_not_found"


class TestStockAdjustment:
    def test_stock_in_increases_quantity(self, manager, flashcards):
        services.adjust_stock(
            actor=manager, material=flashcards,
            movement_type=MaterialMovement.Type.IN, quantity=10, note="Restock",
        )
        flashcards.refresh_from_db()
        assert flashcards.quantity == 30

    def test_stock_out_decreases_quantity(self, manager, flashcards):
        services.adjust_stock(
            actor=manager, material=flashcards,
            movement_type=MaterialMovement.Type.OUT, quantity=5, note="Damaged",
        )
        flashcards.refresh_from_db()
        assert flashcards.quantity == 15

    def test_cannot_remove_more_than_is_held(self, manager, kit):
        with pytest.raises(services.MaterialError) as exc:
            services.adjust_stock(
                actor=manager, material=kit,
                movement_type=MaterialMovement.Type.OUT, quantity=99,
            )
        assert exc.value.code == "insufficient_stock"

    def test_adjustment_records_its_reason(self, manager, flashcards):
        services.adjust_stock(
            actor=manager, material=flashcards,
            movement_type=MaterialMovement.Type.OUT, quantity=2,
            note="Damaged return written off",
        )

        movement = MaterialMovement.objects.filter(material=flashcards).first()
        assert movement.note == "Damaged return written off"

    def test_damaged_return_workflow(self, manager, branch, patient, flashcards):
        """
        A refund returns stock unconditionally, so writing off damage is a
        second, explicit step. Documented here so the two-step nature is
        visible rather than surprising.
        """
        services.adjust_stock(
            actor=manager, material=flashcards,
            movement_type=MaterialMovement.Type.IN, quantity=1, note="Refund return",
        )
        services.adjust_stock(
            actor=manager, material=flashcards,
            movement_type=MaterialMovement.Type.OUT, quantity=1, note="Damaged — written off",
        )

        flashcards.refresh_from_db()
        assert flashcards.quantity == 20


class TestMaterialCreationValidation:
    def test_negative_opening_quantity_is_rejected(self, manager_client):
        """
        Regression test: quantity had no lower-bound validation (unlike
        unit_cost/selling_price, which both reject below their minimum), so a
        material could be created already sitting at negative stock.
        """
        response = manager_client.post(
            reverse("materials:material-list"),
            {
                "name": "Bad Opening Stock", "unit": "piece", "quantity": -5,
                "unit_cost": "10.00", "selling_price": "20.00", "reorder_level": 0,
            },
        )
        assert response.status_code == 400
        assert "quantity" in response.json()

    def test_zero_opening_quantity_is_accepted(self, manager_client):
        response = manager_client.post(
            reverse("materials:material-list"),
            {
                "name": "Zero Opening Stock", "unit": "piece", "quantity": 0,
                "unit_cost": "10.00", "selling_price": "20.00", "reorder_level": 0,
            },
        )
        assert response.status_code == 201
        assert response.json()["quantity"] == 0


class TestMaterialImages:
    def test_material_without_an_image_is_fine(self, manager_client):
        response = manager_client.post(
            reverse("materials:material-list"),
            {
                "name": "No Image", "unit": "piece", "quantity": 5,
                "unit_cost": "100.00", "selling_price": "150.00", "reorder_level": 1,
            },
        )
        assert response.status_code == 201
        assert response.json()["imageUrl"] == ""

    def test_external_image_url_is_rejected(self, manager_client, settings):
        """
        Without this the API stores any URL a client sends, and a material
        image could point at arbitrary external content.
        """
        settings.CLOUDINARY_CLOUD_NAME = "okp6ue8w"

        response = manager_client.post(
            reverse("materials:material-list"),
            {
                "name": "Evil", "unit": "piece", "quantity": 1,
                "unit_cost": "1.00", "selling_price": "2.00", "reorder_level": 0,
                "image_url": "https://evil.example.com/tracker.png",
            },
        )
        assert response.status_code == 400
        assert "image_url" in response.json()

    def test_another_cloudinary_account_is_rejected(self, manager_client, settings):
        settings.CLOUDINARY_CLOUD_NAME = "okp6ue8w"

        response = manager_client.post(
            reverse("materials:material-list"),
            {
                "name": "Other Account", "unit": "piece", "quantity": 1,
                "unit_cost": "1.00", "selling_price": "2.00", "reorder_level": 0,
                "image_url": "https://res.cloudinary.com/someone-else/image/upload/x.png",
            },
        )
        assert response.status_code == 400

    def test_own_cloudinary_url_is_accepted(self, manager_client, settings):
        settings.CLOUDINARY_CLOUD_NAME = "okp6ue8w"

        response = manager_client.post(
            reverse("materials:material-list"),
            {
                "name": "Good", "unit": "piece", "quantity": 1,
                "unit_cost": "1.00", "selling_price": "2.00", "reorder_level": 0,
                "image_url": "https://res.cloudinary.com/okp6ue8w/image/upload/v1/x.png",
                "image_public_id": "speech-erp/materials/x",
            },
        )
        assert response.status_code == 201

    def test_public_id_is_stored_for_later_deletion(self, manager_client, settings):
        """Without the public_id there is no reliable way to replace or delete."""
        settings.CLOUDINARY_CLOUD_NAME = "okp6ue8w"

        response = manager_client.post(
            reverse("materials:material-list"),
            {
                "name": "With Id", "unit": "piece", "quantity": 1,
                "unit_cost": "1.00", "selling_price": "2.00", "reorder_level": 0,
                "image_url": "https://res.cloudinary.com/okp6ue8w/image/upload/v1/y.png",
                "image_public_id": "speech-erp/materials/y",
            },
        )

        material = Material.objects.get(pk=response.json()["id"])
        assert material.image_public_id == "speech-erp/materials/y"


class TestMaterialSummary:
    def test_totals_and_low_stock_count(self, manager_client, flashcards, kit):
        body = manager_client.get(reverse("materials:material-summary")).json()

        assert body["totalItems"] == 2
        # 20 × 250 + 5 × 2000
        assert Decimal(body["totalStockValue"]) == Decimal("15000.00")

    def test_low_stock_counted_at_and_below_reorder_level(
        self, manager_client, material_factory
    ):
        material_factory(quantity=5, reorder_level=5)   # at level → low
        material_factory(quantity=2, reorder_level=5)   # below → low
        material_factory(quantity=9, reorder_level=5)   # above → not

        body = manager_client.get(reverse("materials:material-summary")).json()
        assert body["lowStockCount"] == 2

    def test_empty_branch_returns_zeros(self, other_manager_client):
        body = other_manager_client.get(reverse("materials:material-summary")).json()
        assert body["totalItems"] == 0
        assert Decimal(body["totalStockValue"]) == Decimal("0.00")


@pytest.mark.isolation
class TestMaterialBranchIsolation:
    """Materials are branch-scoped — physical stock doesn't move between sites."""

    def test_manager_sees_only_their_branchs_stock(
        self, manager_client, flashcards, material_factory, other_branch
    ):
        material_factory(branch=other_branch, name="Their Item")

        results = manager_client.get(reverse("materials:material-list")).json()["results"]
        assert [r["name"] for r in results] == ["Flashcards Set"]

    def test_manager_cannot_read_another_branchs_material(
        self, other_manager_client, flashcards
    ):
        response = other_manager_client.get(
            reverse("materials:material-detail", args=[flashcards.id])
        )
        assert response.status_code == 404

    def test_manager_cannot_adjust_another_branchs_stock(
        self, other_manager_client, flashcards
    ):
        response = other_manager_client.post(
            reverse("materials:material-adjust-stock", args=[flashcards.id]),
            {"type": "in", "quantity": 100},
        )
        assert response.status_code == 404

    def test_admin_sees_every_branch(self, admin_client, flashcards, material_factory, other_branch):
        material_factory(branch=other_branch)
        assert admin_client.get(reverse("materials:material-list")).json()["count"] == 2

    def test_admin_cannot_sell(self, admin_client, patient, flashcards):
        """Selling is a branch-counter action."""
        response = admin_client.post(
            reverse("materials:material-sell"),
            {
                "patient": str(patient.id), "method": "cash",
                "items": [{"material": str(flashcards.id), "quantity": 1}],
            },
            format="json",
        )
        assert response.status_code == 403


class TestSoftDelete:
    def test_delete_is_soft_and_history_survives(
        self, manager_client, manager, branch, patient, flashcards
    ):
        services.sell_materials(
            actor=manager, branch=branch, patient=patient,
            items=[{"material_id": flashcards.id, "quantity": 1}], method="cash",
        )

        manager_client.delete(reverse("materials:material-detail", args=[flashcards.id]))

        assert not Material.objects.filter(pk=flashcards.id).exists()
        assert Material.all_objects.filter(pk=flashcards.id).exists()
        assert MaterialSaleItem.objects.filter(material_id=flashcards.id).exists()
