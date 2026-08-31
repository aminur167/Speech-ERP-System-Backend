"""
Health check.

Exists for the frontend's real connectivity detection (docs/00's
Full Offline-First section): `navigator.onLine` only reports whether a
network interface is up, not whether the server is actually reachable, so
the offline queue pings this endpoint to know the difference. Deliberately
does no database work -- it needs to answer fast and cheaply, and often,
without adding load.
"""

from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.models import AuditLog
from apps.common.permissions import IsAdmin
from apps.common.serializers import AuditLogSerializer


class HealthCheckSerializer(serializers.Serializer):
    status = serializers.CharField()


class HealthCheckView(APIView):
    permission_classes = [AllowAny]
    serializer_class = HealthCheckSerializer

    @extend_schema(tags=["common"], responses=HealthCheckSerializer)
    def get(self, request):
        return Response({"status": "ok"})


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Admin-only. The audit trail (docs/00's engineering checklist: "a real
    audit log table for who approved/rejected/terminated/edited what") is
    org-wide by nature -- a Manager's own actions are already visible to
    them through the records those actions produced (an expense they
    submitted, a payment they took); the point of this view is Admin
    oversight across every branch, so it is never branch-scoped to the
    requesting user the way other endpoints are.
    """

    queryset = AuditLog.objects.select_related("branch").all()
    serializer_class = AuditLogSerializer
    permission_classes = [IsAdmin]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["action", "target_type", "branch"]
