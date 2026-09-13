"""
Service catalog workflows: reviewing a Manager's proposed package, and a
Manager asking permission to change an existing one.
"""

import re
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common import audit
from apps.common.models import AuditLog
from apps.notifications.inapp import notify_admins, notify_requester
from apps.services.models import PackageActionRequest, Service


class ServiceError(Exception):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


# Package codes are issued by the system, never typed. A typed code is how a
# branch ends up with both "MON-1" and "mon-01", and receipts and audit entries
# quote the code as the package's identity.
CODE_PREFIXES = {"daily": "DAY", "monthly": "MON", "installment": "INS", "online": "ONL"}
_CODE_ATTEMPTS = 5


def next_service_code(*, branch, category: str) -> str:
    """
    The branch's next free code for a category, e.g. ``MON-004``.

    Counts soft-deleted packages too — the (branch, code) constraint does — so
    a code is never reissued to a different package. Older hand-typed codes
    that don't follow the pattern are simply not counted.
    """
    prefix = CODE_PREFIXES.get(category, "PKG")
    pattern = re.compile(rf"^{prefix}-(\d+)$")
    highest = 0
    codes = Service.all_objects.filter(branch=branch, code__startswith=f"{prefix}-").values_list(
        "code", flat=True
    )
    for code in codes:
        match = pattern.match(code)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{prefix}-{highest + 1:03d}"


def save_with_next_code(*, serializer, branch, **extra) -> Service:
    """
    Save a new package under the branch's next free code.

    Two packages created in one branch at the same moment can pick the same
    number; the unique constraint refuses the second, which takes the next one.
    Each attempt is its own savepoint so the caller's transaction stays usable.
    """
    category = serializer.validated_data["category"]
    for attempt in range(_CODE_ATTEMPTS):
        try:
            with transaction.atomic():
                return serializer.save(
                    branch=branch,
                    code=next_service_code(branch=branch, category=category),
                    **extra,
                )
        except IntegrityError:
            if attempt == _CODE_ATTEMPTS - 1:
                raise
    raise AssertionError("unreachable")


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


# ---------------------------------------------------------------------------
# Manager change requests — permission first, then the change
# ---------------------------------------------------------------------------


class ApprovalRequired(ServiceError):
    """A Manager tried an action they have no live approval for."""


_VERBS = {
    PackageActionRequest.Action.EDIT: "edit",
    PackageActionRequest.Action.DELETE: "delete",
    PackageActionRequest.Action.DEACTIVATE: "deactivate",
    PackageActionRequest.Action.ACTIVATE: "activate",
}


def request_package_action(*, actor, service: Service, action: str, reason: str):
    """
    Ask Admin for permission to change a live package.

    Refused when the request could not lead anywhere useful: a package still
    awaiting its own review, an action that would change nothing, a question
    already waiting for Admin, or an approval the Manager already holds.
    """
    reason = (reason or "").strip()
    if service.review_status != Service.ReviewStatus.APPROVED:
        raise ServiceError(
            "Only a live package can be changed. This one is still a proposal.",
            code="not_approved",
        )
    if not reason:
        raise ServiceError("Say why this change is needed.", code="reason_required")
    if action == PackageActionRequest.Action.DEACTIVATE and not service.is_active:
        raise ServiceError("This package is already inactive.", code="already_inactive")
    if action == PackageActionRequest.Action.ACTIVATE and service.is_active:
        raise ServiceError("This package is already active.", code="already_active")
    if live_grant(service=service, action=action, user=actor) is not None:
        raise ServiceError(
            "You already have approval for this — go ahead and do it.",
            code="already_approved",
        )

    try:
        with transaction.atomic():
            request = PackageActionRequest.objects.create(
                service=service,
                branch=service.branch,
                action=action,
                reason=reason,
                requested_by=actor,
            )
    except IntegrityError:
        raise ServiceError(
            "A request for this is already waiting for Admin.", code="already_pending"
        ) from None

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=request,
        branch=service.branch,
        reason=reason,
        changes={"package": service.code, "action": action},
    )
    notify_admins(
        title="Package change needs approval",
        message=(
            f'{service.branch.name} wants to {_VERBS[action]} "{service.name}" '
            f"({service.code}): {reason}"
        ),
        link="/admin/package-requests",
        exclude=actor,
    )
    return request


def review_package_action(*, actor, request, approve: bool, review_note: str = ""):
    """
    Admin decides. An approval opens a window of
    PACKAGE_ACTION_GRANT_HOURS for the Manager who asked; a rejection needs a
    reason, because a bare "no" leaves them with nothing to act on.
    """
    review_note = (review_note or "").strip()
    with transaction.atomic():
        request = PackageActionRequest.objects.select_for_update().get(pk=request.pk)
        if request.status != PackageActionRequest.Status.PENDING:
            raise ServiceError(
                f"This request is already {request.effective_status}.", code="not_pending"
            )
        if not approve and not review_note:
            raise ServiceError(
                "A reason is required when rejecting a request.", code="note_required"
            )

        now = timezone.now()
        request.status = (
            PackageActionRequest.Status.APPROVED if approve else PackageActionRequest.Status.REJECTED
        )
        request.reviewed_by = actor
        request.reviewed_at = now
        request.review_note = review_note
        if approve:
            request.expires_at = now + timedelta(hours=settings.PACKAGE_ACTION_GRANT_HOURS)
        request.save(
            update_fields=["status", "reviewed_by", "reviewed_at", "review_note", "expires_at"]
        )

    service = request.service
    audit.record(
        actor=actor,
        action=AuditLog.Action.APPROVE if approve else AuditLog.Action.REJECT,
        target=request,
        branch=request.branch,
        reason=review_note,
        changes={
            "status": {"from": "pending", "to": request.status},
            "package": service.code,
            "action": request.action,
        },
    )

    verb = _VERBS[request.action]
    if approve:
        until = timezone.localtime(request.expires_at).strftime("%d %b %Y, %H:%M")
        title = "Package change approved"
        message = (
            f'You can now {verb} "{service.name}". The approval is for one use and '
            f"expires {until}."
        )
    else:
        title = "Package change rejected"
        message = f'Your request to {verb} "{service.name}" was rejected: {review_note}'
    notify_requester(
        actor=actor,
        recipient=request.requested_by,
        title=title,
        message=message,
        link="/manager/packages",
    )
    return request


def live_grant(*, service: Service, action: str, user):
    """The approval this user could spend right now on this action, if any."""
    return (
        PackageActionRequest.objects.filter(
            service=service,
            action=action,
            status=PackageActionRequest.Status.APPROVED,
            requested_by=user,
            expires_at__gt=timezone.now(),
        )
        .order_by("expires_at")
        .first()
    )


def consume_grant(*, actor, service: Service, action: str):
    """
    Spend a Manager's approval for this exact action.

    Must run inside the same transaction as the change it authorises: if the
    change fails, the rollback restores the approval, so a Manager never loses
    their permission to an error they then have to explain to Admin again.
    The permission belongs to the Manager who asked, for accountability — a
    colleague at the same branch cannot use it.
    """
    grant = (
        PackageActionRequest.objects.select_for_update()
        .filter(
            service=service,
            action=action,
            status=PackageActionRequest.Status.APPROVED,
            requested_by=actor,
            expires_at__gt=timezone.now(),
        )
        .order_by("expires_at")
        .first()
    )
    if grant is None:
        raise ApprovalRequired(
            f"Admin's approval is needed to {_VERBS[action]} this package. "
            "Send a request with your reason first.",
            code="approval_required",
        )
    grant.status = PackageActionRequest.Status.USED
    grant.used_at = timezone.now()
    grant.save(update_fields=["status", "used_at"])
    return grant
