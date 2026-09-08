from django.contrib import admin

from apps.common.admin import ReadOnlyAdminMixin
from apps.staff.models import SalaryPayment, StaffAttendance, StaffBonus, StaffMember


class StaffAttendanceInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = StaffAttendance
    extra = 0
    can_delete = False
    fields = ["date", "status", "check_in_at", "check_out_at"]
    readonly_fields = fields
    ordering = ["-date"]


class StaffBonusInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = StaffBonus
    extra = 0
    can_delete = False
    fields = ["amount", "reason", "awarded_by", "created_at"]
    readonly_fields = fields
    ordering = ["-created_at"]


@admin.register(StaffMember)
class StaffMemberAdmin(admin.ModelAdmin):
    list_display = ["name", "staff_code", "designation", "branch", "monthly_salary", "status"]
    list_filter = ["branch", "designation", "status"]
    search_fields = ["name", "staff_code", "phone", "email"]
    autocomplete_fields = ["branch"]
    inlines = [StaffAttendanceInline, StaffBonusInline]


@admin.register(StaffAttendance)
class StaffAttendanceAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["staff", "date", "status", "check_in_at", "check_out_at", "branch"]
    list_filter = ["status", "branch"]
    search_fields = ["staff__name", "staff__staff_code"]
    autocomplete_fields = ["staff", "branch"]
    date_hierarchy = "date"


@admin.register(StaffBonus)
class StaffBonusAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["staff", "amount", "reason", "awarded_by", "branch", "created_at"]
    list_filter = ["branch"]
    search_fields = ["staff__name", "staff__staff_code", "reason"]
    autocomplete_fields = ["staff", "branch", "awarded_by"]
    date_hierarchy = "created_at"


@admin.register(SalaryPayment)
class SalaryPaymentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Read-only — every state change goes through the service layer so the approval trail and the auto-created Expense stay consistent."""

    list_display = ["staff", "month", "amount", "status", "requested_by", "reviewed_by", "branch"]
    list_filter = ["status", "branch"]
    search_fields = ["staff__name", "staff__staff_code", "month"]
    autocomplete_fields = ["staff", "branch", "requested_by", "reviewed_by", "expense"]
    date_hierarchy = "created_at"
