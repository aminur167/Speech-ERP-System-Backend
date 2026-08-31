from django.contrib import admin

from apps.common.admin import ReadOnlyAdminMixin
from apps.materials.models import Material, MaterialMovement, MaterialSaleItem


class MaterialMovementInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = MaterialMovement
    extra = 0
    can_delete = False
    fields = ["type", "quantity", "note", "created_by", "created_at"]
    readonly_fields = fields
    ordering = ["-created_at"]


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    """
    Editable — this is catalog/inventory data (name, price, reorder level),
    not a transaction record. Actual quantity changes still belong in the
    movements ledger below; editing `quantity` here directly skips that trail,
    so treat it as a last-resort correction, not the normal way to receive or
    adjust stock.
    """

    list_display = ["name", "code", "branch", "quantity", "unit_cost", "selling_price", "is_low_stock"]
    list_filter = ["branch", "unit"]
    search_fields = ["name", "code"]
    autocomplete_fields = ["branch"]
    inlines = [MaterialMovementInline]

    @admin.display(boolean=True)
    def is_low_stock(self, obj):
        return obj.is_low_stock


@admin.register(MaterialMovement)
class MaterialMovementAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ["material", "type", "quantity", "branch", "created_by", "created_at"]
    list_filter = ["type", "branch"]
    search_fields = ["material__name", "material__code", "note"]
    autocomplete_fields = ["material", "branch", "created_by"]
    date_hierarchy = "created_at"


@admin.register(MaterialSaleItem)
class MaterialSaleItemAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """One line of a POS sale — priced at what was actually charged, so it must never be edited after the fact."""

    list_display = ["material", "payment", "quantity", "unit_price", "line_total"]
    search_fields = ["material__name", "material__code", "payment__receipt_number"]
    autocomplete_fields = ["material", "payment"]
