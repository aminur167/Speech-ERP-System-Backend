"""
Managers' requests to change a package, shown and decided on the catalog.

There is no separate requests page. Each package's row carries its open
change requests (`changeRequests` — edit, delete, deactivate, activate;
waiting for Admin, or approved and not yet used), which the Services page
shows as the package's status and which Admin approves or rejects from the
row's own menu. Rejecting returns the package to plain Available; approving
lets that Manager make the change once. The most recent request is on top.
"""

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.services import services as catalog
from apps.services.models import PackageActionRequest, Service

pytestmark = pytest.mark.django_db

CATALOG = reverse("services:service-list")
REQUESTS = "/api/services/action-requests/"


def catalog_rows(client, **params):
    body = client.get(CATALOG, {"includePending": "true", "includeInactive": "true", **params})
    assert body.status_code == 200, body.json()
    data = body.json()
    return data["results"] if isinstance(data, dict) else data


def row_for(rows, service):
    return next(row for row in rows if row["id"] == service.pk)


def ask(manager, service, action="delete", reason="No longer offered"):
    return catalog.request_package_action(
        actor=manager, service=service, action=action, reason=reason
    )


def backdate(request, minutes):
    PackageActionRequest.objects.filter(pk=request.pk).update(
        created_at=timezone.now() - timedelta(minutes=minutes)
    )


class TestTheCatalogShowsThem:
    def test_a_package_with_no_request_has_none(self, admin_client, service_factory):
        package = service_factory()

        assert row_for(catalog_rows(admin_client), package)["changeRequests"] == []

    def test_a_pending_request_is_shown_to_admin(
        self, admin_client, manager, service_factory
    ):
        package = service_factory()
        request = ask(manager, package, reason="Duplicate of MON-001")

        [info] = row_for(catalog_rows(admin_client), package)["changeRequests"]

        assert info["id"] == str(request.pk)
        assert info["action"] == "delete"
        assert info["status"] == "pending"
        assert info["reason"] == "Duplicate of MON-001"
        assert info["requestedBy"] == manager.name

    @pytest.mark.parametrize("action", ["edit", "deactivate"])
    def test_every_kind_of_request_is_shown(self, admin_client, manager, service_factory, action):
        package = service_factory()
        ask(manager, package, action=action, reason="Because")

        [info] = row_for(catalog_rows(admin_client), package)["changeRequests"]

        assert info["action"] == action

    def test_an_inactive_packages_activate_request_is_shown(
        self, admin_client, manager, service_factory
    ):
        package = service_factory(is_active=False)
        ask(manager, package, action="activate", reason="Back in demand")

        [info] = row_for(catalog_rows(admin_client), package)["changeRequests"]

        assert info["action"] == "activate"

    def test_several_open_requests_are_all_shown_newest_first(
        self, admin_client, manager, service_factory
    ):
        package = service_factory()
        older = ask(manager, package, action="edit", reason="Fee")
        backdate(older, 10)
        newer = ask(manager, package, action="delete", reason="Gone")

        requests = row_for(catalog_rows(admin_client), package)["changeRequests"]

        assert [r["id"] for r in requests] == [str(newer.pk), str(older.pk)]

    def test_the_manager_sees_them_on_their_own_catalog(
        self, manager_client, manager, service_factory
    ):
        package = service_factory()
        ask(manager, package)

        [info] = row_for(catalog_rows(manager_client), package)["changeRequests"]
        assert info["status"] == "pending"

    def test_one_query_for_every_row(self, admin_client, manager, service_factory):
        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                catalog_rows(admin_client)
            return len(ctx.captured_queries)

        for _ in range(3):
            ask(manager, service_factory())
        few = count_queries()
        for _ in range(10):
            ask(manager, service_factory(), action="edit", reason="Fee")

        assert count_queries() == few


class TestLatestOnTop:
    def test_a_package_awaiting_a_decision_goes_above_the_rest(
        self, admin_client, manager, service_factory
    ):
        service_factory(category=Service.Category.DAILY, name="A first by name")
        waiting = service_factory(category=Service.Category.ONLINE, name="Z last by name")
        ask(manager, waiting, action="edit", reason="Fee")

        assert catalog_rows(admin_client)[0]["id"] == waiting.pk

    def test_the_most_recent_request_comes_first(
        self, admin_client, manager, service_factory
    ):
        # Category/name order alone would put "A" first.
        first_asked = service_factory(category=Service.Category.DAILY, name="A asked first")
        last_asked = service_factory(category=Service.Category.ONLINE, name="Z asked last")
        older = ask(manager, first_asked)
        backdate(older, 30)
        ask(manager, last_asked)

        rows = catalog_rows(admin_client)

        assert [rows[0]["id"], rows[1]["id"]] == [last_asked.pk, first_asked.pk]

    def test_a_new_proposal_and_requests_share_one_newest_first_order(
        self, admin_client, manager, service_factory
    ):
        requested = service_factory(name="Asked earlier")
        older = ask(manager, requested, action="edit", reason="Fee")
        backdate(older, 30)
        proposal = service_factory(
            name="Proposed just now", review_status=Service.ReviewStatus.PENDING
        )

        rows = catalog_rows(admin_client)

        assert [rows[0]["id"], rows[1]["id"]] == [proposal.pk, requested.pk]


class TestDecidingFromTheCatalog:
    def test_approving_shows_it_approved_and_lets_that_manager_delete(
        self, admin_client, manager, manager_client, service_factory
    ):
        package = service_factory()
        request = ask(manager, package)

        response = admin_client.post(f"{REQUESTS}{request.pk}/approve/", {}, format="json")
        assert response.status_code == 200, response.json()

        [info] = row_for(catalog_rows(admin_client), package)["changeRequests"]
        assert info["status"] == "approved"
        assert info["expiresAt"] is not None

        deleted = manager_client.delete(reverse("services:service-detail", args=[package.pk]))
        assert deleted.status_code == 204
        assert not Service.objects.filter(pk=package.pk).exists()

    def test_approving_an_edit_lets_that_manager_edit(
        self, admin_client, manager, manager_client, service_factory
    ):
        package = service_factory()
        request = ask(manager, package, action="edit", reason="Fee")
        admin_client.post(f"{REQUESTS}{request.pk}/approve/", {}, format="json")

        edited = manager_client.patch(
            reverse("services:service-detail", args=[package.pk]), {"fee": "6500.00"}
        )

        assert edited.status_code == 200, edited.json()
        assert row_for(catalog_rows(admin_client), package)["changeRequests"] == []

    def test_rejecting_puts_it_back_to_available(
        self, admin_client, manager, manager_client, service_factory
    ):
        package = service_factory()
        request = ask(manager, package)

        response = admin_client.post(
            f"{REQUESTS}{request.pk}/reject/", {"reviewNote": "Still in use"}, format="json"
        )
        assert response.status_code == 200, response.json()

        row = row_for(catalog_rows(admin_client), package)
        assert row["changeRequests"] == []
        assert row["isActive"] is True
        refused = manager_client.delete(reverse("services:service-detail", args=[package.pk]))
        assert refused.status_code != 204
        assert Service.objects.filter(pk=package.pk).exists()

    def test_rejecting_still_needs_a_reason(self, admin_client, manager, service_factory):
        request = ask(manager, service_factory())

        response = admin_client.post(f"{REQUESTS}{request.pk}/reject/", {}, format="json")

        assert response.status_code == 400

    def test_an_approval_left_unused_until_it_lapses_shows_nothing(
        self, admin_user, admin_client, manager, service_factory
    ):
        package = service_factory()
        request = ask(manager, package)
        catalog.review_package_action(actor=admin_user, request=request, approve=True)
        PackageActionRequest.objects.filter(pk=request.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        assert row_for(catalog_rows(admin_client), package)["changeRequests"] == []

    def test_a_manager_cannot_decide_it(self, manager_client, manager, service_factory):
        request = ask(manager, service_factory())

        response = manager_client.post(f"{REQUESTS}{request.pk}/approve/", {}, format="json")

        assert response.status_code == 403


class TestTheBadgeAndTheNotification:
    def test_the_services_badge_counts_every_kind_of_request(
        self, admin_client, manager, service_factory
    ):
        before = admin_client.get(reverse("services:service-pending-count")).json()["count"]
        ask(manager, service_factory())
        ask(manager, service_factory(), action="edit", reason="Fee")

        after = admin_client.get(reverse("services:service-pending-count")).json()["count"]

        assert after == before + 2

    def test_the_admin_notification_links_to_the_services_page(
        self, admin_user, manager, service_factory
    ):
        from apps.notifications.models import Notification

        ask(manager, service_factory())

        note = Notification.objects.filter(recipient=admin_user).latest("created_at")
        assert note.link == "/admin/services"
