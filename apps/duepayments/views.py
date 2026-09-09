"""
Due payment endpoints.

Read-only: collection itself happens through the enrollment endpoints, where
it's atomic with settling the bill. A second collection path here would be a
second chance to get that wrong.
"""

from datetime import datetime
from decimal import Decimal

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.duepayments import services
from apps.duepayments.serializers import DuePaymentListSerializer, DueSummarySerializer

_BRANCH_PARAM = OpenApiParameter(
    "branch", str, description="Admin only — narrow to one branch's data."
)


def _branch_id_for(request):
    """Manager: their own branch. Admin: optionally narrowed, else all."""
    if request.user.is_manager:
        return request.user.branch_id
    return request.query_params.get("branch") or None


def _parse_month(value):
    """
    A "YYYY-MM" cycle, or None. A malformed one is ignored rather than 400ing:
    the filter narrows a list, so the safe failure is showing everything.
    """
    if not value:
        return None
    try:
        datetime.strptime(value, "%Y-%m")
    except (ValueError, TypeError):
        return None
    return value


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


class DuePaymentListView(APIView):
    """GET /api/due-payments/"""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["due-payments"],
        parameters=[
            _BRANCH_PARAM,
            OpenApiParameter("type", str, description='"monthly" or "installment".'),
            OpenApiParameter(
                "month",
                str,
                description=(
                    'Cycle month as "YYYY-MM". Keeps monthly rows whose payable '
                    "bill is that month or earlier — i.e. who owes as of the end "
                    "of it. Installments ignore it."
                ),
            ),
            OpenApiParameter("search", str),
            OpenApiParameter("page", int),
            OpenApiParameter("pageSize", int),
        ],
        responses=DuePaymentListSerializer,
    )
    def get(self, request):
        items = services.collect_due_items(
            branch_id=_branch_id_for(request),
            month=_parse_month(request.query_params.get("month")),
        )

        type_filter = request.query_params.get("type")
        if type_filter:
            items = [i for i in items if i["type"] == type_filter]

        search = request.query_params.get("search", "").strip().lower()
        if search:
            # One box, four things a manager might have in front of them: the
            # patient's name, the code on their card, the service, or a
            # service id copied from another screen. Ids match exactly —
            # a substring match on a numeric id turns "1" into a wildcard.
            items = [
                i
                for i in items
                if search in i["patientName"].lower()
                or search in i["patientCode"].lower()
                or search in i["serviceName"].lower()
                or search == i["serviceId"].lower()
                or search == i["patientId"].lower()
            ]

        # Paginated in Python: these rows are assembled across two tables and
        # already bounded by one item per active enrollment.
        try:
            page = max(1, int(request.query_params.get("page", 1)))
            page_size = min(100, max(1, int(request.query_params.get("pageSize", 10))))
        except ValueError:
            page, page_size = 1, 10

        start = (page - 1) * page_size
        window = items[start : start + page_size]

        return Response(
            {
                "count": len(items),
                # Across everything the filters matched, not just this page:
                # the screen puts it next to the table's own heading, and a
                # per-page subtotal labelled as the total is worse than none.
                "totalAmount": sum(
                    (i["amount"] for i in items), Decimal("0.00")
                ),
                "next": str(page + 1) if start + page_size < len(items) else None,
                "previous": str(page - 1) if page > 1 else None,
                "results": window,
            }
        )


class DuePaymentSummaryView(APIView):
    """
    GET /api/due-payments/summary/

    `?date=` reconstructs what was outstanding at the end of that day rather
    than returning the current snapshot — the dashboard date picker depends on
    this.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["due-payments"],
        parameters=[
            _BRANCH_PARAM,
            OpenApiParameter("date", str, description="ISO date; reconstructs that day's total."),
        ],
        responses=DueSummarySerializer,
    )
    def get(self, request):
        return Response(
            services.due_summary(
                branch_id=_branch_id_for(request),
                as_of=_parse_date(request.query_params.get("date")),
            )
        )
