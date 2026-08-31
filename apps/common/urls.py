from rest_framework.routers import DefaultRouter

from apps.common.views import AuditLogViewSet

app_name = "common"

router = DefaultRouter()
router.register("audit-logs", AuditLogViewSet, basename="audit-log")

urlpatterns = router.urls
