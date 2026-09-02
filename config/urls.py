"""
Root URL configuration.

Everything is mounted under /api/ to match the frontend's configured base URL
(`NEXT_PUBLIC_API_BASE_URL`, defaulting to http://localhost:8000/api).
"""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.permissions import AllowAny

from apps.common.views import HealthCheckView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthCheckView.as_view(), name="health"),
    # Raw OpenAPI schema, and the interactive Swagger UI built from it.
    # Publicly viewable, like most APIs' docs (Stripe, GitHub, ...) — it only
    # describes request/response shapes, not data. Every actual endpoint
    # reached through Swagger's "Try it out" still needs a real token; viewing
    # the docs is not the same as calling the API.
    path(
        "api/schema/",
        SpectacularAPIView.as_view(permission_classes=[AllowAny]),
        name="schema",
    ),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema", permission_classes=[AllowAny]),
        name="swagger-ui",
    ),
    path("api/auth/", include("apps.accounts.urls")),
    path("api/branches/", include("apps.branches.urls")),
    path("api/patients/", include("apps.patients.urls")),
    path("api/services/", include("apps.services.urls")),
    path("api/", include("apps.payments.urls")),
    path("api/enrollments/", include("apps.enrollments.urls")),
    path("api/materials/", include("apps.materials.urls")),
    path("api/due-payments/", include("apps.duepayments.urls")),
    path("api/expenses/", include("apps.expenses.urls")),
    path("api/daily-closing/", include("apps.dailyclosing.urls")),
    path("api/transactions/", include("apps.reporting.urls")),
    path("api/", include("apps.common.urls")),
    path("api/", include("apps.notifications.urls")),
]
