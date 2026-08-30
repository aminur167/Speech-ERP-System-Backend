"""
Root URL configuration.

Everything is mounted under /api/ to match the frontend's configured base URL
(`NEXT_PUBLIC_API_BASE_URL`, defaulting to http://localhost:8000/api).
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("apps.accounts.urls")),
    path("api/branches/", include("apps.branches.urls")),
    path("api/patients/", include("apps.patients.urls")),
    path("api/services/", include("apps.services.urls")),
    path("api/", include("apps.payments.urls")),
    path("api/enrollments/", include("apps.enrollments.urls")),
    path("api/materials/", include("apps.materials.urls")),
]
