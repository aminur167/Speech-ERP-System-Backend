"""
Patient model.

Two rules from docs/02-patients.md shape this file:

1. **Every column stays nullable**, including the "required" ones. Requiredness
   is enforced in the create serializer, not the schema, so tightening
   validation doesn't break existing incomplete records or force a manager
   editing an old patient to invent missing history.

2. **Age is never stored.** It is derived from `date_of_birth` on read. A
   stored age is wrong within a year and nobody notices.
"""

from datetime import date

from django.db import models

from apps.common.models import SoftDeleteModel, TimeStampedModel
from apps.common.validators import normalize_phone, validate_bd_phone


def calculate_age(date_of_birth: date | None, *, on: date | None = None) -> int | None:
    """
    Whole years, counting the birthday correctly.

    Naive year subtraction is off by one for anyone whose birthday hasn't
    happened yet this year — which is roughly half the patient list on any
    given day.
    """
    if date_of_birth is None:
        return None

    reference = on or date.today()
    age = reference.year - date_of_birth.year
    had_birthday = (reference.month, reference.day) >= (date_of_birth.month, date_of_birth.day)
    return age if had_birthday else age - 1


class Patient(TimeStampedModel, SoftDeleteModel):
    class Gender(models.TextChoices):
        MALE = "male", "Male"
        FEMALE = "female", "Female"
        OTHER = "other", "Other"

    class GuardianRelation(models.TextChoices):
        FATHER = "father", "Father"
        MOTHER = "mother", "Mother"
        GUARDIAN = "guardian", "Guardian"
        OTHER = "other", "Other"

    class BloodGroup(models.TextChoices):
        A_POS = "A+", "A+"
        A_NEG = "A-", "A-"
        B_POS = "B+", "B+"
        B_NEG = "B-", "B-"
        AB_POS = "AB+", "AB+"
        AB_NEG = "AB-", "AB-"
        O_POS = "O+", "O+"
        O_NEG = "O-", "O-"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    # Per-branch series (PT-DHK-2026-00001) so registration works offline
    # without branches coordinating — see docs/02 and docs/04.
    patient_code = models.CharField(max_length=32, unique=True, db_index=True)

    name = models.CharField(max_length=150, db_index=True)
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=10, choices=Gender.choices, blank=True, db_index=True)
    blood_group = models.CharField(max_length=3, choices=BloodGroup.choices, blank=True)

    phone = models.CharField(max_length=20, db_index=True, validators=[validate_bd_phone])
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)

    # Nullable in the schema; required by the serializer only for minors.
    guardian_name = models.CharField(max_length=150, blank=True, db_index=True)
    guardian_relation = models.CharField(
        max_length=16, choices=GuardianRelation.choices, blank=True
    )
    guardian_phone = models.CharField(max_length=20, blank=True, validators=[validate_bd_phone])
    emergency_contact = models.CharField(max_length=20, blank=True, validators=[validate_bd_phone])

    referred_by = models.CharField(max_length=150, blank=True)
    chief_complaint = models.TextField(blank=True)
    national_id = models.CharField(max_length=32, blank=True)
    notes = models.TextField(blank=True)

    # Lifecycle, distinct from is_deleted: `inactive` archives a patient who
    # stopped coming, `is_deleted` is for records created in error.
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )

    branch = models.ForeignKey(
        "branches.Branch",
        on_delete=models.PROTECT,  # never cascade patient records away
        related_name="patients",
    )
    created_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="registered_patients",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # The directory filters and sorts on these constantly; without
            # them the endpoint degrades badly as data accumulates.
            models.Index(fields=["branch", "-created_at"]),
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["name"]),
            models.Index(fields=["phone"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.patient_code})"

    def save(self, *args, **kwargs):
        # Normalise on the way in so search matches regardless of how the
        # number was typed.
        for field in ("phone", "guardian_phone", "emergency_contact"):
            value = getattr(self, field, "")
            if value:
                setattr(self, field, normalize_phone(value))
        super().save(*args, **kwargs)

    @property
    def age(self) -> int | None:
        return calculate_age(self.date_of_birth)

    @property
    def is_minor(self) -> bool:
        """
        Unknown date of birth is treated as adult.

        The alternative — demanding guardian details for a record that simply
        lacks a birth date — would block editing exactly the incomplete legacy
        records this system has to tolerate.
        """
        from django.conf import settings

        age = self.age
        if age is None:
            return False
        return age < settings.PATIENT_MINOR_AGE
