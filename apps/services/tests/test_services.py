"""
Service catalog tests — follows the required-test list in docs/03.

The highest-value groups here are the delete-vs-deactivate distinction and the
deliberate absence of branch scoping. Both are places where a well-meaning
change could quietly break something important: deleting a package patients
are paying for, or "adding branch scoping everywhere" and splitting the shared
catalog.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.services.models import Service

pytestmark = pytest.mark.django_db


def service_payload(**overrides):
    payload = {
        "name": "Monthly 1:1 Individual Plan",
        "code": "PKG-M1-01",
        "category": "monthly",
        "fee": "12600.00",
        "is_online": False,
        "description": "Twelve one-to-one therapy sessions per month.",
        "duration_label": "1 Month (auto-renew)",
        "sessions_label": "12 Sessions",
        "expiry_label": "Last day of billing month",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def service(db):
    return Service.objects.create(
        name="Individual Therapy",
        code="MON-INDIV",
        category=Service.Category.MONTHLY,
        fee=Decimal("5000.00"),
    )


class TestServiceCrud:
    def test_admin_creates_a_service(self, admin_client):
        response = admin_client.post(reverse("services:service-list"), service_payload())

        assert response.status_code == 201
        assert response.json()["code"] == "PKG-M1-01"

    def test_optional_fields_may_be_omitted(self, admin_client):
        response = admin_client.post(
            reverse("services:service-list"),
            {"name": "Bare", "code": "BARE-01", "category": "daily", "fee": "500.00"},
        )

        assert response.status_code == 201
        assert response.json()["originalFee"] is None

    def test_duplicate_code_rejected(self, admin_client, service):
        response = admin_client.post(
            reverse("services:service-list"), service_payload(code=service.code)
        )
        assert response.status_code == 400
        assert "code" in response.json()

    def test_code_is_uppercased(self, admin_client):
        response = admin_client.post(
            reverse("services:service-list"), service_payload(code="pkg-lower-01")
        )
        assert response.json()["code"] == "PKG-LOWER-01"

    @pytest.mark.money
    def test_fee_round_trips_exactly(self, admin_client):
        """The float-vs-Decimal regression: 12600.50 must not become 12600.49."""
        response = admin_client.post(
            reverse("services:service-list"), service_payload(fee="12600.50")
        )

        assert response.json()["fee"] == "12600.50"
        assert Service.objects.get(code="PKG-M1-01").fee == Decimal("12600.50")

    @pytest.mark.parametrize("bad_fee", ["0", "0.00", "-100.00"])
    def test_non_positive_fee_rejected(self, admin_client, bad_fee):
        response = admin_client.post(
            reverse("services:service-list"), service_payload(fee=bad_fee)
        )
        assert response.status_code == 400

    def test_invalid_category_rejected(self, admin_client):
        response = admin_client.post(
            reverse("services:service-list"), service_payload(category="weekly")
        )
        assert response.status_code == 400

    def test_admin_updates_a_service(self, admin_client, service):
        response = admin_client.patch(
            reverse("services:service-detail", args=[service.id]), {"fee": "5500.00"}
        )

        assert response.status_code == 200
        service.refresh_from_db()
        assert service.fee == Decimal("5500.00")


class TestServicePermissions:
    def test_manager_can_read(self, manager_client, service):
        """Read access is intentional — the enrollment wizards need the catalog."""
        assert manager_client.get(reverse("services:service-list")).status_code == 200

    def test_manager_can_propose_a_package_but_it_lands_pending(self, manager_client):
        """A Manager may create a package, but it's a proposal, not a live entry — see TestServiceReview."""
        response = manager_client.post(reverse("services:service-list"), service_payload())
        assert response.status_code == 201
        assert response.json()["reviewStatus"] == "pending"

    def test_manager_cannot_update(self, manager_client, service):
        response = manager_client.patch(
            reverse("services:service-detail", args=[service.id]), {"fee": "1.00"}
        )
        assert response.status_code == 403

    def test_manager_cannot_delete(self, manager_client, service):
        response = manager_client.delete(reverse("services:service-detail", args=[service.id]))
        assert response.status_code == 403

    def test_manager_cannot_deactivate_or_activate(self, manager_client, service):
        assert (
            manager_client.post(reverse("services:service-deactivate", args=[service.id])).status_code
            == 403
        )
        assert (
            manager_client.post(reverse("services:service-activate", args=[service.id])).status_code
            == 403
        )

    def test_anonymous_denied(self, api_client):
        assert api_client.get(reverse("services:service-list")).status_code == 401


class TestCategoryFiltering:
    @pytest.fixture(autouse=True)
    def catalog(self, db):
        for code, category in [
            ("DLY-1", "daily"), ("MON-1", "monthly"),
            ("INS-1", "installment"), ("ONL-1", "online"),
        ]:
            Service.objects.create(
                name=code, code=code, category=category, fee=Decimal("1000.00")
            )

    @pytest.mark.parametrize("category", ["daily", "monthly", "installment", "online"])
    def test_each_wizard_gets_only_its_category(self, manager_client, category):
        response = manager_client.get(reverse("services:service-list"), {"category": category})

        results = response.json()["results"]
        assert len(results) == 1
        assert all(item["category"] == category for item in results)

    def test_no_filter_returns_everything(self, manager_client):
        assert manager_client.get(reverse("services:service-list")).json()["count"] == 4


class TestDiscountPricing:
    @pytest.mark.money
    def test_original_fee_above_fee_round_trips(self, admin_client):
        response = admin_client.post(
            reverse("services:service-list"),
            service_payload(fee="12600.00", original_fee="14000.00"),
        )

        body = response.json()
        assert body["fee"] == "12600.00"
        assert body["originalFee"] == "14000.00"

    def test_omitted_original_fee_is_null(self, admin_client):
        response = admin_client.post(reverse("services:service-list"), service_payload())
        assert response.json()["originalFee"] is None


class TestNoRegistrationFee:
    """
    Registration is free. A money field displayed but never charged is worse
    than none — staff assume it was collected and reconciliation drifts.
    """

    def test_serializer_has_no_registration_fee_field(self, admin_client, service):
        response = admin_client.get(reverse("services:service-detail", args=[service.id]))
        assert "registrationFee" not in response.json()
        assert "registration_fee" not in response.json()

    def test_posting_a_registration_fee_is_ignored(self, admin_client):
        response = admin_client.post(
            reverse("services:service-list"),
            service_payload(registration_fee="1000.00", registrationFee="1000.00"),
        )

        assert response.status_code == 201
        assert not hasattr(Service.objects.get(code="PKG-M1-01"), "registration_fee")
        assert "registrationFee" not in response.json()


class TestDeleteBlockedWhileInUse:
    """
    The highest-value group here. "Cannot delete" alone strands the admin, so
    the refusal must carry the affected count and point at deactivation.
    """

    def test_delete_blocked_by_an_active_monthly_enrollment(
        self, admin_client, manager, branch, service, patient_factory
    ):
        from apps.enrollments.services import create_monthly_enrollment

        create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient_factory(), service=service
        )

        response = admin_client.delete(reverse("services:service-detail", args=[service.id]))

        assert response.status_code == 400
        assert response.json()["activeEnrollmentCount"] == 1
        assert Service.objects.filter(pk=service.id).exists()

    def test_delete_blocked_by_an_active_installment_plan(
        self, admin_client, manager, branch, patient_factory
    ):
        from apps.enrollments.services import create_installment_plan

        service = Service.objects.create(
            name="Assessment", code="PKG-ASM-01",
            category=Service.Category.INSTALLMENT, fee=Decimal("18500.00"),
        )
        create_installment_plan(
            actor=manager, branch=branch, patient=patient_factory(),
            service=service, number_of_installments=3,
        )

        response = admin_client.delete(reverse("services:service-detail", args=[service.id]))

        assert response.status_code == 400
        assert response.json()["activeEnrollmentCount"] == 1

    def test_error_message_names_the_count(
        self, admin_client, manager, branch, service, patient_factory
    ):
        from apps.enrollments.services import create_monthly_enrollment

        for _ in range(3):
            create_monthly_enrollment(
                actor=manager, branch=branch, patient=patient_factory(), service=service
            )

        response = admin_client.delete(reverse("services:service-detail", args=[service.id]))

        assert response.json()["activeEnrollmentCount"] == 3
        assert "3 patients" in response.json()["detail"]
        assert "eactivate" in response.json()["detail"]  # points at the alternative

    def test_delete_allowed_once_enrollments_are_terminated(
        self, admin_client, manager, branch, service, patient_factory
    ):
        from apps.enrollments.models import EnrollmentStatus
        from apps.enrollments.services import create_monthly_enrollment

        enrollment = create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient_factory(), service=service
        )
        enrollment.status = EnrollmentStatus.TERMINATED
        enrollment.save(update_fields=["status"])

        response = admin_client.delete(reverse("services:service-detail", args=[service.id]))
        assert response.status_code == 204

    def test_delete_allowed_with_no_enrollments(self, admin_client, service):
        assert (
            admin_client.delete(reverse("services:service-detail", args=[service.id])).status_code
            == 204
        )


class TestDeactivateVersusDelete:
    def test_deactivate_succeeds_even_with_active_enrollments(
        self, admin_client, manager, branch, service, patient_factory
    ):
        from apps.enrollments.services import create_monthly_enrollment

        create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient_factory(), service=service
        )

        response = admin_client.post(reverse("services:service-deactivate", args=[service.id]))

        assert response.status_code == 200
        service.refresh_from_db()
        assert service.is_active is False

    def test_existing_enrollments_keep_working_after_deactivation(
        self, admin_client, manager, branch, service, patient_factory
    ):
        """
        The entire point of deactivation. If bills stopped generating or
        payments started failing, retiring a package would break the plans of
        everyone already on it.
        """
        from apps.enrollments.services import collect_bill_payment, create_monthly_enrollment

        enrollment = create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient_factory(), service=service
        )
        admin_client.post(reverse("services:service-deactivate", args=[service.id]))

        bill = enrollment.oldest_unpaid_bill()
        payment, bill = collect_bill_payment(
            actor=manager, branch=branch, bill=bill, method="cash"
        )

        assert payment.amount == service.fee
        assert bill.is_settled

    def test_deactivated_service_hidden_from_wizard_listing(
        self, admin_client, manager_client, service
    ):
        admin_client.post(reverse("services:service-deactivate", args=[service.id]))

        response = manager_client.get(
            reverse("services:service-list"), {"category": "monthly"}
        )
        assert response.json()["count"] == 0

    def test_admin_sees_inactive_with_include_flag(self, admin_client, service):
        admin_client.post(reverse("services:service-deactivate", args=[service.id]))

        response = admin_client.get(
            reverse("services:service-list"), {"includeInactive": "true"}
        )
        assert response.json()["count"] == 1
        assert response.json()["results"][0]["isActive"] is False

    def test_manager_cannot_see_inactive_even_with_the_flag(
        self, admin_client, manager_client, service
    ):
        """Inactive visibility is Admin-only; the flag must not be a bypass."""
        admin_client.post(reverse("services:service-deactivate", args=[service.id]))

        response = manager_client.get(
            reverse("services:service-list"), {"includeInactive": "true"}
        )
        assert response.json()["count"] == 0

    def test_reactivating_makes_it_selectable_again(self, admin_client, manager_client, service):
        admin_client.post(reverse("services:service-deactivate", args=[service.id]))
        admin_client.post(reverse("services:service-activate", args=[service.id]))

        assert manager_client.get(reverse("services:service-list")).json()["count"] == 1


class TestSoftDelete:
    def test_deleted_service_hidden_even_with_include_inactive(self, admin_client, service):
        admin_client.delete(reverse("services:service-detail", args=[service.id]))

        response = admin_client.get(
            reverse("services:service-list"), {"includeInactive": "true"}
        )
        assert response.json()["count"] == 0

    def test_history_still_resolves_a_deleted_service_name(
        self, admin_client, manager, branch, service, patient_factory
    ):
        """
        A receipt reprinted years later must not degrade to "Unknown service",
        which is why deletion is soft and FKs still resolve.
        """
        from apps.enrollments.models import EnrollmentStatus
        from apps.enrollments.services import collect_bill_payment, create_monthly_enrollment

        enrollment = create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient_factory(), service=service
        )
        payment, _ = collect_bill_payment(
            actor=manager, branch=branch, bill=enrollment.oldest_unpaid_bill(), method="cash"
        )
        enrollment.status = EnrollmentStatus.TERMINATED
        enrollment.save(update_fields=["status"])

        admin_client.delete(reverse("services:service-detail", args=[service.id]))

        payment.refresh_from_db()
        assert "Individual Therapy" in payment.description


class TestEnrollmentCounts:
    def test_counts_active_monthly_and_installment_together(
        self, manager_client, manager, branch, service, patient_factory
    ):
        from apps.enrollments.services import create_installment_plan, create_monthly_enrollment

        for _ in range(2):
            create_monthly_enrollment(
                actor=manager, branch=branch, patient=patient_factory(), service=service
            )
        create_installment_plan(
            actor=manager, branch=branch, patient=patient_factory(),
            service=service, number_of_installments=2,
        )

        counts = manager_client.get(reverse("services:service-enrollment-counts")).json()
        assert counts[str(service.id)] == 3

    def test_terminated_enrollments_are_not_counted(
        self, manager_client, manager, branch, service, patient_factory
    ):
        from apps.enrollments.models import EnrollmentStatus
        from apps.enrollments.services import create_monthly_enrollment

        enrollment = create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient_factory(), service=service
        )
        enrollment.status = EnrollmentStatus.TERMINATED
        enrollment.save(update_fields=["status"])

        counts = manager_client.get(reverse("services:service-enrollment-counts")).json()
        assert str(service.id) not in counts

    def test_service_with_no_enrollments_is_absent(self, manager_client, service):
        """
        Absent rather than 0 — the card renders nothing for undefined, so an
        explicit zero would just add noise. Pinned so it stays consistent.
        """
        counts = manager_client.get(reverse("services:service-enrollment-counts")).json()
        assert str(service.id) not in counts

    @pytest.mark.slow
    def test_counts_use_a_bounded_number_of_queries(
        self, manager_client, manager, branch, patient_factory, django_assert_max_num_queries
    ):
        """A per-service loop would scale with the catalogue; two aggregates don't."""
        from apps.enrollments.services import create_monthly_enrollment

        for index in range(5):
            svc = Service.objects.create(
                name=f"S{index}", code=f"S-{index}",
                category=Service.Category.MONTHLY, fee=Decimal("1000.00"),
            )
            create_monthly_enrollment(
                actor=manager, branch=branch, patient=patient_factory(), service=svc
            )

        with django_assert_max_num_queries(6):
            manager_client.get(reverse("services:service-enrollment-counts"))


@pytest.mark.isolation
class TestCatalogIsNotBranchScoped:
    """
    The deliberate exception to branch isolation. Asserted explicitly so a
    future "add branch scoping everywhere" change can't silently split the
    shared catalog into per-branch ones.
    """

    def test_managers_of_different_branches_see_the_same_catalog(
        self, manager_client, other_manager_client, service
    ):
        mine = manager_client.get(reverse("services:service-list")).json()
        theirs = other_manager_client.get(reverse("services:service-list")).json()

        assert mine["count"] == theirs["count"] == 1
        assert mine["results"][0]["id"] == theirs["results"][0]["id"]

    def test_a_manager_can_read_a_service_by_id_regardless_of_branch(
        self, other_manager_client, service
    ):
        response = other_manager_client.get(
            reverse("services:service-detail", args=[service.id])
        )
        assert response.status_code == 200


class TestServiceReview:
    """A Manager's proposed package stays invisible to enrollment until Admin reviews it."""

    def _propose(self, manager_client, **overrides):
        response = manager_client.post(
            reverse("services:service-list"), service_payload(**overrides)
        )
        assert response.status_code == 201
        return response.json()["id"]

    def test_pending_package_is_excluded_from_the_default_list(self, manager_client):
        """The default list (no ?includePending) is what enrollment wizards call — never show an unapproved package there."""
        self._propose(manager_client)

        response = manager_client.get(reverse("services:service-list"))

        assert response.json()["count"] == 0

    def test_manager_sees_their_own_pending_proposal_with_includepending(self, manager_client):
        self._propose(manager_client)

        response = manager_client.get(
            reverse("services:service-list"), {"includePending": "true"}
        )

        assert response.json()["count"] == 1
        assert response.json()["results"][0]["reviewStatus"] == "pending"

    def test_manager_does_not_see_another_managers_pending_proposal(
        self, manager_client, other_manager_client
    ):
        self._propose(other_manager_client, code="PKG-OTHER-01")

        response = manager_client.get(
            reverse("services:service-list"), {"includePending": "true"}
        )

        assert response.json()["count"] == 0

    def test_admin_sees_every_pending_proposal_with_includepending(
        self, admin_client, manager_client, other_manager_client
    ):
        self._propose(manager_client, code="PKG-MINE-01")
        self._propose(other_manager_client, code="PKG-THEIRS-01")

        response = admin_client.get(
            reverse("services:service-list"), {"includePending": "true"}
        )

        assert response.json()["count"] == 2

    def test_manager_cannot_review(self, manager_client):
        service_id = self._propose(manager_client)

        response = manager_client.post(
            reverse("services:service-review", args=[service_id]), {"approve": True}
        )

        assert response.status_code == 403

    def test_admin_approving_makes_the_package_immediately_enrollable(
        self, admin_client, manager_client
    ):
        service_id = self._propose(manager_client)

        response = admin_client.post(
            reverse("services:service-review", args=[service_id]), {"approve": True}
        )
        assert response.status_code == 200
        assert response.json()["reviewStatus"] == "approved"

        listed = manager_client.get(reverse("services:service-list")).json()
        assert listed["count"] == 1

    def test_admin_rejecting_requires_a_reason(self, admin_client, manager_client):
        service_id = self._propose(manager_client)

        response = admin_client.post(
            reverse("services:service-review", args=[service_id]), {"approve": False}
        )

        assert response.status_code == 400
        assert response.json()["code"] == "note_required"

    def test_admin_rejecting_with_a_reason_succeeds(self, admin_client, manager_client):
        service_id = self._propose(manager_client)

        response = admin_client.post(
            reverse("services:service-review", args=[service_id]),
            {"approve": False, "reviewNote": "Duplicate of an existing package."},
        )

        assert response.status_code == 200
        assert response.json()["reviewStatus"] == "rejected"
        assert response.json()["reviewNote"] == "Duplicate of an existing package."

    def test_cannot_review_an_already_decided_package(self, admin_client, manager_client):
        service_id = self._propose(manager_client)
        admin_client.post(reverse("services:service-review", args=[service_id]), {"approve": True})

        response = admin_client.post(
            reverse("services:service-review", args=[service_id]), {"approve": True}
        )

        assert response.status_code == 400
        assert response.json()["code"] == "not_pending"

    def test_admin_created_service_is_approved_immediately(self, admin_client):
        response = admin_client.post(reverse("services:service-list"), service_payload())

        assert response.status_code == 201
        assert response.json()["reviewStatus"] == "approved"


class TestPendingCount:
    """Powers the sidebar's Services badge — Admin-only, org-wide, live regardless of what page is open."""

    def _propose(self, manager_client, **overrides):
        response = manager_client.post(
            reverse("services:service-list"), service_payload(**overrides)
        )
        assert response.status_code == 201

    def test_admin_sees_the_total_pending_count_across_every_branch(
        self, admin_client, manager_client, other_manager_client
    ):
        self._propose(manager_client, code="PKG-BADGE-01")
        self._propose(other_manager_client, code="PKG-BADGE-02")

        response = admin_client.get(reverse("services:service-pending-count"))

        assert response.status_code == 200
        assert response.json()["count"] == 2

    def test_manager_cannot_read_the_pending_count(self, manager_client):
        response = manager_client.get(reverse("services:service-pending-count"))
        assert response.status_code == 403

    def test_reviewing_decreases_the_count(self, admin_client, manager_client):
        self._propose(manager_client, code="PKG-BADGE-03")
        service_id = manager_client.get(
            reverse("services:service-list"), {"includePending": "true"}
        ).json()["results"][0]["id"]

        admin_client.post(
            reverse("services:service-review", args=[service_id]), {"approve": True}
        )

        response = admin_client.get(reverse("services:service-pending-count"))
        assert response.json()["count"] == 0
