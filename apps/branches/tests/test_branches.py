"""
Branch tests.

The important ones here aren't the CRUD paths — they're the boundaries:
managers must not manage branches, must not see other branches, and the
manager password must never be readable back through the API.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.branches.models import Branch
from apps.common.models import AuditLog

pytestmark = pytest.mark.django_db


def branch_payload(**overrides):
    payload = {
        "name": "Sylhet Branch",
        "code": "BR-SYL-001",
        "status": "active",
        "address": "Zindabazar, Sylhet 3100",
        "phone": "+880 821-715522",
        "manager_name": "Imran Hossain",
        "manager_email": "manager.syl@speechlab.test",
        "manager_code": "MGR-SYL-001",
        "manager_password": "sylhet-pass-123",
        "therapist_count": 9,
        "support_count": 4,
        "opened_at": "2024-06-01",
    }
    payload.update(overrides)
    return payload


class TestBranchCreation:
    def test_admin_creates_branch_and_manager_together(self, admin_client):
        response = admin_client.post(reverse("branches:branch-list"), branch_payload())

        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Sylhet Branch"
        assert body["managerEmail"] == "manager.syl@speechlab.test"

        branch = Branch.objects.get(code="BR-SYL-001")
        assert branch.manager is not None
        assert branch.manager.role == User.Role.MANAGER
        assert branch.manager.branch_id == branch.id

    def test_created_manager_can_actually_log_in(self, admin_client, api_client):
        """
        Provisioning is only correct if the resulting credentials work — the
        mock kept a password column that was never a real login.
        """
        admin_client.post(reverse("branches:branch-list"), branch_payload())

        response = api_client.post(
            reverse("accounts:login"),
            {"email": "manager.syl@speechlab.test", "password": "sylhet-pass-123"},
        )
        assert response.status_code == 200

    def test_manager_password_is_hashed(self, admin_client):
        admin_client.post(reverse("branches:branch-list"), branch_payload())

        manager = User.objects.get(email="manager.syl@speechlab.test")
        assert manager.password != "sylhet-pass-123"
        assert manager.check_password("sylhet-pass-123")

    def test_password_never_appears_in_any_response(self, admin_client):
        """The mock returned managerPassword and the UI displayed it. Not here."""
        create = admin_client.post(reverse("branches:branch-list"), branch_payload())
        listing = admin_client.get(reverse("branches:branch-list"))
        detail = admin_client.get(
            reverse("branches:branch-detail", args=[create.json()["id"]])
        )

        for response in (create, listing, detail):
            body = response.content.decode()
            assert "sylhet-pass-123" not in body
            assert "managerPassword" not in body

    def test_creation_is_atomic(self, admin_client, manager):
        """
        A duplicate manager email must leave no half-built branch behind — no
        branch without a login, no user attached to nothing.
        """
        before = Branch.objects.count()

        response = admin_client.post(
            reverse("branches:branch-list"),
            branch_payload(manager_email=manager.email),
        )

        assert response.status_code == 400
        assert Branch.objects.count() == before
        assert not Branch.objects.filter(code="BR-SYL-001").exists()

    def test_duplicate_branch_code_rejected(self, admin_client, branch):
        response = admin_client.post(
            reverse("branches:branch-list"), branch_payload(code=branch.code)
        )
        assert response.status_code == 400

    def test_password_required_on_create(self, admin_client):
        payload = branch_payload()
        del payload["manager_password"]

        response = admin_client.post(reverse("branches:branch-list"), payload)
        assert response.status_code == 400

    def test_short_password_rejected(self, admin_client):
        response = admin_client.post(
            reverse("branches:branch-list"), branch_payload(manager_password="short")
        )
        assert response.status_code == 400

    def test_creation_is_audited(self, admin_client, admin_user):
        admin_client.post(reverse("branches:branch-list"), branch_payload())

        entry = AuditLog.objects.filter(
            action=AuditLog.Action.CREATE, target_type="Branch"
        ).first()
        assert entry is not None
        assert entry.actor_id == admin_user.id


class TestBranchUpdate:
    def test_admin_updates_branch_details(self, admin_client, branch, manager):
        payload = branch_payload(
            name="Dhaka Main Branch (Renamed)",
            code=branch.code,
            manager_email=branch.manager.email,
            manager_name=branch.manager.name,
        )
        del payload["manager_password"]

        response = admin_client.put(
            reverse("branches:branch-detail", args=[branch.id]), payload
        )

        assert response.status_code == 200
        branch.refresh_from_db()
        assert branch.name == "Dhaka Main Branch (Renamed)"

    def test_blank_password_keeps_existing_one(self, admin_client, branch, manager):
        """
        Editing a phone number shouldn't force retyping the manager's password
        — and the server can't pre-fill it, since it only stores a hash.
        """
        payload = branch_payload(
            name=branch.name,
            code=branch.code,
            manager_email=manager.email,
            manager_name=manager.name,
            manager_password="",
        )

        admin_client.put(reverse("branches:branch-detail", args=[branch.id]), payload)

        manager.refresh_from_db()
        assert manager.check_password("manager-pass-123")

    def test_supplying_a_password_changes_it(self, admin_client, branch, manager):
        payload = branch_payload(
            name=branch.name,
            code=branch.code,
            manager_email=manager.email,
            manager_name=manager.name,
            manager_password="rotated-pass-789",
        )

        admin_client.put(reverse("branches:branch-detail", args=[branch.id]), payload)

        manager.refresh_from_db()
        assert manager.check_password("rotated-pass-789")

    def test_rotated_password_invalidates_the_old_one(
        self, admin_client, api_client, branch, manager
    ):
        """Rotation only counts if the previous password stops working."""
        payload = branch_payload(
            name=branch.name,
            code=branch.code,
            manager_email=manager.email,
            manager_name=manager.name,
            manager_password="rotated-pass-789",
        )
        admin_client.put(reverse("branches:branch-detail", args=[branch.id]), payload)

        old = api_client.post(
            reverse("accounts:login"),
            {"email": manager.email, "password": "manager-pass-123"},
        )
        new = api_client.post(
            reverse("accounts:login"),
            {"email": manager.email, "password": "rotated-pass-789"},
        )

        assert old.status_code == 401
        assert new.status_code == 200

    def test_changing_manager_email_changes_the_login(
        self, admin_client, api_client, branch, manager
    ):
        payload = branch_payload(
            name=branch.name,
            code=branch.code,
            manager_email="renamed.manager@speechlab.test",
            manager_name=manager.name,
        )
        del payload["manager_password"]

        admin_client.put(reverse("branches:branch-detail", args=[branch.id]), payload)

        manager.refresh_from_db()
        assert manager.email == "renamed.manager@speechlab.test"

        response = api_client.post(
            reverse("accounts:login"),
            {"email": "renamed.manager@speechlab.test", "password": "manager-pass-123"},
        )
        assert response.status_code == 200

    def test_email_taken_by_another_account_rejected(
        self, admin_client, branch, other_manager
    ):
        payload = branch_payload(
            name=branch.name, code=branch.code, manager_email=other_manager.email
        )
        del payload["manager_password"]

        response = admin_client.put(
            reverse("branches:branch-detail", args=[branch.id]), payload
        )
        assert response.status_code == 400


class TestBranchPermissions:
    @pytest.mark.isolation
    def test_manager_sees_only_their_own_branch(
        self, manager_client, branch, other_branch
    ):
        response = manager_client.get(reverse("branches:branch-list"))

        assert response.status_code == 200
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["id"] == branch.id

    @pytest.mark.isolation
    def test_manager_cannot_retrieve_another_branch_by_id(
        self, manager_client, other_branch
    ):
        """
        The classic IDOR: list is scoped correctly but detail is left open, so
        another branch's row leaks to anyone who guesses the id.
        """
        response = manager_client.get(
            reverse("branches:branch-detail", args=[other_branch.id])
        )
        assert response.status_code == 404

    def test_admin_sees_all_branches(self, admin_client, branch, other_branch):
        response = admin_client.get(reverse("branches:branch-list"))
        assert response.json()["count"] == 2

    def test_manager_cannot_create_branch(self, manager_client):
        response = manager_client.post(reverse("branches:branch-list"), branch_payload())
        assert response.status_code == 403

    def test_manager_cannot_update_own_branch(self, manager_client, branch, manager):
        payload = branch_payload(
            name="Self-promoted", code=branch.code, manager_email=manager.email
        )
        del payload["manager_password"]

        response = manager_client.put(
            reverse("branches:branch-detail", args=[branch.id]), payload
        )
        assert response.status_code == 403

    def test_manager_cannot_delete_branch(self, manager_client, branch):
        response = manager_client.delete(reverse("branches:branch-detail", args=[branch.id]))
        assert response.status_code == 403

    def test_anonymous_denied(self, api_client):
        assert api_client.get(reverse("branches:branch-list")).status_code == 401


class TestBranchDeletion:
    def test_delete_is_soft(self, admin_client, branch):
        """
        A branch parents every patient, payment, and closing beneath it.
        Removing the row would take that history with it.
        """
        response = admin_client.delete(reverse("branches:branch-detail", args=[branch.id]))

        assert response.status_code == 204
        assert not Branch.objects.filter(pk=branch.id).exists()      # hidden
        assert Branch.all_objects.filter(pk=branch.id).exists()      # retained

        branch.refresh_from_db()
        assert branch.is_deleted is True
        assert branch.deleted_at is not None


class TestBranchOverview:
    def test_admin_gets_overview_for_all_branches(
        self, admin_client, branch, other_branch
    ):
        response = admin_client.get(reverse("branches:branch-overview"))

        assert response.status_code == 200
        assert len(response.json()) == 2
        assert "patientCount" in response.json()[0]

    def test_admin_gets_overview_for_a_single_branch(self, admin_client, branch):
        response = admin_client.get(
            reverse("branches:branch-overview-detail", args=[branch.id])
        )

        assert response.status_code == 200
        assert response.json()["branch"]["id"] == branch.id

    @pytest.mark.isolation
    def test_manager_denied_overview(self, manager_client):
        """
        Admin-only: the overview exposes cross-branch revenue, which is not a
        manager's to see even for their own branch.
        """
        response = manager_client.get(reverse("branches:branch-overview"))
        assert response.status_code == 403

    @pytest.mark.isolation
    def test_manager_denied_single_branch_overview(self, manager_client, branch):
        response = manager_client.get(
            reverse("branches:branch-overview-detail", args=[branch.id])
        )
        assert response.status_code == 403

    def test_empty_branch_returns_zeros_not_an_error(self, admin_client, other_branch):
        response = admin_client.get(
            reverse("branches:branch-overview-detail", args=[other_branch.id])
        )

        assert response.status_code == 200
        body = response.json()
        assert body["patientCount"] == 0
        assert body["totalCollected"] == "0.00"
        assert body["monthlyRevenue"] == "0.00"


class TestBranchModel:
    def test_short_code_extracted_for_per_branch_sequences(self, branch):
        """Receipt numbers are per-branch (RCPT-DHK-2026-00001)."""
        assert branch.short_code == "DHK"

    def test_short_code_falls_back_when_code_is_unusual(self, db):
        odd = Branch.objects.create(
            name="Odd", code="SINGLE", status=Branch.Status.ACTIVE,
            address="a", phone="p", opened_at="2024-01-01",
        )
        assert odd.short_code == "SINGLE"
