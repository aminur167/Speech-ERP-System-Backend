from rest_framework.routers import DefaultRouter

from apps.staff.views import SalaryPaymentViewSet, StaffMemberViewSet

app_name = "staff"

router = DefaultRouter()
router.register("salary-payments", SalaryPaymentViewSet, basename="salarypayment")
router.register("", StaffMemberViewSet, basename="staffmember")

urlpatterns = router.urls
