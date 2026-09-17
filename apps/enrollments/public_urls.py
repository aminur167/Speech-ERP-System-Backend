"""
Public (unauthenticated) booking endpoints for the clinic's own website.
Mounted at /api/public/ — see config/urls.py.
"""

from django.urls import path

from apps.enrollments.public_views import (
    PublicBookingAvailabilityView,
    PublicBookingCreateView,
    PublicBranchListView,
    PublicServiceListView,
)

app_name = "public"

urlpatterns = [
    path("branches/", PublicBranchListView.as_view(), name="branches"),
    path("services/", PublicServiceListView.as_view(), name="services"),
    path(
        "bookings/availability/",
        PublicBookingAvailabilityView.as_view(),
        name="booking-availability",
    ),
    path("bookings/", PublicBookingCreateView.as_view(), name="booking-create"),
]
