"""
Root URL configuration.

Everything is mounted under /api/ to match the frontend's configured base URL
(`NEXT_PUBLIC_API_BASE_URL`, defaulting to http://localhost:8000/api).
"""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("admin/", admin.site.urls),
    # Raw OpenAPI schema, and the interactive Swagger UI built from it. Both
    # require an authenticated request like any other endpoint here — the API
    # has no public signup path, so there's no reason its shape should be
    # world-readable.
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
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
]
