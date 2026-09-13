"""
A Manager changing an existing package: permission first, then the change.

The rules that matter:
  * no approval, no change — edit, delete, deactivate and activate are all
    refused for a Manager without one, whatever the screen shows;
  * an approval is one use of one action on one package, for the Manager who
    asked, and it lapses;
  * a change that fails does not spend the approval;
  * Admin still acts directly.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.enrollments import services as enrollment_services
from apps.notifications.models import Notification
from apps.services.models import PackageActionRequest, Service

pytestmark = pytest.mark.django_db

REQUESTS = "/api/services/action-requests/"


def detail(service):
    return f"/api/services/{service.pk}/"


def ask(client, service, action, reason="Fee changed at head office"):
    return client.post(
        f"/api/services/{service.pk}/request-action/",
        {"action": action, "reason": reason},
        format="json",
    )


def approve(client, request_id, note=""):
    return client.post(f"{REQUESTS}{request_id}/approve/", {"reviewNote": note}, format="json")


def reject(client, request_id, note=""):
    return client.post(f"{REQUESTS}{request_id}/reject/", {"reviewNote": note}, format="json")


@pytest.fixture
def service(db, branch):
    return Service.objects.create(
        branch=branch,
        name="Individual Therapy",
        code="MON-INDIV",
        category=Service.Category.MONTHLY,
        fee=Decimal("5000.00"),
    )


@pytest.fixture
def approved(manager_client, admin_client, service):
    """Returns a helper that asks for `action` and has Admin approve it."""

    def _approved(action):
        created = ask(manager_client, service, action).json()
        approve(admin_client, created["id"])
        return created["id"]

    return _approved


class TestNoApprovalNoChange:
    def test_a_manager_cannot_edit_without_approval(self, manager_client, service):
        response = manager_client.patch(detail(service), {"fee": "6000.00"}, format="json")

        assert response.status_code == 403
        assert response.json()["code"] == "approval_required"
        service.refresh_from_db()
        assert service.fee == Decimal("5000.00")

    def test_nor_delete(self, manager_client, service):
        assert manager_client.delete(detail(service)).status_code == 403
        assert Service.objects.filter(pk=service.pk).exists()

    def test_nor_deactivate(self, manager_client, service):
        assert manager_client.post(f"{detail(service)}deactivate/").status_code == 403
        service.refresh_from_db()
        assert service.is_active is True

    def test_admin_still_acts_directly(self, admin_client, service):
        response = admin_client.patch(detail(service), {"fee": "6000.00"}, format="json")

        assert response.status_code == 200
        service.refresh_from_db()
        assert service.fee == Decimal("6000.00")


class TestAsking:
    def test_a_request_with_a_reason_is_created_and_admin_is_told(
        self, manager_client, service, admin_user
    ):
        response = ask(manager_client, service, "edit")

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "pending"
        assert body["reason"] == "Fee changed at head office"
        assert Notification.objects.filter(recipient=admin_user).exists()

    def test_a_reason_is_required(self, manager_client, service):
        assert ask(manager_client, service, "edit", reason="").status_code == 400

    def test_the_same_question_cannot_be_asked_twice(self, manager_client, service):
        ask(manager_client, service, "edit")

        again = ask(manager_client, service, "edit")

        assert again.status_code == 400
        assert again.json()["code"] == "already_pending"

    def test_a_change_that_would_do_nothing_is_refused(self, manager_client, service):
        response = ask(manager_client, service, "activate")

        assert response.status_code == 400
        assert response.json()["code"] == "already_active"

    def test_a_proposal_awaiting_review_cannot_be_changed_this_way(
        self, manager_client, service
    ):
        Service.objects.filter(pk=service.pk).update(review_status=Service.ReviewStatus.PENDING)

        response = ask(manager_client, service, "edit")

        assert response.status_code == 400
        assert response.json()["code"] == "not_approved"

    def test_admin_does_not_ask(self, admin_client, service):
        assert ask(admin_client, service, "edit").status_code == 403

    def test_another_branch_cannot_ask_about_this_package(self, other_manager_client, service):
        assert ask(other_manager_client, service, "edit").status_code == 404


class TestDeciding:
    def test_approving_opens_a_window_and_tells_the_manager(
        self, manager_client, admin_client, service, manager
    ):
        created = ask(manager_client, service, "edit").json()

        response = approve(admin_client, created["id"])

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approved"
        assert body["expiresAt"]
        assert Notification.objects.filter(recipient=manager, title__icontains="approved").exists()

    def test_rejecting_needs_a_reason(self, manager_client, admin_client, service):
        created = ask(manager_client, service, "edit").json()

        response = reject(admin_client, created["id"])

        assert response.status_code == 400
        assert response.json()["code"] == "note_required"

    def test_a_rejected_request_grants_nothing(self, manager_client, admin_client, service):
        created = ask(manager_client, service, "edit").json()
        reject(admin_client, created["id"], note="Keep the current fee")

        response = manager_client.patch(detail(service), {"fee": "6000.00"}, format="json")

        assert response.status_code == 403

    def test_a_manager_cannot_approve(self, manager_client, service):
        created = ask(manager_client, service, "edit").json()

        assert approve(manager_client, created["id"]).status_code == 403

    def test_a_decided_request_cannot_be_decided_again(
        self, manager_client, admin_client, service
    ):
        created = ask(manager_client, service, "edit").json()
        approve(admin_client, created["id"])

        response = reject(admin_client, created["id"], note="Changed my mind")

        assert response.status_code == 400
        assert response.json()["code"] == "not_pending"

    def test_the_pending_count_is_admin_only(self, admin_client, manager_client, service):
        ask(manager_client, service, "edit")

        assert admin_client.get(f"{REQUESTS}pending-count/").json()["count"] == 1
        assert manager_client.get(f"{REQUESTS}pending-count/").status_code == 403


class TestUsingAnApproval:
    def test_approved_edit_is_applied_and_spent(self, manager_client, approved, service):
        request_id = approved("edit")

        response = manager_client.patch(detail(service), {"fee": "6000.00"}, format="json")

        assert response.status_code == 200
        service.refresh_from_db()
        assert service.fee == Decimal("6000.00")
        assert PackageActionRequest.objects.get(pk=request_id).status == "used"

    def test_it_works_once(self, manager_client, approved, service):
        approved("edit")
        manager_client.patch(detail(service), {"fee": "6000.00"}, format="json")

        again = manager_client.patch(detail(service), {"fee": "7000.00"}, format="json")

        assert again.status_code == 403

    def test_an_edit_approval_does_not_allow_a_delete(self, manager_client, approved, service):
        approved("edit")

        assert manager_client.delete(detail(service)).status_code == 403

    def test_an_expired_approval_is_refused(self, manager_client, approved, service):
        request_id = approved("edit")
        PackageActionRequest.objects.filter(pk=request_id).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        response = manager_client.patch(detail(service), {"fee": "6000.00"}, format="json")

        assert response.status_code == 403
        listed = manager_client.get(REQUESTS).json()["results"][0]
        assert listed["status"] == "expired"

    def test_approved_deactivate_then_activate(self, manager_client, admin_client, service):
        created = ask(manager_client, service, "deactivate").json()
        approve(admin_client, created["id"])
        assert manager_client.post(f"{detail(service)}deactivate/").status_code == 200
        service.refresh_from_db()
        assert service.is_active is False

        created = ask(manager_client, service, "activate").json()
        approve(admin_client, created["id"])
        assert manager_client.post(f"{detail(service)}activate/").status_code == 200
        service.refresh_from_db()
        assert service.is_active is True

    def test_a_refused_delete_keeps_the_approval(
        self, manager_client, approved, service, manager, branch, patient_factory
    ):
        """Patients are enrolled, so the delete fails — the permission must survive it."""
        request_id = approved("delete")
        enrollment_services.create_monthly_enrollment(
            actor=manager, branch=branch, patient=patient_factory(), service=service
        )

        response = manager_client.delete(detail(service))

        assert response.status_code == 400
        assert PackageActionRequest.objects.get(pk=request_id).status == "approved"

    def test_an_invalid_edit_keeps_the_approval(self, manager_client, approved, service):
        request_id = approved("edit")

        response = manager_client.patch(detail(service), {"fee": "0"}, format="json")

        assert response.status_code == 400
        assert PackageActionRequest.objects.get(pk=request_id).status == "approved"

    def test_a_colleague_cannot_use_someone_elses_approval(
        self, approved, service, branch, authenticate
    ):
        from apps.accounts.models import User

        approved("edit")
        colleague = User.objects.create_user(
            email="second.manager@speechlab.test",
            password="pass-12345",
            name="Second Manager",
            role=User.Role.MANAGER,
            branch=branch,
        )
        client = authenticate(colleague)

        assert client.patch(detail(service), {"fee": "6000.00"}, format="json").status_code == 403


class TestListing:
    def test_open_requests_are_what_the_catalog_needs(self, manager_client, admin_client, service):
        waiting = ask(manager_client, service, "edit").json()
        spent = ask(manager_client, service, "deactivate").json()
        approve(admin_client, spent["id"])
        manager_client.post(f"{detail(service)}deactivate/")

        results = manager_client.get(REQUESTS, {"open": "true"}).json()["results"]

        assert [row["id"] for row in results] == [waiting["id"]]

    def test_a_manager_sees_only_their_branch(self, manager_client, other_manager_client, service):
        ask(manager_client, service, "edit")

        assert other_manager_client.get(REQUESTS).json()["count"] == 0


class TestManagerSeesInactivePackages:
    """Without this, a package deactivated with approval could never be requested back."""

    def test_include_inactive_shows_the_branchs_retired_packages(self, manager_client, service):
        Service.objects.filter(pk=service.pk).update(is_active=False)

        listed = manager_client.get("/api/services/", {"includeInactive": "true"}).json()["results"]

        assert [row["id"] for row in listed] == [service.id]

    def test_without_it_they_stay_hidden(self, manager_client, service):
        Service.objects.filter(pk=service.pk).update(is_active=False)

        assert manager_client.get("/api/services/").json()["count"] == 0
