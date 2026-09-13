"""
Deactivating and deleting a branch from its menu.

Deleting is refused while a branch still has active patient services or
active staff — hiding it would leave billing and payroll running somewhere
nobody can open. Deactivating is always available and is the suggested
alternative.
"""

from decimal import Decimal

import pytest

from apps.branches.models import Branch
from apps.common.models import AuditLog
from apps.enrollments import services as enrollment_services

pytestmark = pytest.mark.django_db


def detail(branch):
    return f"/api/branches/{branch.pk}/"


def status_url(branch, verb):
    return f"/api/branches/{branch.pk}/{verb}/"


class TestDeletingABranch:
    def test_an_empty_branch_is_deleted_and_audited(self, admin_client, other_branch):
        response = admin_client.delete(detail(other_branch))

        assert response.status_code == 204
        assert not Branch.objects.filter(pk=other_branch.pk).exists()
        entry = AuditLog.objects.filter(action=AuditLog.Action.SOFT_DELETE).latest("created_at")
        assert entry.changes["code"] == other_branch.code

    def test_refused_while_staff_are_active(self, admin_client, branch, staff_member_factory):
        staff_member_factory()

        response = admin_client.delete(detail(branch))

        assert response.status_code == 400
        body = response.json()
        assert body["code"] == "branch_in_use"
        assert body["activeStaffCount"] == 1
        assert Branch.objects.filter(pk=branch.pk).exists()

    def test_refused_while_a_patient_service_is_active(
        self, admin_client, manager, branch, patient_factory, service_factory
    ):
        enrollment_services.create_monthly_enrollment(
            actor=manager,
            branch=branch,
            patient=patient_factory(),
            service=service_factory(fee=Decimal("5000.00")),
        )

        response = admin_client.delete(detail(branch))

        assert response.status_code == 400
        assert response.json()["activeServiceCount"] == 1

    def test_a_manager_cannot_delete_a_branch(self, manager_client, branch):
        assert manager_client.delete(detail(branch)).status_code == 403


class TestBranchStatus:
    def test_admin_deactivates_and_reactivates(self, admin_client, branch):
        off = admin_client.post(status_url(branch, "deactivate"))
        assert off.status_code == 200
        branch.refresh_from_db()
        assert branch.status == Branch.Status.INACTIVE

        on = admin_client.post(status_url(branch, "activate"))
        assert on.status_code == 200
        branch.refresh_from_db()
        assert branch.status == Branch.Status.ACTIVE

    def test_the_change_is_audited_with_before_and_after(self, admin_client, branch):
        admin_client.post(status_url(branch, "deactivate"))

        entry = AuditLog.objects.filter(action=AuditLog.Action.UPDATE).latest("created_at")
        assert entry.changes["status"] == {"from": "active", "to": "inactive"}

    def test_repeating_it_records_nothing_new(self, admin_client, branch):
        admin_client.post(status_url(branch, "deactivate"))
        count = AuditLog.objects.filter(action=AuditLog.Action.UPDATE).count()

        admin_client.post(status_url(branch, "deactivate"))

        assert AuditLog.objects.filter(action=AuditLog.Action.UPDATE).count() == count

    def test_a_manager_cannot_change_branch_status(self, manager_client, branch):
        assert manager_client.post(status_url(branch, "deactivate")).status_code == 403
