"""
Branch serializers.

Manager credentials are **write-only**. The mock returned `managerPassword` in
the branch payload and the Admin UI displayed it; that is not reproduced. A
password that can be read back is a password that can be leaked by any client,
log, or cache — and hashing means the server genuinely cannot produce it.
"""

from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.branches.models import Branch

User = get_user_model()


class BranchSerializer(serializers.ModelSerializer):
    """Read shape, matching the frontend's `Branch` type minus the password."""

    managerName = serializers.CharField(source="manager.name", read_only=True, default="")
    managerEmail = serializers.EmailField(source="manager.email", read_only=True, default="")
    managerCode = serializers.CharField(source="manager.staff_code", read_only=True, default="")
    therapistCount = serializers.IntegerField(source="therapist_count", read_only=True)
    supportCount = serializers.IntegerField(source="support_count", read_only=True)
    openedAt = serializers.DateField(source="opened_at", read_only=True)

    class Meta:
        model = Branch
        fields = [
            "id",
            "name",
            "code",
            "status",
            "address",
            "phone",
            "managerName",
            "managerEmail",
            "managerCode",
            "therapistCount",
            "supportCount",
            "openedAt",
        ]
        read_only_fields = fields


class BranchWriteSerializer(serializers.Serializer):
    """
    Create/update payload.

    Manager fields are flattened here to match the frontend's existing branch
    form, then split back out into the User record by `services.py`.
    """

    name = serializers.CharField(max_length=150)
    code = serializers.CharField(max_length=32)
    status = serializers.ChoiceField(choices=Branch.Status.choices, default=Branch.Status.ACTIVE)
    address = serializers.CharField()
    phone = serializers.CharField(max_length=32)

    manager_name = serializers.CharField(max_length=150)
    manager_email = serializers.EmailField()
    manager_code = serializers.CharField(max_length=32, required=False, allow_blank=True)
    # Required on create, optional on update (blank = keep current).
    manager_password = serializers.CharField(
        write_only=True, required=False, allow_blank=True, min_length=8
    )

    therapist_count = serializers.IntegerField(min_value=0, default=0)
    support_count = serializers.IntegerField(min_value=0, default=0)
    opened_at = serializers.DateField()

    def __init__(self, *args, **kwargs):
        self.instance = kwargs.get("instance")
        super().__init__(*args, **kwargs)

    def validate_code(self, value):
        value = value.strip().upper()
        existing = Branch.all_objects.filter(code=value)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("A branch with this code already exists.")
        return value

    def validate_manager_email(self, value):
        value = value.lower().strip()
        existing = User.all_objects.filter(email=value)
        # On update, the branch's own manager keeping their email is fine.
        if self.instance is not None and self.instance.manager_id:
            existing = existing.exclude(pk=self.instance.manager_id)
        if existing.exists():
            raise serializers.ValidationError("This email is already used by another account.")
        return value

    def validate_manager_password(self, value):
        """
        Same strength bar as a self-service change, not just a length check.

        An Admin sets this password for someone else, who then uses it to
        log in and handle real payments -- there's no reason that credential
        should be held to a lower standard than a manager changing their own.
        Blank stays blank here (optional-on-update, "keep current" signal);
        `validate()` below is what enforces non-blank on create.
        """
        if value:
            from django.contrib.auth.password_validation import validate_password

            validate_password(value)
        return value

    def validate(self, attrs):
        creating = self.instance is None
        if creating and not attrs.get("manager_password"):
            raise serializers.ValidationError(
                {"manager_password": ["A password is required when creating a branch manager."]}
            )
        return attrs


class BranchOverviewSerializer(serializers.Serializer):
    """
    Branch plus its headline figures, for the Admin branches grid.

    Aggregates are computed in the view — see `10-transactions-reporting.md`
    for why revenue counts only `paid` payments.
    """

    branch = BranchSerializer()
    patientCount = serializers.IntegerField()
    totalCollected = serializers.DecimalField(max_digits=14, decimal_places=2)
    monthlyRevenue = serializers.DecimalField(max_digits=14, decimal_places=2)
