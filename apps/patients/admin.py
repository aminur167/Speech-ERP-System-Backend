from django.contrib import admin

from apps.patients.models import Patient


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ["patient_code", "name", "phone", "gender", "status", "branch", "created_at"]
    list_filter = ["status", "gender", "branch"]
    search_fields = ["patient_code", "name", "phone", "guardian_name", "guardian_phone"]
    autocomplete_fields = ["branch", "created_by"]
    date_hierarchy = "created_at"
    list_select_related = ["branch"]
