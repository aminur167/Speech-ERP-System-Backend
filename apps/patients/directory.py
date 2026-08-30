"""
Patient Directory — the denormalized listing from docs/02.

This is the most performance-sensitive read in the system, and the naive
implementation is a textbook N+1: one query for patients, then per patient a
query for enrollments, one for plans, one for the latest payment, and two more
for the category and method sets. At twelve demo rows that is invisible; at
50,000 patients it is fatal.

So the whole page is assembled in a fixed number of queries regardless of page
size:

  1. the patient page itself (with `branch` joined),
  2. active monthly enrollments for those patients,
  3. active installment plans for those patients,
  4. one grouped pass over payments giving every patient's distinct methods,
  5. one grouped pass over payments giving every patient's distinct categories,
  6. the latest payment method per patient.

Steps 2-6 are keyed by patient id into dictionaries, so building a row is
dictionary lookups rather than database round trips. `TestDirectoryPerformance`
pins this with a query-count bound: adding a patient to the page must not add
a query.
"""

from collections import defaultdict
from datetime import date

from django.db.models import Max, Q

from apps.enrollments.models import EnrollmentStatus, InstallmentPlan, MonthlyEnrollment
from apps.patients.models import Patient
from apps.payments.models import Payment, PaymentCategory, PaymentStatus

# What the frontend renders when a patient has no service or has never paid.
# An em dash rather than an empty string, so a missing value looks deliberate
# in the table instead of looking like a rendering failure.
EMPTY = "—"


class Status:
    ACTIVE_CARE = "active-care"
    IN_PROGRESS = "in-progress"
    ACTION_NEEDED = "action-needed"


# Display labels for payment methods. The stored value is the key
# (`bank_transfer`); this is the only place the backend spells the label, and
# it matches src/utils/paymentMethod.ts.
METHOD_LABELS = {
    "cash": "Cash",
    "bkash": "bKash",
    "nagad": "Nagad",
    "rocket": "Rocket",
    "bank_transfer": "Bank Transfer",
    "online_payment": "Online Payment",
    "card": "Card",
}


def _method_label(method: str) -> str:
    return METHOD_LABELS.get(method, method.replace("_", " ").title() if method else EMPTY)


def build_rows(patients: list[Patient]) -> list[dict]:
    """
    Denormalize one page of patients.

    Takes an already-sliced list, not a queryset — the pagination has to
    happen before the joins, or the bulk lookups below would load the whole
    table's worth of related rows to render ten of them.
    """
    if not patients:
        return []

    ids = [p.id for p in patients]

    monthly = _active_monthly_by_patient(ids)
    installment = _active_installment_by_patient(ids)
    methods, categories, latest_method = _payment_facts(ids)

    rows = []
    for patient in patients:
        monthly_service = monthly.get(patient.id)
        installment_service = installment.get(patient.id)

        # Monthly wins over installment for both fields — a patient in
        # ongoing therapy is described by that therapy, even if they are also
        # paying off a package.
        if monthly_service:
            status = Status.ACTIVE_CARE
            therapy_type = monthly_service
        elif installment_service:
            status = Status.IN_PROGRESS
            therapy_type = installment_service
        else:
            status = Status.ACTION_NEEDED
            therapy_type = EMPTY

        rows.append(
            {
                "id": patient.id,
                "patientCode": patient.patient_code,
                "name": patient.name,
                "age": patient.age,
                "gender": patient.gender,
                "guardianName": patient.guardian_name,
                "guardianRelation": patient.guardian_relation,
                "phone": patient.phone,
                "branchId": str(patient.branch_id),
                # Resolved from the FK. Never a hardcoded id-to-name map —
                # that exact bug hit the frontend twice.
                "branchName": patient.branch.name,
                "therapyType": therapy_type,
                "paymentType": _method_label(latest_method.get(patient.id, "")),
                "status": status,
                "serviceCategories": sorted(categories.get(patient.id, set())),
                "paymentMethods": sorted(methods.get(patient.id, set())),
                "createdAt": patient.created_at,
            }
        )
    return rows


def _active_monthly_by_patient(ids) -> dict:
    """Service name of each patient's active monthly enrollment, newest first."""
    rows = (
        MonthlyEnrollment.objects.filter(
            patient_id__in=ids, status=EnrollmentStatus.ACTIVE
        )
        .select_related("service")
        .order_by("patient_id", "-created_at")
        .values_list("patient_id", "service__name")
    )
    # The first row per patient wins, so a patient holding several active
    # enrollments is described by the most recent.
    found = {}
    for patient_id, service_name in rows:
        found.setdefault(patient_id, service_name)
    return found


def _active_installment_by_patient(ids) -> dict:
    rows = (
        InstallmentPlan.objects.filter(patient_id__in=ids, status=EnrollmentStatus.ACTIVE)
        .select_related("service")
        .order_by("patient_id", "-created_at")
        .values_list("patient_id", "service__name")
    )
    found = {}
    for patient_id, service_name in rows:
        found.setdefault(patient_id, service_name)
    return found


def _payment_facts(ids) -> tuple[dict, dict, dict]:
    """
    Distinct methods, distinct categories, and the latest method per patient.

    Void payments are excluded throughout — a voided transaction never
    happened, so it should not put a payment method or a service category
    against a patient's name.
    """
    payments = Payment.objects.filter(patient_id__in=ids).exclude(
        status=PaymentStatus.VOID
    )

    methods = defaultdict(set)
    for patient_id, method in payments.values_list("patient_id", "method").distinct():
        methods[patient_id].add(method)

    categories = defaultdict(set)
    category_rows = (
        payments.exclude(category="")
        # Materials are goods, not therapy. Including them would put "Material
        # sale" in the Service Type column for anyone who ever bought a kit.
        .exclude(category=PaymentCategory.MATERIAL_SALE)
        .values_list("patient_id", "category")
        .distinct()
    )
    for patient_id, category in category_rows:
        categories[patient_id].add(category)

    # Latest method: find each patient's most recent payment timestamp, then
    # match rows back to it. Two queries rather than one per patient.
    latest_times = dict(
        payments.values_list("patient_id").annotate(latest=Max("created_at")).values_list(
            "patient_id", "latest"
        )
    )
    latest_method = {}
    if latest_times:
        pairs = Q()
        for patient_id, when in latest_times.items():
            pairs |= Q(patient_id=patient_id, created_at=when)
        for patient_id, method in payments.filter(pairs).values_list(
            "patient_id", "method"
        ):
            latest_method.setdefault(patient_id, method)

    return methods, categories, latest_method


def filter_queryset(queryset, params):
    """
    Apply the directory's own filters.

    `status`, `serviceCategory` and `paymentType` are all derived rather than
    stored, so each translates into an existence check against the related
    tables rather than a column comparison.
    """
    status = params.get("status")
    if status == Status.ACTIVE_CARE:
        queryset = queryset.filter(monthly_enrollments__status=EnrollmentStatus.ACTIVE)
    elif status == Status.IN_PROGRESS:
        queryset = queryset.filter(
            installment_plans__status=EnrollmentStatus.ACTIVE
        ).exclude(monthly_enrollments__status=EnrollmentStatus.ACTIVE)
    elif status == Status.ACTION_NEEDED:
        queryset = queryset.exclude(
            monthly_enrollments__status=EnrollmentStatus.ACTIVE
        ).exclude(installment_plans__status=EnrollmentStatus.ACTIVE)

    gender = params.get("gender")
    if gender:
        queryset = queryset.filter(gender=gender)

    service_category = params.get("serviceCategory")
    if service_category:
        queryset = queryset.filter(
            payments__category=service_category
        ).exclude(payments__status=PaymentStatus.VOID)

    payment_type = params.get("paymentType")
    if payment_type:
        queryset = queryset.filter(payments__method=payment_type).exclude(
            payments__status=PaymentStatus.VOID
        )

    queryset = _apply_time_filter(queryset, params)

    # The derived filters join across to-many relations, so one patient can
    # match several times.
    return queryset.distinct()


def _apply_time_filter(queryset, params):
    """An exact date always beats a relative range — same rule as elsewhere."""
    from datetime import timedelta

    from django.utils import timezone

    exact = _parse_date(params.get("date"))
    if exact:
        return queryset.filter(created_at__date=exact)

    time_range = params.get("timeRange")
    today = timezone.localdate()
    if time_range == "today":
        return queryset.filter(created_at__date=today)
    if time_range == "week":
        return queryset.filter(created_at__date__gte=today - timedelta(days=6))
    if time_range == "month":
        return queryset.filter(created_at__year=today.year, created_at__month=today.month)
    return queryset


def _parse_date(value) -> date | None:
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
