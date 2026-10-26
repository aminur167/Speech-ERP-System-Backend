from rest_framework.routers import DefaultRouter

from apps.enquiries.views import EnquiryViewSet

app_name = "enquiries"

router = DefaultRouter()
router.register("", EnquiryViewSet, basename="enquiry")

urlpatterns = router.urls
