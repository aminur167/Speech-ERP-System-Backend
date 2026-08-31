"""Shared Django admin building blocks."""

from django.contrib import admin

from apps.common.models import AuditLog, CodeSequence


class ReadOnlyAdminMixin:
    """
    Browsable in the Django admin, but never editable, addable, or deletable
    from it.

    Applied to every model whose only legitimate way to change is through the
    tested service-layer functions (payments, bills, closings, the audit log
    itself). Editing these rows directly here would bypass the Decimal
    precision rules, the audit trail, and the state machines — oldest-first
    billing, refund approval, append-only closings — those functions enforce.
    Staff can still open a row and read every field; they just can't save one.
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["created_at", "action", "target_type", "target_id", "actor_email", "branch"]
    list_filter = ["action", "target_type", "branch"]
    search_fields = ["actor_email", "target_type", "target_id", "reason"]
    date_hierarchy = "created_at"
    list_select_related = ["actor", "branch"]


@admin.register(CodeSequence)
class CodeSequenceAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """
    The counters backing every race-safe receipt/patient/expense code.

    Read-only for the same reason the codes themselves are never
    hand-assigned: a single wrong edit here can issue a duplicate code the
    next time this scope/year is used.
    """

    list_display = ["scope", "year", "last_value"]
    list_filter = ["year"]
    search_fields = ["scope"]
