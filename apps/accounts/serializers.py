"""
Auth and profile serializers.

Response shapes mirror what the frontend already consumes (`AuthUser` in
`src/types/domain.ts`): id, name, email, role, branchId.
"""

from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User


class UserSerializer(serializers.ModelSerializer):
    """The authenticated user, as the frontend's `AuthUser` shape."""

    branchId = serializers.CharField(source="branch_id", allow_null=True, read_only=True)
    branchName = serializers.CharField(source="branch.name", read_only=True, default=None)

    class Meta:
        model = User
        fields = ["id", "name", "email", "role", "branchId", "branchName", "staff_code"]
        read_only_fields = fields


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        user = authenticate(
            request=self.context.get("request"),
            username=attrs["email"].lower().strip(),
            password=attrs["password"],
        )

        # 401 rather than 400: these are authentication failures, not malformed
        # input, and the frontend renders `detail` under the password field.
        #
        # One identical message for both "no such user" and "wrong password" —
        # distinguishing them lets an attacker enumerate valid accounts.
        if user is None:
            raise AuthenticationFailed("Invalid email or password.")

        if not user.is_active or user.is_deleted:
            raise AuthenticationFailed("This account has been deactivated.")

        # A manager with no branch would be scoped to nothing and see empty
        # screens everywhere; fail clearly at login instead.
        if user.is_manager and user.branch_id is None:
            raise AuthenticationFailed(
                "This manager account is not assigned to a branch. Contact your administrator."
            )

        attrs["user"] = user
        return attrs

    def to_representation(self, instance):
        user = instance["user"]
        refresh = RefreshToken.for_user(user)
        return {
            "user": UserSerializer(user).data,
            "accessToken": str(refresh.access_token),
            "refreshToken": str(refresh),
        }


class ProfileUpdateSerializer(serializers.ModelSerializer):
    """
    Self-service profile edits.

    Deliberately narrow: a user may change their display name, nothing else.
    Role, branch, and email decide what they can see and do, so they are
    changed by an Admin through branch management — not by the user themselves.
    """

    class Meta:
        model = User
        fields = ["name"]

    def validate_name(self, value):
        value = value.strip()
        if len(value) < 2:
            raise serializers.ValidationError("Name must be at least 2 characters.")
        return value


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False)
    new_password = serializers.CharField(write_only=True, trim_whitespace=False, min_length=8)

    def validate_current_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        from django.contrib.auth.password_validation import validate_password

        validate_password(value, self.context["request"].user)
        return value

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=["password"])

        # A password change should end every other session, not just stop
        # accepting the old password on a fresh login -- otherwise a refresh
        # token issued before a compromised password was changed keeps
        # working for its full remaining lifetime regardless.
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken,
            OutstandingToken,
        )

        for outstanding in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=outstanding)

        return user


class RefreshRequestSerializer(serializers.Serializer):
    """Documentation-only shape for RefreshView's request body."""

    refreshToken = serializers.CharField()


class RefreshResponseSerializer(serializers.Serializer):
    """Documentation-only shape for RefreshView's response."""

    accessToken = serializers.CharField()
    refreshToken = serializers.CharField()
