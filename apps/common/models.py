"""
Base models shared across the project.

See docs/00-OVERVIEW.md for the rules these encode — soft delete on financial
and medical records, and a tamper-evident audit trail for money-moving and
approval actions.
"""

import uuid

from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    """Creation/modification timestamps on every record."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SoftDeleteQuerySet(models.QuerySet):
    def alive(self):
        return self.filter(is_deleted=False)

    def dead(self):
        return self.filter(is_deleted=True)

    def delete(self):
        """Soft-delete in bulk. Use hard_delete() when a real delete is meant."""
        return self.update(is_deleted=True, deleted_at=models.functions.Now())

    def hard_delete(self):
        return super().delete()


class SoftDeleteManager(models.Manager):
    """
    Default manager that hides soft-deleted rows.

    `all_objects` on each model exposes everything, which admin/audit paths and
    historical joins need — a receipt must still resolve a deleted service's
    name (docs/03-services-catalog.md).
    """

    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).filter(is_deleted=False)


class SoftDeleteModel(models.Model):
    """
    Never hard-delete financial or medical records.

    Deleting a patient or a payment destroys history the clinic may need years
    later — for a dispute, an audit, or a reprint. Deletion here means hiding.
    """

    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = SoftDeleteManager()
    all_objects = models.Manager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        from django.utils import timezone

        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=["is_deleted", "deleted_at"])

    def hard_delete(self, using=None, keep_parents=False):
        super().delete(using=using, keep_parents=keep_parents)

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        self.save(update_fields=["is_deleted", "deleted_at"])


class CodeSequence(models.Model):
    """
    Counter rows backing race-safe sequential codes (see apps/common/sequences.py).

    One row per (scope, year). `scope` identifies what's being numbered and for
    whom — e.g. "receipt:BR-DHK-001" or "expense". Per-branch scopes exist so a
    branch can issue receipt numbers offline without coordinating with any
    other branch (docs/04-payments-core.md).
    """

    scope = models.CharField(max_length=64)
    year = models.IntegerField()
    last_value = models.BigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["scope", "year"], name="uniq_sequence_scope_year")
        ]
        indexes = [models.Index(fields=["scope", "year"])]

    def __str__(self):
        return f"{self.scope}/{self.year} = {self.last_value}"


class AuditLog(models.Model):
    """
    Append-only record of consequential actions: who did what, when, and why.

    Distinct from the domain tables' own fields (an expense's `review_note`, a
    closing's amendment row). Those show current state; this is the history
    that survives even when current state is overwritten. Never update or
    delete a row here.
    """

    class Action(models.TextChoices):
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        SOFT_DELETE = "soft_delete", "Soft delete"
        APPROVE = "approve", "Approve"
        REJECT = "reject", "Reject"
        VOID = "void", "Void"
        REFUND_REQUEST = "refund_request", "Refund requested"
        REFUND_APPROVE = "refund_approve", "Refund approved"
        REFUND_REJECT = "refund_reject", "Refund rejected"
        TERMINATE = "terminate", "Terminate"
        AMEND = "amend", "Amend"
        WRITE_OFF = "write_off", "Write off"
        LOGIN = "login", "Login"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,  # keep the entry even if the user is removed
        related_name="audit_entries",
    )
    # Denormalised so the trail stays readable even if the user row changes.
    actor_email = models.CharField(max_length=254, blank=True)

    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)

    # Target identified loosely rather than by FK: a generic relation would
    # block deleting the target, and a hard FK per model would need a column
    # per table. The label+id pair is enough to find the record later.
    target_type = models.CharField(max_length=64, db_index=True)
    target_id = models.CharField(max_length=64, db_index=True)

    branch = models.ForeignKey(
        "branches.Branch",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
    )

    reason = models.TextField(blank=True)
    # Before/after snapshots of whatever changed, for actions where that matters.
    changes = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["-created_at"]),
        ]

    def __str__(self):
        return f"{self.action} {self.target_type}#{self.target_id} by {self.actor_email or 'system'}"
