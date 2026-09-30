"""
A Manager's request to delete a package, shown and decided on the catalog.

The package's row carries its open delete request (`deleteRequest`), which
the Services page shows as the status in place of "Available" and which
Admin approves or rejects from the row's own menu. Rejecting it returns the
package to plain Available; approving it lets that Manager delete it once.
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


def ask_to_delete(manager, service, reason="No longer offered"):
    return catalog.request_package_action(
        actor=manager, service=service, action="delete", reason=reason
    )


class TestTheCatalogShowsIt:
    def test_a_package_with_no_request_has_none(self, admin_client, service_factory):
        package = service_factory()

        assert row_for(catalog_rows(admin_client), package)["deleteRequest"] is None

    def test_a_pending_request_is_shown_to_admin(
        self, admin_client, manager, service_factory
    ):
        package = service_factory()
        request = ask_to_delete(manager, package, reason="Duplicate of MON-001")

        info = row_for(catalog_rows(admin_client), package)["deleteRequest"]

        assert info["id"] == str(request.pk)
        assert info["status"] == "pending"
        assert info["reason"] == "Duplicate of MON-001"
        assert info["requestedBy"] == manager.name

    def test_the_manager_sees_it_on_their_own_catalog(
        self, manager_client, manager, service_factory
    ):
        package = service_factory()
        ask_to_delete(manager, package)

        assert row_for(catalog_rows(manager_client), package)["deleteRequest"]["status"] == "pending"

    def test_a_package_awaiting_deletion_goes_on_top(
        self, admin_client, manager, service_factory
    ):
        service_factory(category=Service.Category.DAILY, name="A first by name")
        doomed = service_factory(category=Service.Category.ONLINE, name="Z last by name")
        ask_to_delete(manager, doomed)

        assert catalog_rows(admin_client)[0]["id"] == doomed.pk

    def test_other_kinds_of_request_do_not_show_as_a_delete(
        self, admin_client, manager, service_factory
    ):
        package = service_factory()
        catalog.request_package_action(
            actor=manager, service=package, action="edit", reason="Fee change"
        )

        assert row_for(catalog_rows(admin_client), package)["deleteRequest"] is None

    def test_one_query_for_every_row(self, admin_client, manager, service_factory):
        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                catalog_rows(admin_client)
            return len(ctx.captured_queries)

        for _ in range(3):
            ask_to_delete(manager, service_factory())
        few = count_queries()
        for _ in range(10):
            ask_to_delete(manager, service_factory())

        assert count_queries() == few


class TestDecidingItFromTheCatalog:
    def test_approving_shows_it_approved_and_lets_that_manager_delete(
        self, admin_user, admin_client, manager, manager_client, service_factory
    ):
        package = service_factory()
        request = ask_to_delete(manager, package)

        response = admin_client.post(f"{REQUESTS}{request.pk}/approve/", {}, format="json")
        assert response.status_code == 200, response.json()

        info = row_for(catalog_rows(admin_client), package)["deleteRequest"]
        assert info["status"] == "approved"
        assert info["expiresAt"] is not None

        deleted = manager_client.delete(reverse("services:service-detail", args=[package.pk]))
        assert deleted.status_code == 204
        assert not Service.objects.filter(pk=package.pk).exists()

    def test_rejecting_puts_it_back_to_available(
        self, admin_client, manager, manager_client, service_factory
    ):
        package = service_factory()
        request = ask_to_delete(manager, package)

        response = admin_client.post(
            f"{REQUESTS}{request.pk}/reject/", {"reviewNote": "Still in use"}, format="json"
        )
        assert response.status_code == 200, response.json()

        row = row_for(catalog_rows(admin_client), package)
        assert row["deleteRequest"] is None
        assert row["isActive"] is True
        # ...and the Manager still cannot delete it.
        refused = manager_client.delete(reverse("services:service-detail", args=[package.pk]))
        assert refused.status_code != 204
        assert Service.objects.filter(pk=package.pk).exists()

    def test_rejecting_still_needs_a_reason(self, admin_client, manager, service_factory):
        request = ask_to_delete(manager, service_factory())

        response = admin_client.post(f"{REQUESTS}{request.pk}/reject/", {}, format="json")

        assert response.status_code == 400

    def test_an_approval_left_unused_until_it_lapses_shows_nothing(
        self, admin_user, admin_client, manager, service_factory
    ):
        package = service_factory()
        request = ask_to_delete(manager, package)
        catalog.review_package_action(actor=admin_user, request=request, approve=True)
        PackageActionRequest.objects.filter(pk=request.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )

        assert row_for(catalog_rows(admin_client), package)["deleteRequest"] is None

    def test_a_manager_cannot_decide_it(self, manager_client, manager, service_factory):
        request = ask_to_delete(manager, service_factory())

        response = manager_client.post(f"{REQUESTS}{request.pk}/approve/", {}, format="json")

        assert response.status_code == 403


class TestCountsAndThePackageRequestsPage:
    def test_the_services_badge_counts_delete_requests(
        self, admin_client, manager, service_factory
    ):
        before = admin_client.get(reverse("services:service-pending-count")).json()["count"]
        ask_to_delete(manager, service_factory())

        after = admin_client.get(reverse("services:service-pending-count")).json()["count"]

        assert after == before + 1

    def test_the_package_requests_badge_leaves_them_out(
        self, admin_client, manager, service_factory
    ):
        ask_to_delete(manager, service_factory())
        catalog.request_package_action(
            actor=manager, service=service_factory(), action="edit", reason="Fee change"
        )

        count = admin_client.get(f"{REQUESTS}pending-count/").json()["count"]

        assert count == 1

    def test_the_package_requests_list_can_leave_them_out(
        self, admin_client, manager, service_factory
    ):
        ask_to_delete(manager, service_factory())
        edit = catalog.request_package_action(
            actor=manager, service=service_factory(), action="edit", reason="Fee change"
        )

        body = admin_client.get(REQUESTS, {"excludeAction": "delete"}).json()
        rows = body["results"] if isinstance(body, dict) else body

        assert [row["id"] for row in rows] == [edit.pk]
