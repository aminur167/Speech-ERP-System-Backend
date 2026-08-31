from django.contrib import admin

from apps.branches.models import Branch


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "status", "manager", "opened_at"]
    list_filter = ["status"]
    search_fields = ["name", "code", "address", "phone"]
    autocomplete_fields = ["manager"]
    date_hierarchy = "opened_at"
