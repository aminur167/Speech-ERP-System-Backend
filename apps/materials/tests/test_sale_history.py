"""
Sell History — the list of what went over the counter, and the two actions
offered on a row.

The pressure points, in the order they would do damage if wrong:

1. **Void returns the stock.** Refund approval always did; void did not, so
   cancelling a mistaken sale the same day quietly lost the branch real
   goods. This is the screen whose Delete button makes that happen often, so
   it is pinned hardest here.
2. **Edit cannot reach a registered patient.** Correcting a counter buyer's
   name is harmless; renaming a real patient from a sales list is not.
3. **Branch isolation**, as everywhere — one branch's counter must be
   invisible to another branch's manager, on reads and on both actions.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.materials import services
from apps.materials.models import Material, MaterialMovement
from apps.payments.models import Payment, PaymentStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def flashcards(material_factory):
    return material_factory(
        name="Flashcards Set", code="MAT-FC-1", quantity=20,
        unit_cost=Decimal("250.00"), selling_price=Decimal("350.00"),
    )


@pytest.fixture
def walk_in(db, branch):
    """
    A counter buyer.

    Built directly rather than through the API so its `patient_code` cannot
    collide with `patient_factory`'s own fixed series -- the endpoint draws
    from the real sequence, which starts at the same number the factory hands
    out first. That the create endpoint produces one of these correctly is
    `TestWalkInCustomer`'s job, not this module's.
    """
    from apps.patients.models import Patient

    return Patient.objects.create(
        patient_code="PT-DHK-2026-09001",
        name="Apu Das",
        phone="01612345678",
        is_walk_in=True,
        branch=branch,
    )


def sell(manager, branch, patient, material, quantity=2):
    payment, _items, _created = services.sell_materials(
        actor=manager, branch=branch, patient=patient,
        items=[{"material_id": material.id, "quantity": quantity}], method="cash",
    )
    return payment


class TestHistoryListing:
    def test_a_sale_appears_with_its_buyer_and_lines(
        self, manager_client, manager, branch, walk_in, flashcards
    ):
        sell(manager, branch, walk_in, flashcards)

        response = manager_client.get(reverse("materials:sale-list"))

        assert response.status_code == 200
        [row] = response.json()["results"]
        assert row["customerName"] == "Apu Das"
        assert row["customerPhone"] == "01612345678"
        assert row["isWalkIn"] is True
        assert row["total"] == "700.00"
        assert row["branchName"] == branch.name
        assert row["items"] == [
            {
                "material": flashcards.id,
                "name": "Flashcards Set",
                "quantity": 2,
                "unitPrice": "350.00",
                "lineTotal": "700.00",
            }
        ]

    def test_sales_to_registered_patients_are_listed_too(
        self, manager_client, manager, branch, patient_factory, flashcards
    ):
        sell(manager, branch, patient_factory(name="Rahim Ahmed"), flashcards)

        [row] = manager_client.get(reverse("materials:sale-list")).json()["results"]

        assert row["customerName"] == "Rahim Ahmed"
        assert row["isWalkIn"] is False

    def test_other_payment_kinds_are_not_sales(
        self, manager_client, manager, branch, patient_factory
    ):
        from apps.payments import services as payment_services

        payment_services.create_payment(
            actor=manager, branch=branch, patient=patient_factory(),
            amount=Decimal("800.00"), method="cash", category="daily",
        )

        assert manager_client.get(reverse("materials:sale-list")).json()["count"] == 0

    def test_a_voided_sale_leaves_the_list(
        self, manager_client, manager, branch, walk_in, flashcards
    ):
        payment = sell(manager, branch, walk_in, flashcards)

        manager_client.post(
            reverse("materials:sale-void", args=[payment.pk]), {"reason": "wrong item"}
        )

        assert manager_client.get(reverse("materials:sale-list")).json()["count"] == 0


class TestHistoryFilters:
    def test_search_matches_the_buyer(
        self, manager_client, manager, branch, walk_in, patient_factory, flashcards
    ):
        sell(manager, branch, walk_in, flashcards)
        sell(manager, branch, patient_factory(name="Rahim Ahmed"), flashcards, quantity=1)

        results = manager_client.get(
            reverse("materials:sale-list"), {"search": "Apu"}
        ).json()["results"]

        assert [row["customerName"] for row in results] == ["Apu Das"]

    def test_search_matches_a_phone_typed_in_another_format(
        self, manager_client, manager, branch, walk_in, flashcards
    ):
        sell(manager, branch, walk_in, flashcards)

        results = manager_client.get(
            reverse("materials:sale-list"), {"search": "+8801612345678"}
        ).json()["results"]

        assert len(results) == 1

    def test_search_matches_a_product(
        self, manager_client, manager, branch, walk_in, flashcards, material_factory
    ):
        other = material_factory(name="Mirror Board", code="MAT-MB-1", quantity=5)
        sell(manager, branch, walk_in, flashcards)
        sell(manager, branch, walk_in, other, quantity=1)

        results = manager_client.get(
            reverse("materials:sale-list"), {"search": "Mirror"}
        ).json()["results"]

        assert [row["items"][0]["name"] for row in results] == ["Mirror Board"]

    def test_a_sale_matching_two_product_lines_is_listed_once(
        self, manager_client, manager, branch, walk_in, material_factory
    ):
        """The search joins through the lines, so this would otherwise duplicate."""
        a = material_factory(name="Picture Cards A", code="MAT-PA-1", quantity=5)
        b = material_factory(name="Picture Cards B", code="MAT-PB-1", quantity=5)
        services.sell_materials(
            actor=manager, branch=branch, patient=walk_in, method="cash",
            items=[
                {"material_id": a.id, "quantity": 1},
                {"material_id": b.id, "quantity": 1},
            ],
        )

        body = manager_client.get(
            reverse("materials:sale-list"), {"search": "Picture Cards"}
        ).json()

        assert body["count"] == 1

    def test_date_filters_to_one_day(
        self, manager_client, manager, branch, walk_in, flashcards
    ):
        old = sell(manager, branch, walk_in, flashcards, quantity=1)
        Payment.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=3)
        )
        sell(manager, branch, walk_in, flashcards, quantity=1)

        today = timezone.localdate().isoformat()
        body = manager_client.get(reverse("materials:sale-list"), {"date": today}).json()

        assert body["count"] == 1

    def test_admin_sees_every_branch_and_can_narrow_to_one(
        self, admin_client, manager, other_manager, branch, other_branch,
        walk_in, flashcards, material_factory, patient_factory,
    ):
        sell(manager, branch, walk_in, flashcards)
        elsewhere = material_factory(
            name="Other Item", code="MAT-OI-1", quantity=5, branch=other_branch
        )
        sell(
            other_manager, other_branch,
            patient_factory(branch=other_branch, name="Chattogram Buyer"),
            elsewhere,
        )

        everything = admin_client.get(reverse("materials:sale-list")).json()
        narrowed = admin_client.get(
            reverse("materials:sale-list"), {"branch": other_branch.id}
        ).json()

        assert everything["count"] == 2
        assert [row["branchName"] for row in narrowed["results"]] == [other_branch.name]


class TestBranchIsolation:
    def test_a_manager_sees_only_their_own_branch(
        self, other_manager_client, manager, branch, walk_in, flashcards
    ):
        sell(manager, branch, walk_in, flashcards)

        assert other_manager_client.get(
            reverse("materials:sale-list")
        ).json()["count"] == 0

    def test_another_branch_cannot_edit_the_buyer(
        self, other_manager_client, manager, branch, walk_in, flashcards
    ):
        payment = sell(manager, branch, walk_in, flashcards)

        response = other_manager_client.patch(
            reverse("materials:sale-customer", args=[payment.pk]),
            {"name": "Hijack", "phone": "01999999999"},
        )

        assert response.status_code == 404

    def test_another_branch_cannot_void_the_sale(
        self, other_manager_client, manager, branch, walk_in, flashcards
    ):
        payment = sell(manager, branch, walk_in, flashcards)

        response = other_manager_client.post(
            reverse("materials:sale-void", args=[payment.pk]), {"reason": "x"}
        )

        assert response.status_code == 404
        payment.refresh_from_db()
        assert payment.status == PaymentStatus.PAID


class TestEditingTheBuyer:
    def test_a_walk_in_name_and_phone_can_be_corrected(
        self, manager_client, manager, branch, walk_in, flashcards
    ):
        payment = sell(manager, branch, walk_in, flashcards)

        response = manager_client.patch(
            reverse("materials:sale-customer", args=[payment.pk]),
            {"name": "Apu Das Gupta", "phone": "01612345679"},
        )

        assert response.status_code == 200, response.json()
        assert response.json()["customerName"] == "Apu Das Gupta"
        walk_in.refresh_from_db()
        assert walk_in.phone == "01612345679"

    def test_a_registered_patient_cannot_be_renamed_from_here(
        self, manager_client, manager, branch, patient_factory, flashcards
    ):
        patient = patient_factory(name="Rahim Ahmed")
        payment = sell(manager, branch, patient, flashcards)

        response = manager_client.patch(
            reverse("materials:sale-customer", args=[payment.pk]),
            {"name": "Someone Else", "phone": "01712345678"},
        )

        assert response.status_code == 400
        assert response.json()["code"] == "not_a_walk_in"
        patient.refresh_from_db()
        assert patient.name == "Rahim Ahmed"

    def test_a_bad_phone_is_rejected(
        self, manager_client, manager, branch, walk_in, flashcards
    ):
        payment = sell(manager, branch, walk_in, flashcards)

        response = manager_client.patch(
            reverse("materials:sale-customer", args=[payment.pk]),
            {"name": "Apu Das", "phone": "12345"},
        )

        assert response.status_code == 400
        assert "phone" in response.json()

    def test_editing_touches_no_money_and_no_stock(
        self, manager_client, manager, branch, walk_in, flashcards
    ):
        payment = sell(manager, branch, walk_in, flashcards)
        stock_before = Material.objects.get(pk=flashcards.pk).quantity

        manager_client.patch(
            reverse("materials:sale-customer", args=[payment.pk]),
            {"name": "Apu Das Gupta", "phone": "01612345679"},
        )

        payment.refresh_from_db()
        assert payment.amount == Decimal("700.00")
        assert payment.status == PaymentStatus.PAID
        assert Material.objects.get(pk=flashcards.pk).quantity == stock_before

    def test_the_correction_is_audited(
        self, manager_client, manager, branch, walk_in, flashcards
    ):
        from apps.common.models import AuditLog

        payment = sell(manager, branch, walk_in, flashcards)
        manager_client.patch(
            reverse("materials:sale-customer", args=[payment.pk]),
            {"name": "Apu Das Gupta", "phone": "01612345678"},
        )

        entry = AuditLog.objects.filter(
            target_type="Patient", target_id=str(walk_in.pk),
            action=AuditLog.Action.UPDATE,
        ).first()
        assert entry is not None
        assert entry.changes["name"] == {"from": "Apu Das", "to": "Apu Das Gupta"}
        assert payment.receipt_number in entry.reason


@pytest.mark.money
class TestVoidReturnsStock:
    """The behaviour that was missing before this screen existed."""

    def test_voiding_a_sale_puts_the_goods_back(
        self, manager_client, manager, branch, walk_in, flashcards
    ):
        before = flashcards.quantity
        payment = sell(manager, branch, walk_in, flashcards, quantity=3)
        assert Material.objects.get(pk=flashcards.pk).quantity == before - 3

        response = manager_client.post(
            reverse("materials:sale-void", args=[payment.pk]), {"reason": "wrong item"}
        )

        assert response.status_code == 200, response.json()
        assert Material.objects.get(pk=flashcards.pk).quantity == before

    def test_the_return_is_written_to_the_stock_ledger(
        self, manager_client, manager, branch, walk_in, flashcards
    ):
        payment = sell(manager, branch, walk_in, flashcards, quantity=3)

        manager_client.post(
            reverse("materials:sale-void", args=[payment.pk]), {"reason": "wrong item"}
        )

        movement = MaterialMovement.objects.filter(
            material=flashcards, type=MaterialMovement.Type.IN
        ).latest("created_at")
        assert movement.quantity == 3
        assert payment.receipt_number in movement.note

    def test_a_multi_line_sale_returns_every_line(
        self, manager_client, manager, branch, walk_in, flashcards, material_factory
    ):
        other = material_factory(name="Mirror Board", code="MAT-MB-2", quantity=10)
        payment, _items, _ = services.sell_materials(
            actor=manager, branch=branch, patient=walk_in, method="cash",
            items=[
                {"material_id": flashcards.id, "quantity": 2},
                {"material_id": other.id, "quantity": 4},
            ],
        )

        manager_client.post(
            reverse("materials:sale-void", args=[payment.pk]), {"reason": "cancelled"}
        )

        assert Material.objects.get(pk=flashcards.pk).quantity == 20
        assert Material.objects.get(pk=other.pk).quantity == 10

    def test_voiding_a_non_sale_payment_returns_nothing(
        self, manager_client, manager, branch, patient_factory
    ):
        """The stock return must be a no-op for a payment with no lines."""
        from apps.payments import services as payment_services

        payment, _ = payment_services.create_payment(
            actor=manager, branch=branch, patient=patient_factory(),
            amount=Decimal("800.00"), method="cash", category="daily",
        )

        payment_services.void_payment(actor=manager, payment=payment, reason="mistake")

        payment.refresh_from_db()
        assert payment.status == PaymentStatus.VOID
        assert not MaterialMovement.objects.filter(
            type=MaterialMovement.Type.IN
        ).exists()

    def test_a_reason_is_required(
        self, manager_client, manager, branch, walk_in, flashcards
    ):
        payment = sell(manager, branch, walk_in, flashcards)

        response = manager_client.post(
            reverse("materials:sale-void", args=[payment.pk]), {"reason": "  "}
        )

        assert response.status_code == 400
        assert response.json()["code"] == "reason_required"

    def test_a_manager_cannot_void_an_older_sale(
        self, manager_client, manager, branch, walk_in, flashcards
    ):
        """The existing same-day rule still governs; this screen does not widen it."""
        payment = sell(manager, branch, walk_in, flashcards)
        Payment.objects.filter(pk=payment.pk).update(
            created_at=timezone.now() - timedelta(days=2)
        )

        response = manager_client.post(
            reverse("materials:sale-void", args=[payment.pk]), {"reason": "late fix"}
        )

        assert response.status_code == 400
        assert response.json()["code"] == "not_same_day"
        assert Material.objects.get(pk=flashcards.pk).quantity == 18

    def test_admin_may_void_an_older_sale(
        self, admin_client, manager, branch, walk_in, flashcards
    ):
        payment = sell(manager, branch, walk_in, flashcards)
        Payment.objects.filter(pk=payment.pk).update(
            created_at=timezone.now() - timedelta(days=2)
        )

        response = admin_client.post(
            reverse("materials:sale-void", args=[payment.pk]),
            {"reason": "approved fix"},
        )

        assert response.status_code == 200, response.json()
        assert Material.objects.get(pk=flashcards.pk).quantity == 20


class TestWalkInsAreNotPatients:
    def test_a_walk_in_is_absent_from_the_patient_list(self, manager_client, walk_in):
        body = manager_client.get(reverse("patients:patient-list")).json()

        assert walk_in.name not in [row["name"] for row in body["results"]]

    def test_searching_for_them_in_the_patient_list_finds_nothing(
        self, manager_client, walk_in
    ):
        body = manager_client.get(
            reverse("patients:patient-list"), {"search": "Apu"}
        ).json()

        assert body["count"] == 0

    def test_a_walk_in_is_absent_from_the_directory(self, manager_client, walk_in):
        body = manager_client.get(reverse("patients:patient-directory")).json()

        assert walk_in.name not in [row["name"] for row in body["results"]]

    def test_they_do_not_count_toward_the_directory_summary(
        self, manager_client, walk_in, patient_factory
    ):
        patient_factory(name="Rahim Ahmed")

        body = manager_client.get(reverse("patients:patient-directory-summary")).json()

        assert body["total"] == 1

    def test_they_are_still_readable_by_id(self, manager_client, walk_in):
        """Sell History reads the buyer by id; a receipt must resolve its name."""
        response = manager_client.get(
            reverse("patients:patient-detail", args=[walk_in.pk])
        )

        assert response.status_code == 200
        assert response.json()["name"] == "Apu Das"
