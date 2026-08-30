"""
Patient serializers.

The interesting logic here is the age-conditional guardian requirement — see
docs/02-patients.md. Everything else is a straight mapping, with camelCase
output to match what the frontend already reads.
"""

from datetime import date

from django.conf import settings
from rest_framework import serializers

from apps.common.validators import normalize_phone, validate_bd_phone
from apps.patients.models import Patient, calculate_age

GUARDIAN_FIELDS = ("guardian_name", "guardian_relation", "guardian_phone")

# Fields a manager must supply when registering someone new. Enforced here
# rather than in the schema so existing incomplete records stay editable.
REQUIRED_ON_CREATE = ("name", "date_of_birth", "gender", "phone", "address")


class PatientSerializer(serializers.ModelSerializer):
    """Read shape. `age` is computed, never stored."""

    patientCode = serializers.CharField(source="patient_code", read_only=True)
    age = serializers.SerializerMethodField()
    dateOfBirth = serializers.DateField(source="date_of_birth", read_only=True)
    bloodGroup = serializers.CharField(source="blood_group", read_only=True)
    guardianName = serializers.CharField(source="guardian_name", read_only=True)
    guardianRelation = serializers.CharField(source="guardian_relation", read_only=True)
    guardianPhone = serializers.CharField(source="guardian_phone", read_only=True)
    emergencyContact = serializers.CharField(source="emergency_contact", read_only=True)
    referredBy = serializers.CharField(source="referred_by", read_only=True)
    chiefComplaint = serializers.CharField(source="chief_complaint", read_only=True)
    nationalId = serializers.CharField(source="national_id", read_only=True)
    branchId = serializers.CharField(source="branch_id", read_only=True)
    branchName = serializers.CharField(source="branch.name", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Patient
        fields = [
            "id", "patientCode", "name", "age", "dateOfBirth", "gender", "bloodGroup",
            "phone", "email", "address",
            "guardianName", "guardianRelation", "guardianPhone", "emergencyContact",
            "referredBy", "chiefComplaint", "nationalId", "notes",
            "status", "branchId", "branchName", "createdAt",
        ]
        read_only_fields = fields

    def get_age(self, obj) -> int | None:
        return obj.age


class PatientListSerializer(serializers.ModelSerializer):
    """
    Lean shape for the enrollment wizards' patient search.

    `PatientSearchResultList` shows name, code, phone and gender — all four
    are here so that component has everything it needs from one call.
    """

    patientCode = serializers.CharField(source="patient_code", read_only=True)
    age = serializers.SerializerMethodField()

    class Meta:
        model = Patient
        fields = ["id", "patientCode", "name", "phone", "gender", "age"]
        read_only_fields = fields

    def get_age(self, obj) -> int | None:
        return obj.age


class PatientWriteSerializer(serializers.ModelSerializer):
    """
    Create/update payload.

    Requiredness differs by operation: a create must be complete, an update
    only validates what it touches. That asymmetry is deliberate — see the
    "required applies to new registrations only" decision in docs/02.
    """

    class Meta:
        model = Patient
        fields = [
            "name", "date_of_birth", "gender", "blood_group",
            "phone", "email", "address",
            "guardian_name", "guardian_relation", "guardian_phone", "emergency_contact",
            "referred_by", "chief_complaint", "national_id", "notes", "status",
        ]
        extra_kwargs = {
            # All optional at field level; create-time requiredness is applied
            # in validate() so the error names every missing field at once
            # rather than one per round-trip.
            field: {"required": False, "allow_blank": True}
            for field in fields
            if field not in {"date_of_birth"}
        }

    date_of_birth = serializers.DateField(required=False, allow_null=True)
    phone = serializers.CharField(required=False, allow_blank=True, validators=[validate_bd_phone])
    guardian_phone = serializers.CharField(
        required=False, allow_blank=True, validators=[validate_bd_phone]
    )
    emergency_contact = serializers.CharField(
        required=False, allow_blank=True, validators=[validate_bd_phone]
    )

    def validate_date_of_birth(self, value):
        if value and value > date.today():
            raise serializers.ValidationError("Date of birth cannot be in the future.")
        return value

    def validate(self, attrs):
        creating = self.instance is None
        errors: dict[str, list[str]] = {}

        if creating:
            for field in REQUIRED_ON_CREATE:
                if not attrs.get(field):
                    errors[field] = ["This field is required."]

        self._validate_guardian_requirement(attrs, errors, creating=creating)

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def _validate_guardian_requirement(self, attrs, errors, *, creating):
        """
        Guardian details are required for minors, optional for adults.

        Blanket-optional would let a child be registered with nobody to call,
        which is a real safety problem in a clinic treating mostly children.
        Blanket-required would block adult patients.
        """
        dob = attrs.get("date_of_birth") or (
            self.instance.date_of_birth if self.instance else None
        )
        if dob is None:
            # No birth date means age is unknown; don't demand guardian details
            # for a record that can't be assessed.
            return

        age = calculate_age(dob)
        if age is None or age >= settings.PATIENT_MINOR_AGE:
            return

        for field in GUARDIAN_FIELDS:
            supplied = attrs.get(field)
            existing = getattr(self.instance, field, "") if self.instance else ""
            # On update, an unchanged field that's already populated is fine —
            # only complain about values that would end up empty.
            if not supplied and not (not creating and existing):
                errors[field] = [
                    f"Required for patients under {settings.PATIENT_MINOR_AGE}."
                ]

    def validate_phone(self, value):
        return normalize_phone(value)

    def validate_guardian_phone(self, value):
        return normalize_phone(value)

    def validate_emergency_contact(self, value):
        return normalize_phone(value)
