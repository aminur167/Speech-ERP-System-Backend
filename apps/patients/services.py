"""Patient creation, including per-branch code allocation."""

from django.db import transaction
from django.utils import timezone

from apps.branches.models import Branch
from apps.common import audit
from apps.common.models import AuditLog
from apps.common.sequences import next_value
from apps.common.transactions import transactional
from apps.patients.models import Patient


def build_patient_code(branch: Branch, year: int, value: int) -> str:
    """`PT-DHK-2026-00001` — branch-scoped so it can be issued offline."""
    return f"PT-{branch.short_code}-{year}-{str(value).zfill(5)}"


@transactional
def create_patient(
    *, actor, branch: Branch, data: dict,
    idempotency_key: str | None = None, client_created_at=None,
) -> Patient:
    """
    Register a patient with a race-safe, branch-scoped code.

    The code is reserved inside this transaction so the number and the row it
    labels commit together — reserving outside would burn codes on any
    subsequent failure and leave gaps in a sequence people read.

    A replayed idempotency key (a network retry, an offline-queue flush)
    must return the SAME patient rather than register them twice — checked
    before the code is even drawn, or a retry would burn a second
    patient_code for no reason.
    """
    if idempotency_key:
        existing = Patient.all_objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing

    year = timezone.localdate().year
    scope = f"patient:{branch.code}"
    value = next_value(scope, year)

    patient = Patient.objects.create(
        patient_code=build_patient_code(branch, year, value),
        branch=branch,
        created_by=actor if actor and actor.is_authenticated else None,
        idempotency_key=idempotency_key or None,
        client_created_at=client_created_at,
        **data,
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=patient,
        branch=branch,
        changes={"patient_code": patient.patient_code, "name": patient.name},
    )
    # Tells the caller this patient was registered by this very call (not a
    # replayed key handing back an existing one): nothing can be owed yet.
    patient.just_created = True
    return patient


@transaction.atomic
def update_patient(*, actor, patient: Patient, data: dict) -> Patient:
    # Imported here, not at module scope: serializers.py imports this module,
    # so a top-level import would close the cycle.
    from apps.patients.serializers import REQUIRED_ON_CREATE

    tracked = dict.fromkeys(data)
    before = {field: getattr(patient, field) for field in tracked}

    for field, value in data.items():
        setattr(patient, field, value)

    # A walk-in who comes back and is properly registered stops being one.
    # Promoting them on the edit that completes their details keeps the flag
    # meaning "this person's details were never taken" rather than decaying
    # into "this row started at the counter", which nobody needs to know.
    if patient.is_walk_in and all(getattr(patient, field) for field in REQUIRED_ON_CREATE):
        before["is_walk_in"] = True
        tracked["is_walk_in"] = None
        patient.is_walk_in = False

    patient.save()

    after = {field: getattr(patient, field) for field in tracked}

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
