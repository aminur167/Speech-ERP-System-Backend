"""
Enquiry serializers.

camelCase out, snake_case in — the same split every other module here uses
(docs/00: the frontend reads DRF's conventions directly).
"""

from rest_framework import serializers

from apps.common.validators import normalize_phone, validate_bd_phone
from apps.enquiries.models import Enquiry


class EnquirySerializer(serializers.ModelSerializer):
    """Read shape. `followUpOverdue` is derived, never stored."""

    enquiryCode = serializers.CharField(source="enquiry_code", read_only=True)
    callerName = serializers.CharField(source="caller_name", read_only=True)
    callerPhone = serializers.CharField(source="caller_phone", read_only=True)
    callerRelation = serializers.CharField(source="caller_relation", read_only=True)
    patientName = serializers.CharField(source="patient_name", read_only=True)
    patientAge = serializers.IntegerField(source="patient_age", read_only=True)
    sourceDetail = serializers.CharField(source="source_detail", read_only=True)
    followUpOn = serializers.DateField(source="follow_up_on", read_only=True)
    followUpOverdue = serializers.SerializerMethodField()
    convertedPatientId = serializers.CharField(
        source="converted_patient_id", read_only=True
    )
    convertedPatientCode = serializers.CharField(
        source="converted_patient.patient_code", read_only=True, default=""
    )
    branchId = serializers.CharField(source="branch_id", read_only=True)
    branchName = serializers.CharField(source="branch.name", read_only=True)
    takenBy = serializers.CharField(source="taken_by.name", read_only=True, default="")
    calledAt = serializers.DateTimeField(source="called_at", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Enquiry
        fields = [
            "id", "enquiryCode", "callerName", "callerPhone", "callerRelation",
            "patientName", "patientAge", "concern", "source", "sourceDetail",
            "status", "followUpOn", "followUpOverdue", "notes",
            "convertedPatientId", "convertedPatientCode",
            "branchId", "branchName", "takenBy", "calledAt", "createdAt",
        ]
        read_only_fields = fields

    def get_followUpOverdue(self, obj) -> bool:
        return obj.is_follow_up_overdue()


class EnquiryWriteSerializer(serializers.ModelSerializer):
    """
    Create/update payload.

    Only the two things a call back needs are required — a name and a number
    — plus where they heard about the clinic, which is the reason this
    record exists at all. Everything else is whatever the caller happened to
    say, and demanding more would just mean the desk inventing it.
    """

    class Meta:
        model = Enquiry
        fields = [
            "caller_name", "caller_phone", "caller_relation",
            "patient_name", "patient_age", "concern",
            "source", "source_detail", "status", "follow_up_on", "notes",
            "called_at",
        ]
        extra_kwargs = {
            field: {"required": False, "allow_blank": True}
            for field in fields
            if field not in {"patient_age", "follow_up_on", "called_at"}
        }

    caller_name = serializers.CharField(max_length=150)
    caller_phone = serializers.CharField(max_length=20, validators=[validate_bd_phone])
    source = serializers.ChoiceField(choices=Enquiry.Source.choices)
    patient_age = serializers.IntegerField(
        required=False, allow_null=True, min_value=0, max_value=120
    )
    follow_up_on = serializers.DateField(required=False, allow_null=True)
    called_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate_caller_phone(self, value):
        return normalize_phone(value)

    def validate_called_at(self, value):
        from django.utils import timezone

        if value and value > timezone.now():
            raise serializers.ValidationError("The call cannot be in the future.")
        return value


class ConvertEnquirySerializer(serializers.Serializer):
    """
    The registration the conversion performs.

    Mirrors the patient create payload rather than inventing a second one:
    the caller gave a name, a number and maybe an age on the phone, and the
    rest of a real registration still has to be collected at the desk.
    `PatientWriteSerializer` is what actually validates it.
    """

    name = serializers.CharField(max_length=150)
    phone = serializers.CharField(max_length=20)
    date_of_birth = serializers.DateField()
    gender = serializers.CharField(max_length=10)
    address = serializers.CharField()
    guardian_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    guardian_relation = serializers.CharField(required=False, allow_blank=True, max_length=16)
    guardian_phone = serializers.CharField(required=False, allow_blank=True, max_length=20)
    chief_complaint = serializers.CharField(required=False, allow_blank=True)


class EnquirySummarySerializer(serializers.Serializer):
    total = serializers.IntegerField()
    converted = serializers.IntegerField()
    dueToday = serializers.IntegerField()
    overdue = serializers.IntegerField()
    topSource = serializers.CharField()
    topSourceCount = serializers.IntegerField()
