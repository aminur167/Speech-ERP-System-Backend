from django.contrib import admin

from apps.common.admin import ReadOnlyAdminMixin
from apps.enrollments.models import (
    Booking,
    Installment,
    InstallmentPlan,
    MonthlyBill,
    MonthlyEnrollment,
)


class MonthlyBillInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = MonthlyBill
    extra = 0
    can_delete = False
    fields = ["month", "label", "amount", "amount_paid", "status", "due_date", "paid_at"]
    readonly_fields = fields
    ordering = ["month"]


@admin.register(MonthlyEnrollment)
class MonthlyEnrollmentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["patient", "service", "branch", "status", "created_at", "terminated_at"]
    list_filter = ["status", "branch"]
    search_fields = ["patient__name", "patient__patient_code", "service__name"]
    autocomplete_fields = ["patient", "service", "branch"]
    date_hierarchy = "created_at"
    inlines = [MonthlyBillInline]


@admin.register(MonthlyBill)
class MonthlyBillAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Standalone view of every bill, for searching/filtering across enrollments directly."""

    list_display = ["enrollment", "month", "label", "amount", "amount_paid", "status", "due_date"]
    list_filter = ["status"]
    search_fields = ["enrollment__patient__name", "enrollment__patient__patient_code", "label"]
    autocomplete_fields = ["enrollment"]
    date_hierarchy = "due_date"


class InstallmentInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = Installment
    extra = 0
    can_delete = False
    fields = ["index", "label", "amount", "amount_paid", "status", "due_date", "paid_at"]
    readonly_fields = fields
    ordering = ["index"]


@admin.register(InstallmentPlan)
class InstallmentPlanAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["patient", "service", "branch", "total_amount", "status", "created_at"]
    list_filter = ["status", "branch"]
    search_fields = ["patient__name", "patient__patient_code", "service__name"]
    autocomplete_fields = ["patient", "service", "branch"]
    date_hierarchy = "created_at"
    inlines = [InstallmentInline]


@admin.register(Booking)
class BookingAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["booking_code", "patient", "service", "branch", "date", "time", "advance_amount", "status"]
    list_filter = ["status", "branch"]
    search_fields = ["booking_code", "patient__name", "patient__patient_code"]
    autocomplete_fields = ["patient", "service", "branch"]
    date_hierarchy = "date"
