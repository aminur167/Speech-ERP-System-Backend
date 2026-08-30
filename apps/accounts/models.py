"""
User model.

Email-based login with a role and an owning branch. Replaces Django's default
username-based User because the app authenticates by email and every
permission decision keys off `role` + `branch`.

Note on the frontend mock: it stored `managerPassword` in plaintext on the
Branch record and displayed it in the Admin UI. That is not reproduced here —
credentials live on the User, hashed, and are never readable back
(docs/01-auth-and-branches.md).
"""

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from apps.common.models import SoftDeleteModel


class UserManager(BaseUserManager):
    """Manager keyed on email rather than username."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address.")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra_fields)
        user.set_password(password)  # hashed, never stored raw
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", User.Role.ADMIN)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin, SoftDeleteModel):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        MANAGER = "manager", "Branch Manager"

    email = models.EmailField(unique=True, db_index=True)
    name = models.CharField(max_length=150)
    role = models.CharField(max_length=16, choices=Role.choices, db_index=True)

    # Null for Admin (org-wide), set for Manager. Every branch-scoped query
    # derives from this field and never from the request (apps/common/mixins.py).
    branch = models.ForeignKey(
        "branches.Branch",
        null=True,
        blank=True,
        on_delete=models.PROTECT,  # never orphan a manager by deleting a branch
        related_name="managers",
    )

    # Staff/manager identifier shown in the UI, e.g. MGR-DHK-001.
    staff_code = models.CharField(max_length=32, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)  # Django admin access
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()
    all_objects = models.Manager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["role", "branch"]),
        ]

    def __str__(self):
        return f"{self.name} <{self.email}>"

    def save(self, *args, **kwargs):
        self.email = self.email.lower().strip()
        super().save(*args, **kwargs)

    @property
    def is_admin(self) -> bool:
        return self.role == self.Role.ADMIN

    @property
    def is_manager(self) -> bool:
        return self.role == self.Role.MANAGER

    def clean(self):
        """
        A manager without a branch can see nothing and act nowhere; an admin
        pinned to one branch contradicts the role. Catch both before they
        become confusing permission failures at runtime.
        """
        from django.core.exceptions import ValidationError

        if self.role == self.Role.MANAGER and self.branch_id is None:
            raise ValidationError({"branch": "A branch manager must belong to a branch."})
        if self.role == self.Role.ADMIN and self.branch_id is not None:
            raise ValidationError({"branch": "An admin is organisation-wide and has no branch."})
