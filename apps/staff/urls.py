from rest_framework.routers import DefaultRouter

from apps.staff.views import StaffMemberViewSet

app_name = "staff"

router = DefaultRouter()
router.register("", StaffMemberViewSet, basename="staffmember")

urlpatterns = router.urls
