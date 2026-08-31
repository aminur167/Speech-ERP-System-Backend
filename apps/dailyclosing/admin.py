from django.contrib import admin

from apps.common.admin import ReadOnlyAdminMixin
from apps.dailyclosing.models import DailyClosing, DailyClosingAmendment


class DailyClosingAmendmentInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = DailyClosingAmendment
    extra = 0
    can_delete = False
    fields = ["previous_actual_total", "corrected_actual_total", "reason", "amended_by", "amended_at"]
    readonly_fields = fields
    ordering = ["amended_at"]


@admin.register(DailyClosing)
class DailyClosingAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["branch", "date", "system_total", "actual_total", "difference", "status", "submitted_by"]
    list_filter = ["status", "branch"]
    search_fields = ["branch__name", "branch__code"]
    autocomplete_fields = ["branch", "submitted_by"]
    date_hierarchy = "date"
    inlines = [DailyClosingAmendmentInline]


@admin.register(DailyClosingAmendment)
class DailyClosingAmendmentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["closing", "previous_actual_total", "corrected_actual_total", "amended_by", "amended_at"]
    search_fields = ["closing__branch__name", "reason"]
    autocomplete_fields = ["closing", "amended_by"]
    date_hierarchy = "amended_at"
