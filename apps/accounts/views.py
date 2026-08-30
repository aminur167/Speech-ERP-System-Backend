"""Auth endpoints: login, refresh, logout, current user, profile."""

from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.serializers import (
    ChangePasswordSerializer,
    LoginSerializer,
    ProfileUpdateSerializer,
    RefreshRequestSerializer,
    RefreshResponseSerializer,
    UserSerializer,
)
from apps.common import audit
from apps.common.models import AuditLog

User = get_user_model()


class LoginView(GenericAPIView):
    """
    POST /api/auth/login/ -> { user, accessToken, refreshToken }

    Rate-limited: unauthenticated and password-guessable, so it's the obvious
    brute-force target.
    """

    permission_classes = [AllowAny]
    serializer_class = LoginSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.to_representation(serializer.validated_data)

        user = serializer.validated_data["user"]
        audit.record(
            actor=user,
            action=AuditLog.Action.LOGIN,
            target=user,
            branch=user.branch,
        )
        return Response(data, status=status.HTTP_200_OK)


class RefreshView(GenericAPIView):
    """
    POST /api/auth/refresh/ -> { accessToken, refreshToken }

    Rotation is on, so the old refresh token is blacklisted here — a stolen
    one can't be replayed after the legitimate client has refreshed.
    """

    permission_classes = [AllowAny]
    serializer_class = None

    @extend_schema(
        tags=["auth"],
        request=RefreshRequestSerializer,
        responses=RefreshResponseSerializer,
    )
    def post(self, request):
        raw_token = request.data.get("refreshToken") or request.data.get("refresh")
        if not raw_token:
            return Response(
                {"detail": "A refresh token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            refresh = RefreshToken(raw_token)
            access = str(refresh.access_token)
            # ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION are configured,
            # so issue a fresh refresh token and invalidate the presented one.
            try:
                refresh.blacklist()
            except AttributeError:
                # Blacklist app not installed — rotation still works, the old
                # token simply remains valid until it expires.
                pass
            new_refresh = RefreshToken.for_user(
                User.objects.get(pk=refresh.payload.get("user_id"))
            )
        except (TokenError, User.DoesNotExist):
            return Response(
                {"detail": "This session has expired. Please sign in again."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return Response(
            {"accessToken": access, "refreshToken": str(new_refresh)},
            status=status.HTTP_200_OK,
        )


class LogoutView(GenericAPIView):
    """
    POST /api/auth/logout/

    Blacklists the refresh token so it can't be reused. Succeeds even if the
    token is already invalid — logging out should never fail in a way that
    leaves the user stuck on a screen they think is authenticated.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = None

    @extend_schema(tags=["auth"], request=None, responses=None)
    def post(self, request):
        raw_token = request.data.get("refreshToken") or request.data.get("refresh")
        if raw_token:
            try:
                RefreshToken(raw_token).blacklist()
            except (TokenError, AttributeError):
                pass
        return Response(status=status.HTTP_204_NO_CONTENT)


class CurrentUserView(GenericAPIView):
    """
    GET /api/auth/me/ -> the authenticated user

    Used on page load to restore a session from a stored token.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = UserSerializer

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class ProfileView(GenericAPIView):
    """PATCH /api/auth/profile/ — the user's own display name."""

    permission_classes = [IsAuthenticated]
    serializer_class = ProfileUpdateSerializer

    def patch(self, request):
        serializer = self.get_serializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        before = {"name": request.user.name}
        serializer.save()
        after = {"name": request.user.name}

        audit.record(
            actor=request.user,
            action=AuditLog.Action.UPDATE,
            target=request.user,
            changes=audit.diff(before, after),
            branch=request.user.branch,
        )
        return Response(UserSerializer(request.user).data)


class ChangePasswordView(GenericAPIView):
    """POST /api/auth/change-password/"""

    permission_classes = [IsAuthenticated]
    serializer_class = ChangePasswordSerializer

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        audit.record(
            actor=request.user,
            action=AuditLog.Action.UPDATE,
            target=request.user,
            reason="Password changed",
            branch=request.user.branch,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
