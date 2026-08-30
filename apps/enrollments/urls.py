from rest_framework.routers import DefaultRouter

from apps.enrollments.views import (
    BookingViewSet,
    InstallmentPlanViewSet,
    MonthlyEnrollmentViewSet,
)

app_name = "enrollments"

router = DefaultRouter()
router.register("monthly", MonthlyEnrollmentViewSet, basename="monthly-enrollment")
router.register("installments", InstallmentPlanViewSet, basename="installment-plan")
router.register("bookings", BookingViewSet, basename="booking")

urlpatterns = router.urls
