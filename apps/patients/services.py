"""Patient creation, including per-branch code allocation."""

from django.db import transaction
from django.utils import timezone

from apps.branches.models import Branch
from apps.common import audit
from apps.common.models import AuditLog
from apps.common.sequences import next_value
from apps.patients.models import Patient


def build_patient_code(branch: Branch, year: int, value: int) -> str:
    """`PT-DHK-2026-00001` — branch-scoped so it can be issued offline."""
    return f"PT-{branch.short_code}-{year}-{str(value).zfill(5)}"


@transaction.atomic
def create_patient(*, actor, branch: Branch, data: dict) -> Patient:
    """
    Register a patient with a race-safe, branch-scoped code.

    The code is reserved inside this transaction so the number and the row it
    labels commit together — reserving outside would burn codes on any
    subsequent failure and leave gaps in a sequence people read.
    """
    year = timezone.localdate().year
    scope = f"patient:{branch.code}"
    value = next_value(scope, year)

    patient = Patient.objects.create(
        patient_code=build_patient_code(branch, year, value),
        branch=branch,
        created_by=actor if actor and actor.is_authenticated else None,
        **data,
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=patient,
        branch=branch,
        changes={"patient_code": patient.patient_code, "name": patient.name},
    )
    return patient


@transaction.atomic
def update_patient(*, actor, patient: Patient, data: dict) -> Patient:
    before = {field: getattr(patient, field) for field in data}

    for field, value in data.items():
        setattr(patient, field, value)
    patient.save()

    after = {field: getattr(patient, field) for field in data}

    changes = audit.diff(before, after)
    if changes:
        audit.record(
            actor=actor,
            action=AuditLog.Action.UPDATE,
            target=patient,
            branch=patient.branch,
            # Dates aren't JSON-serialisable; the audit column is JSONField.
            changes={k: {"from": str(v["from"]), "to": str(v["to"])} for k, v in changes.items()},
        )
    return patient
