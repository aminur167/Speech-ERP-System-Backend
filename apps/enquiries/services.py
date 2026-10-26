"""
Enquiry business logic: recording a call, filtering the list, and turning a
caller into a registered patient.
"""

from datetime import date as date_cls

from django.db.models import Count, Q
from django.utils import timezone

from apps.common import audit
from apps.common.models import AuditLog
from apps.common.sequences import format_code, next_value
from apps.common.transactions import transactional
from apps.common.validators import normalize_phone
from apps.enquiries.models import Enquiry


class EnquiryError(Exception):
    """Business-rule violation in an enquiry operation."""

    def __init__(self, message: str, *, code: str = "invalid", extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra or {}


def build_enquiry_code(branch, year: int, value: int) -> str:
    """`ENQ-DHK-2026-00001` — per-branch, like every other code here."""
    return format_code(f"ENQ-{branch.short_code}", year, value)


@transactional
def create_enquiry(*, actor, branch, data: dict) -> Enquiry:
    """
    Record a call.

    The code is drawn inside this transaction so the number and the row it
    labels commit together — reserving outside would burn codes on any later
    failure and leave gaps in a sequence people read.
    """
    year = timezone.localdate().year
    value = next_value(f"enquiry:{branch.code}", year)

    enquiry = Enquiry.objects.create(
        enquiry_code=build_enquiry_code(branch, year, value),
        branch=branch,
        taken_by=actor if actor and actor.is_authenticated else None,
        called_at=data.pop("called_at", None) or timezone.now(),
        **data,
    )

    audit.record(
        actor=actor,
        action=AuditLog.Action.CREATE,
        target=enquiry,
        branch=branch,
        changes={
            "caller": enquiry.caller_name,
            "phone": enquiry.caller_phone,
            "source": enquiry.source,
        },
    )
    return enquiry


@transactional
def update_enquiry(*, actor, enquiry: Enquiry, data: dict) -> Enquiry:
    before = {field: getattr(enquiry, field) for field in data}

    for field, value in data.items():
        setattr(enquiry, field, value)
    enquiry.save()

    after = {field: getattr(enquiry, field) for field in data}
    changes = audit.diff(before, after)
    if changes:
        audit.record(
            actor=actor,
            action=AuditLog.Action.UPDATE,
            target=enquiry,
            branch=enquiry.branch,
            changes={
                key: {"from": str(value["from"]), "to": str(value["to"])}
                for key, value in changes.items()
            },
        )
    return enquiry


@transactional
def convert_to_patient(*, actor, enquiry: Enquiry, patient_data: dict):
    """
    Register the caller as a patient and tie the two together.

    The link is what makes the ad report possible — without it "enrolled" is
    a status someone remembered to set, not a fact that can be counted
    against the source that produced it.

    Refused on an enquiry already converted: a second patient row for the
    same caller would double-count the ad that brought them.
    """
    from apps.patients import services as patient_services

    if enquiry.is_converted:
        raise EnquiryError(
            "This enquiry has already been registered as a patient.",
            code="already_converted",
            extra={"patientId": str(enquiry.converted_patient_id)},
        )

    patient = patient_services.create_patient(
        actor=actor, branch=enquiry.branch, data=patient_data
    )

    enquiry.converted_patient = patient
    enquiry.status = Enquiry.Status.CONVERTED
    # Nobody is waiting on a call back once they have enrolled.
    enquiry.follow_up_on = None
    enquiry.save(update_fields=["converted_patient", "status", "follow_up_on", "updated_at"])

    audit.record(
        actor=actor,
        action=AuditLog.Action.UPDATE,
        target=enquiry,
        branch=enquiry.branch,
        reason=f"Registered as {patient.patient_code}",
        changes={"status": {"from": "—", "to": Enquiry.Status.CONVERTED}},
    )
    return enquiry, patient


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _parse_date(value):
    try:
        return date_cls.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def filter_enquiries(queryset, params):
    """
    Everything the Enquiries toolbar can narrow by.

    `status` and `followUp` are deliberately separate: the first says where
    someone is in the funnel, the second says whether today's work includes
    them. A manager opening the page filters on the second; an admin
    measuring an ad filters on the first.
    """
    today = timezone.localdate()

    status = (params.get("status") or "").strip()
    if status:
        queryset = queryset.filter(status=status)

    source = (params.get("source") or "").strip()
    if source:
        queryset = queryset.filter(source=source)

    follow_up = (params.get("followUp") or "").strip()
    if follow_up:
        # Closed enquiries are excluded from every "needs a call" bucket --
        # someone who enrolled or said no is not outstanding work.
        open_only = ~Q(status__in=Enquiry.CLOSED_STATUSES)
        if follow_up == "today":
            queryset = queryset.filter(open_only, follow_up_on=today)
        elif follow_up == "overdue":
            queryset = queryset.filter(open_only, follow_up_on__lt=today)
        elif follow_up == "upcoming":
            queryset = queryset.filter(open_only, follow_up_on__gt=today)
        elif follow_up == "none":
            queryset = queryset.filter(follow_up_on__isnull=True)

    date_from = _parse_date(params.get("dateFrom"))
    if date_from:
        queryset = queryset.filter(called_at__date__gte=date_from)

    date_to = _parse_date(params.get("dateTo"))
    if date_to:
        queryset = queryset.filter(called_at__date__lte=date_to)

    term = (params.get("search") or "").strip()
    if term:
        lookup = (
            Q(caller_name__icontains=term)
            | Q(patient_name__icontains=term)
            | Q(caller_phone__icontains=term)
            | Q(concern__icontains=term)
            | Q(enquiry_code__icontains=term)
            | Q(source_detail__icontains=term)
        )
        normalized = normalize_phone(term)
        if normalized and normalized != term:
            lookup |= Q(caller_phone__icontains=normalized)
        queryset = queryset.filter(lookup)

    return queryset


def enquiry_summary(queryset) -> dict:
    """
    The counts the page header shows, over whatever the filters left.

    One aggregate rather than several: the header asks four questions of the
    same rows, and asking them separately would be four scans of a table
    that grows by every call the clinic ever takes.
    """
    today = timezone.localdate()
    open_only = ~Q(status__in=Enquiry.CLOSED_STATUSES)

    counts = queryset.aggregate(
        total=Count("pk"),
        converted=Count("pk", filter=Q(status=Enquiry.Status.CONVERTED)),
        due_today=Count("pk", filter=Q(follow_up_on=today) & open_only),
        overdue=Count("pk", filter=Q(follow_up_on__lt=today) & open_only),
    )

    # Which source brought the most of these -- the client's actual question.
    top = (
        queryset.values("source")
        .annotate(n=Count("pk"))
        .order_by("-n")
        .first()
    )

    return {
        "total": counts["total"],
        "converted": counts["converted"],
        "dueToday": counts["due_today"],
        "overdue": counts["overdue"],
        "topSource": top["source"] if top else "",
        "topSourceCount": top["n"] if top else 0,
    }
