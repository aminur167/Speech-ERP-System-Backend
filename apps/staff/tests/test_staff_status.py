"""
Marking a staff member inactive from the row menu, and deleting them.

Inactive is the everyday choice — someone left or is on long leave — and keeps
their history. Both are branch-desk actions, so Manager only.
"""

import pytest

from apps.common.models import AuditLog
from apps.staff.models import StaffMember

pytestmark = pytest.mark.django_db


def status_url(member, verb):
    return f"/api/staff/{member.pk}/{verb}/"


class TestStaffStatus:
    def test_manager_deactivates_and_reactivates(self, manager_client, staff_member_factory):
        member = staff_member_factory()

        assert manager_client.post(status_url(member, "deactivate")).status_code == 200
        member.refresh_from_db()
        assert member.status == StaffMember.Status.INACTIVE

        assert manager_client.post(status_url(member, "activate")).status_code == 200
        member.refresh_from_db()
        assert member.status == StaffMember.Status.ACTIVE

    def test_the_change_is_audited(self, manager_client, staff_member_factory):
        member = staff_member_factory()

        manager_client.post(status_url(member, "deactivate"))

        entry = AuditLog.objects.filter(action=AuditLog.Action.UPDATE).latest("created_at")
        assert entry.changes["status"] == {"from": "active", "to": "inactive"}

    def test_admin_cannot_change_staff_status(self, admin_client, staff_member_factory):
        member = staff_member_factory()

        assert admin_client.post(status_url(member, "deactivate")).status_code == 403

    def test_another_branch_cannot_reach_the_member(
        self, other_manager_client, staff_member_factory
    ):
        member = staff_member_factory()

        assert other_manager_client.post(status_url(member, "deactivate")).status_code == 404


class TestDeletingStaff:
    def test_the_delete_is_audited(self, manager_client, staff_member_factory):
        member = staff_member_factory(name="Leaving Soon")

        response = manager_client.delete(f"/api/staff/{member.pk}/")

        assert response.status_code == 204
        assert not StaffMember.objects.filter(pk=member.pk).exists()
        entry = AuditLog.objects.filter(action=AuditLog.Action.SOFT_DELETE).latest("created_at")
        assert entry.changes["name"] == "Leaving Soon"
