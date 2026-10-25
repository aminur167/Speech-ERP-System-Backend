from rest_framework.routers import DefaultRouter

from apps.materials.views import MaterialViewSet, SaleHistoryViewSet

app_name = "materials"

router = DefaultRouter()
# Registered before the catch-all "" route, or `sales` is read as a material id.
router.register("sales", SaleHistoryViewSet, basename="sale")
router.register("", MaterialViewSet, basename="material")

urlpatterns = router.urls
