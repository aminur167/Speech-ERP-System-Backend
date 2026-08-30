"""
Audit trail helper.

One entry point so every consequential action is recorded the same way. Call
`record()` inside the same `transaction.atomic()` block as the change itself —
an audit entry that survives a rolled-back change (or vice versa) is worse than
none, because it describes something that never happened.
"""

from apps.common.models import AuditLog


def record(
    *,
    actor,
    action: str,
    target,
    reason: str = "",
    changes: dict | None = None,
    branch=None,
):
    """
    Append an audit entry.

    `target` is any model instance; its class name and pk are stored rather
    than a FK, so the entry survives the target being removed.

    `branch` is inferred from the target when it has one, so callers don't have
    to remember to pass it for the common case.
    """
    if branch is None:
        branch = getattr(target, "branch", None)

    return AuditLog.objects.create(
        actor=actor if (actor and actor.is_authenticated) else None,
        actor_email=getattr(actor, "email", "") or "",
        action=action,
        target_type=target.__class__.__name__,
        target_id=str(target.pk),
        branch=branch,
        reason=reason or "",
        changes=changes or {},
    )


def diff(before: dict, after: dict) -> dict:
    """
    Field-level before/after for the changed keys only.

    Storing whole snapshots makes entries noisy and hides what actually moved,
    which is the thing a dispute usually hinges on.
    """
    changed = {}
    for key, new_value in after.items():
        old_value = before.get(key)
        if old_value != new_value:
            changed[key] = {"from": old_value, "to": new_value}
    return changed
