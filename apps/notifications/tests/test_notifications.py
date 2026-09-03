"""In-app notification API, and the triggers that create them from the package-review workflow."""

import pytest
from django.urls import reverse

from apps.notifications.inapp import notify
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db


class TestNotificationApi:
    def test_a_user_only_sees_their_own_notifications(self, manager, other_manager):
        notify(recipient=manager, title="Mine", message="For me")
        notify(recipient=other_manager, title="Theirs", message="Not for me")

        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import RefreshToken

        client = APIClient()
        token = RefreshToken.for_user(manager)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")

        response = client.get(reverse("notifications:notification-list"))

        assert response.status_code == 200
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["title"] == "Mine"

    def test_unread_count(self, manager_client, manager):
        notify(recipient=manager, title="A", message="a")
        notify(recipient=manager, title="B", message="b")

        response = manager_client.get(reverse("notifications:notification-unread-count"))

        assert response.status_code == 200
        assert response.json()["count"] == 2

    def test_marking_one_read_decreases_the_unread_count(self, manager_client, manager):
        n = notify(recipient=manager, title="A", message="a")
        notify(recipient=manager, title="B", message="b")

        response = manager_client.post(
            reverse("notifications:notification-read", args=[n.id])
        )
        assert response.status_code == 200
        assert response.json()["isRead"] is True

        count = manager_client.get(
            reverse("notifications:notification-unread-count")
        ).json()["count"]
        assert count == 1

    def test_cannot_mark_someone_elses_notification_read(
        self, manager_client, other_manager
    ):
        n = notify(recipient=other_manager, title="Not yours", message="x")

        response = manager_client.post(
            reverse("notifications:notification-read", args=[n.id])
        )

        assert response.status_code == 404

    def test_mark_all_read(self, manager_client, manager):
        notify(recipient=manager, title="A", message="a")
        notify(recipient=manager, title="B", message="b")

        response = manager_client.post(reverse("notifications:notification-mark-all-read"))

        assert response.status_code == 200
        assert response.json()["updated"] == 2
        assert Notification.objects.filter(recipient=manager, is_read=False).count() == 0


class TestPackageReviewNotifications:
    def test_proposing_a_package_notifies_every_admin(self, manager_client, admin_user):
        from apps.services.tests.test_services import service_payload as make_payload

        response = manager_client.post(
            reverse("services:service-list"), make_payload()
        )
        assert response.status_code == 201

        notes = Notification.objects.filter(recipient=admin_user)
        assert notes.count() == 1
        assert "proposed" in notes.first().message.lower()

    def test_approving_notifies_the_proposer(self, admin_client, manager_client, manager):
        from apps.services.tests.test_services import service_payload as make_payload

        created = manager_client.post(
            reverse("services:service-list"), make_payload()
        ).json()

        admin_client.post(
            reverse("services:service-review", args=[created["id"]]), {"approve": True}
        )

        notes = Notification.objects.filter(recipient=manager, title="Package approved")
        assert notes.count() == 1

    def test_rejecting_notifies_the_proposer_with_the_reason(
        self, admin_client, manager_client, manager
    ):
        from apps.services.tests.test_services import service_payload as make_payload

        created = manager_client.post(
            reverse("services:service-list"), make_payload()
        ).json()

        admin_client.post(
            reverse("services:service-review", args=[created["id"]]),
            {"approve": False, "reviewNote": "Not needed."},
        )

        notes = Notification.objects.filter(recipient=manager, title="Package rejected")
        assert notes.count() == 1
        assert "Not needed." in notes.first().message


class TestPackageEditNotifications:
    """A branch's manager finds out when Admin edits that branch's package."""

    def test_editing_notifies_the_branch_manager_with_what_changed(
        self, admin_client, manager, service_factory
    ):
        service = service_factory()

        response = admin_client.patch(
            reverse("services:service-detail", args=[service.id]), {"fee": "7500.00"}
        )
        assert response.status_code == 200

        note = Notification.objects.filter(
            recipient=manager, title="Package updated by Admin"
        ).first()
        assert note is not None
        assert service.name in note.message
        assert "7500.00" in note.message

    def test_another_branchs_manager_is_not_notified(
        self, admin_client, other_manager, service_factory
    ):
        service = service_factory()

        admin_client.patch(
            reverse("services:service-detail", args=[service.id]), {"fee": "7500.00"}
        )

        assert not Notification.objects.filter(recipient=other_manager).exists()

    def test_an_edit_that_changes_nothing_notifies_nobody(
        self, admin_client, manager, service_factory
    ):
        """Saving the form untouched must not ping the branch for no reason."""
        service = service_factory()

        admin_client.patch(
            reverse("services:service-detail", args=[service.id]), {"name": service.name}
        )

        assert not Notification.objects.filter(recipient=manager).exists()
