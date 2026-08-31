from django.contrib import admin

from apps.common.admin import ReadOnlyAdminMixin
from apps.payments.models import Payment, RefundRequest, RefundRequestItem


@admin.register(Payment)
class PaymentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = [
        "receipt_number", "transaction_id", "patient", "amount", "method",
        "status", "category", "branch", "created_at",
    ]
    list_filter = ["status", "method", "category", "branch"]
    search_fields = ["receipt_number", "transaction_id", "patient__name", "patient__patient_code"]
    autocomplete_fields = ["patient", "branch", "collected_by"]
    date_hierarchy = "created_at"
    list_select_related = ["patient", "branch"]


class RefundRequestItemInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = RefundRequestItem
    extra = 0
    can_delete = False
    fields = ["material", "quantity", "unit_price", "line_total"]
    readonly_fields = fields

    @admin.display()
    def line_total(self, obj):
        return obj.line_total


@admin.register(RefundRequest)
class RefundRequestAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = [
        "payment", "amount", "status", "bill_action", "requested_by",
        "requested_at", "reviewed_by", "reviewed_at",
    ]
    list_filter = ["status", "bill_action", "branch"]
    search_fields = ["payment__receipt_number", "reason", "review_note"]
    autocomplete_fields = ["payment", "requested_by", "reviewed_by", "branch"]
    date_hierarchy = "requested_at"
    inlines = [RefundRequestItemInline]
