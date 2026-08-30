from rest_framework.routers import DefaultRouter

from apps.materials.views import MaterialViewSet

app_name = "materials"

router = DefaultRouter()
router.register("", MaterialViewSet, basename="material")

urlpatterns = router.urls
