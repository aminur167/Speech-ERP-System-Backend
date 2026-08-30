from rest_framework.routers import DefaultRouter

from apps.dailyclosing.views import DailyClosingViewSet

app_name = "dailyclosing"

router = DefaultRouter()
router.register("", DailyClosingViewSet, basename="daily-closing")

urlpatterns = router.urls
