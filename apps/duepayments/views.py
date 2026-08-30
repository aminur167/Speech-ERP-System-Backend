"""
Due payment endpoints.

Read-only: collection itself happens through the enrollment endpoints, where
it's atomic with settling the bill. A second collection path here would be a
second chance to get that wrong.
"""

from datetime import datetime

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
            OpenApiParameter("search", str),
            OpenApiParameter("page", int),
            OpenApiParameter("pageSize", int),
        ],
        responses=DuePaymentListSerializer,
    )
    def get(self, request):
        items = services.collect_due_items(branch_id=_branch_id_for(request))

        type_filter = request.query_params.get("type")
        if type_filter:
            items = [i for i in items if i["type"] == type_filter]

        search = request.query_params.get("search", "").strip().lower()
        if search:
            items = [
                i
                for i in items
                if search in i["patientName"].lower()
                or search in i["patientCode"].lower()
                or search in i["serviceName"].lower()
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
