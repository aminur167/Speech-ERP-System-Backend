"""
User admin.

A thin subclass of Django's own `UserAdmin`, adapted for an email-based user
with no username field. Passwords always go through Django's hashed-password
widget (`SetPasswordForm`/`AdminPasswordChangeForm`) — never a raw text
field — so staff can create accounts and reset passwords here without a
plaintext password ever touching the database or this screen.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.accounts.models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ["name"]
    list_display = ["email", "name", "role", "branch", "is_active", "is_staff", "date_joined"]
    list_filter = ["role", "is_active", "is_staff", "branch"]
    search_fields = ["email", "name", "staff_code"]
    autocomplete_fields = ["branch"]

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("name", "role", "branch", "staff_code")}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "name", "role", "branch", "password1", "password2"),
            },
        ),
    )
