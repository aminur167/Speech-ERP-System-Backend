"""
Health check.

Exists for the frontend's real connectivity detection (docs/00's
Full Offline-First section): `navigator.onLine` only reports whether a
network interface is up, not whether the server is actually reachable, so
the offline queue pings this endpoint to know the difference. Deliberately
does no database work -- it needs to answer fast and cheaply, and often,
without adding load.
"""

from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthCheckSerializer(serializers.Serializer):
    status = serializers.CharField()


class HealthCheckView(APIView):
    permission_classes = [AllowAny]
    serializer_class = HealthCheckSerializer

    @extend_schema(tags=["common"], responses=HealthCheckSerializer)
    def get(self, request):
        return Response({"status": "ok"})
