from rest_framework.routers import DefaultRouter

from apps.expenses.views import ExpenseViewSet

app_name = "expenses"

router = DefaultRouter()
router.register("", ExpenseViewSet, basename="expense")

urlpatterns = router.urls
