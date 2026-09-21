"""
Several read requests in one round trip.

The dashboard needs ten small reads at once. Sent separately, each one pays
for its own trip, its own token check and its own turn in the server's queue —
on a small server that queue, not the queries, is most of the wait.

``POST /api/batch/`` takes a list of GET requests and answers them all at
once. Each is handed to **the same view that serves it on its own**, as the
same user, so permissions, branch scoping and the response shape cannot
differ from calling it directly. Only the round trips go away.

Deliberately narrow:

* only paths on ``ALLOWED_PATHS`` — small, read-only summary endpoints; a
  batch can never write anything, and new paths are added on purpose;
* GET only, a handful per batch;
* each part gets its own status, so one failing does not fail the others.
"""

import copy

from django.http import QueryDict
from django.urls import Resolver404, resolve
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

API_PREFIX = "/api"
MAX_REQUESTS = 12

#: Relative to /api, exactly as the frontend's API client writes them.
ALLOWED_PATHS = frozenset(
    {
        "/branches/overview/",
        "/daily-closing/today-summary/",
        "/daily-closing/history/",
        "/due-payments/summary/",
        "/expenses/summary/",
        "/patients/directory/summary/",
        "/transactions/by-category/",
        "/transactions/by-method/",
        "/transactions/dashboard-metrics/",
        "/transactions/summary/",
        "/transactions/trend/",
    }
)


class _PartSerializer(serializers.Serializer):
    path = serializers.ChoiceField(choices=sorted(ALLOWED_PATHS))
    params = serializers.DictField(child=serializers.CharField(allow_blank=True), required=False)


class _BatchSerializer(serializers.Serializer):
    requests = serializers.ListField(
        child=_PartSerializer(), min_length=1, max_length=MAX_REQUESTS
    )


class _PartResponseSerializer(serializers.Serializer):
    status = serializers.IntegerField()
    data = serializers.JSONField(allow_null=True)


class _BatchResponseSerializer(serializers.Serializer):
    responses = _PartResponseSerializer(many=True)


class BatchView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = _BatchSerializer

    @extend_schema(tags=["common"], request=_BatchSerializer, responses=_BatchResponseSerializer)
    def post(self, request):
        payload = _BatchSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        responses = [
            self._answer(request, part["path"], part.get("params") or {})
            for part in payload.validated_data["requests"]
        ]
        return Response({"responses": responses}, status=status.HTTP_200_OK)

    @staticmethod
    def _answer(request, path: str, params: dict) -> dict:
        full_path = API_PREFIX + path
        try:
            match = resolve(full_path)
        except Resolver404:
            return {"status": status.HTTP_404_NOT_FOUND, "data": {"detail": "Not found."}}

        sub = copy.copy(request._request)
        sub.method = "GET"
        sub.path = sub.path_info = full_path
        query = QueryDict(mutable=True)
        for key, value in params.items():
            query[key] = value
        sub.GET = query
        # Already authenticated for this batch: hand the view the same user and
        # token instead of verifying the token again for every part.
        sub._force_auth_user = request.user
        sub._force_auth_token = request.auth

        response = match.func(sub, *match.args, **match.kwargs)
        return {"status": response.status_code, "data": getattr(response, "data", None)}
