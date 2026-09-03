"""Service catalog review workflow — approving/rejecting a Manager's proposed package."""

from django.utils import timezone

from apps.common import audit
from apps.common.models import AuditLog
from apps.notifications.inapp import notify_requester
from apps.services.models import Service


class ServiceError(Exception):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


def review_service(*, actor, service: Service, approve: bool, review_note: str = "") -> Service:
    """
    Approve or reject a Manager's proposed package.

    Mirrors apps.expenses.services.review_expense's shape (including requiring
    a reason to reject) -- same decision, same reasoning: a rejection with no
    explanation leaves the Manager who proposed it with nothing to act on.
    """
    if service.review_status != Service.ReviewStatus.PENDING:
        raise ServiceError(
            f"This package is already {service.review_status}, not pending review.",
            code="not_pending",
        )

    if not approve and not review_note.strip():
        raise ServiceError(
            "A reason is required when rejecting a proposed package.", code="note_required"
        )

    service.review_status = (
        Service.ReviewStatus.APPROVED if approve else Service.ReviewStatus.REJECTED
    )
    service.review_note = review_note
    service.reviewed_by = actor
    service.reviewed_at = timezone.now()
    service.save(
        update_fields=["review_status", "review_note", "reviewed_by", "reviewed_at"]
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.APPROVE if approve else AuditLog.Action.REJECT,
        target=service,
        reason=review_note,
        changes={"review_status": {"from": "pending", "to": service.review_status}},
    )

    if approve:
        title = "Package approved"
        message = f'"{service.name}" ({service.code}) is now live and enrollable.'
    else:
        title = "Package rejected"
        message = f'"{service.name}" ({service.code}) was rejected: {review_note}'

    notify_requester(
        actor=actor,
        recipient=service.proposed_by,
        title=title,
        message=message,
        link="/manager/packages",
    )
    return service
