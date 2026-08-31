from django.contrib import admin

from apps.services.models import Service


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "category", "fee", "is_active", "is_online"]
    list_filter = ["category", "is_active", "is_online"]
    search_fields = ["name", "code"]
