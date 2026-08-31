from django.contrib import admin

from apps.common.admin import ReadOnlyAdminMixin
from apps.expenses.models import Expense


@admin.register(Expense)
class ExpenseAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """
    Read-only: status changes must go through the review workflow (which
    enforces the auto-approve threshold and requires a reason on rejection),
    never a direct edit here.
    """

    list_display = [
        "expense_code", "category", "amount", "status", "branch",
        "submitted_by", "created_at",
    ]
    list_filter = ["status", "category", "branch"]
    search_fields = ["expense_code", "description", "paid_to"]
    autocomplete_fields = ["branch", "submitted_by", "reviewed_by"]
    date_hierarchy = "created_at"
