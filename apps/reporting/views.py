"""
Reporting endpoints.

All read-only aggregates over the Payment table. Branch scoping follows the
usual rule — manager sees their own, admin sees all or narrows with ?branch=.
"""

from datetime import datetime

from django.db.models import Q
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.payments.models import Payment, PaymentStatus
from apps.payments.serializers import PaymentSerializer
from apps.reporting import services


def _branch_id_for(request):
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


class _ReportView(APIView):
    permission_classes = [IsAuthenticated]


class TransactionListView(_ReportView):
    """
    GET /api/transactions/

    The denormalized payment history the frontend reads. Refunded and void
    payments appear here deliberately — they're excluded from revenue totals,
    not from the record of what happened.
    """

    def get(self, request):
        queryset = Payment.objects.select_related("patient", "branch", "collected_by")

        branch_id = _branch_id_for(request)
        if branch_id:
            queryset = queryset.filter(branch_id=branch_id)

        patient_id = request.query_params.get("patientId") or request.query_params.get("patient")
        if patient_id:
            queryset = queryset.filter(patient_id=patient_id)

        for param, field in (("method", "method"), ("status", "status")):
            value = request.query_params.get(param)
            if value:
                queryset = queryset.filter(**{field: value})

        # Exact date wins over relative period, matching the dashboard picker.
        date_param = _parse_date(request.query_params.get("date"))
        period = request.query_params.get("period")
        if date_param:
            queryset = queryset.filter(created_at__date=date_param)
        elif period == "today":
            from django.utils import timezone

            queryset = queryset.filter(created_at__date=timezone.localdate())
        elif period == "month":
            from django.utils import timezone

            today = timezone.localdate()
            queryset = queryset.filter(
                created_at__year=today.year, created_at__month=today.month
            )

        search = request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(patient__name__icontains=search)
                | Q(patient__patient_code__icontains=search)
                | Q(receipt_number__icontains=search)
                | Q(transaction_id__icontains=search)
            )

        paginator = PageNumberPagination()
        paginator.page_size = int(request.query_params.get("pageSize", 10) or 10)
        page = paginator.paginate_queryset(queryset.order_by("-created_at"), request)

        return paginator.get_paginated_response(PaymentSerializer(page, many=True).data)


class TransactionsSummaryView(_ReportView):
    def get(self, request):
        return Response(
            services.transactions_summary(
                branch_id=_branch_id_for(request),
                as_of=_parse_date(request.query_params.get("date")),
            )
        )


class RevenueTrendView(_ReportView):
    def get(self, request):
        try:
            days = min(90, max(1, int(request.query_params.get("days", 7))))
        except ValueError:
            days = 7
        return Response(
            services.revenue_trend(branch_id=_branch_id_for(request), days=days)
        )


class RevenueByMethodView(_ReportView):
    def get(self, request):
        return Response(
            services.revenue_by_method(
                branch_id=_branch_id_for(request),
                as_of=_parse_date(request.query_params.get("date")),
            )
        )


class RevenueByCategoryView(_ReportView):
    def get(self, request):
        return Response(
            services.revenue_by_category(
                branch_id=_branch_id_for(request),
                as_of=_parse_date(request.query_params.get("date")),
            )
        )


class DashboardMetricsView(_ReportView):
    def get(self, request):
        return Response(
            services.dashboard_metrics(
                branch_id=_branch_id_for(request),
                as_of=_parse_date(request.query_params.get("date")),
            )
        )


class CollectionForDateView(_ReportView):
    def get(self, request):
        from django.utils import timezone

        target = _parse_date(request.query_params.get("date")) or timezone.localdate()
        return Response(
            {
                "date": target.isoformat(),
                "amount": services.collection_for_date(
                    branch_id=_branch_id_for(request), target=target
                ),
            }
        )


class RefundsAndVoidsView(_ReportView):
    def get(self, request):
        return Response(services.refunds_and_voids(branch_id=_branch_id_for(request)))


class NetRevenueView(_ReportView):
    def get(self, request):
        return Response(
            services.net_revenue(
                branch_id=_branch_id_for(request),
                as_of=_parse_date(request.query_params.get("date")),
            )
        )
