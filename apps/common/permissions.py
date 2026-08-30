"""
Role permissions.

Every rule here is enforced server-side regardless of what the UI shows. The
frontend hides buttons a role shouldn't use; that is a convenience, not a
control — see docs/00-OVERVIEW.md.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsAdmin(BasePermission):
    """Admin only. Used for branch management, service catalog writes, approvals."""

    message = "Only an administrator can perform this action."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_admin)


class IsManager(BasePermission):
    """
    Branch manager only.

    Deliberately excludes Admin for money-moving actions that must happen at
    the branch: collecting payments, selling materials, submitting the daily
    closing. Admin can see everything but shouldn't transact on a branch's
    behalf (docs/07, docs/09).
    """

    message = "Only a branch manager can perform this action."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_manager)


class IsAdminOrReadOnly(BasePermission):
    """Anyone authenticated may read; only Admin may write."""

    message = "Only an administrator can modify this."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return user.is_admin
