"""Audit log read API — Admin-only oversight across every branch."""

import pytest
from django.urls import reverse

from apps.common.audit import record
from apps.common.models import AuditLog

pytestmark = pytest.mark.django_db


@pytest.fixture
def audit_entry(branch, admin_user, patient_factory):
    patient = patient_factory()
    return record(
        actor=admin_user,
        action=AuditLog.Action.APPROVE,
        target=patient,
        reason="Within policy.",
    )


def test_manager_cannot_list_audit_logs(manager_client, audit_entry):
    response = manager_client.get(reverse("common:audit-log-list"))
    assert response.status_code == 403


def test_unauthenticated_cannot_list_audit_logs(api_client, audit_entry):
    response = api_client.get(reverse("common:audit-log-list"))
    assert response.status_code == 401


def test_admin_can_list_audit_logs(admin_client, audit_entry):
    response = admin_client.get(reverse("common:audit-log-list"))
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 1
    row = results[0]
    assert row["action"] == "approve"
    assert row["targetType"] == "Patient"
    assert row["targetId"] == str(audit_entry.target_id)
    assert row["reason"] == "Within policy."


def test_admin_can_filter_by_action(admin_client, admin_user, patient_factory):
    patient = patient_factory()
    record(actor=admin_user, action=AuditLog.Action.APPROVE, target=patient)
    record(actor=admin_user, action=AuditLog.Action.REJECT, target=patient)

    response = admin_client.get(reverse("common:audit-log-list"), {"action": "reject"})

    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["action"] == "reject"


def test_admin_sees_entries_across_every_branch(
    admin_client, branch, other_branch, admin_user, patient_factory
):
    patient_a = patient_factory(branch=branch)
    patient_b = patient_factory(branch=other_branch)
    record(actor=admin_user, action=AuditLog.Action.CREATE, target=patient_a)
    record(actor=admin_user, action=AuditLog.Action.CREATE, target=patient_b)

    response = admin_client.get(reverse("common:audit-log-list"))

    assert response.status_code == 200
    assert response.json()["count"] == 2
