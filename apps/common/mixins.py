"""
Branch scoping — the single implementation every branch-scoped view reuses.

This is deliberately one place rather than a per-module check. Getting it right
in eight ViewSets and forgetting the ninth is exactly how real multi-tenant
isolation bugs happen (docs/00-OVERVIEW.md, "Branch Data Isolation").

Rules enforced here:
  * A Manager's branch comes from `request.user.branch` — never from the
    request. A Manager sending `?branch=<other>` is ignored, not honoured.
  * Admin sees every branch, and may narrow to one with `?branch=<id>`.
  * Detail routes are scoped by the same queryset, so an out-of-scope object
    404s rather than leaking via a guessed id.
"""

from rest_framework.exceptions import ValidationError


class BranchScopedQuerySetMixin:
    """
    Restricts `get_queryset()` to the requesting user's branch.

    Set `branch_field` when the FK isn't named `branch` (e.g. a nested model
    reached via `enrollment__branch`).
    """

    branch_field = "branch"

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user

        if user.is_manager:
            if user.branch_id is None:
                # A manager with no branch can't be scoped to anything; showing
                # them everything would be the worst possible failure mode.
                return queryset.none()
            return queryset.filter(**{f"{self.branch_field}_id": user.branch_id})

        # Admin: unscoped, with optional narrowing.
        requested_branch = self.request.query_params.get("branch")
        if requested_branch:
            return queryset.filter(**{f"{self.branch_field}_id": requested_branch})
        return queryset

    def get_effective_branch_id(self):
        """
        The branch a write should be attributed to.

        Managers always write to their own branch. Admin must say which branch
        they mean — guessing would silently file a record under the wrong one.
        """
        user = self.request.user
        if user.is_manager:
            return user.branch_id

        branch_id = self.request.data.get("branch") or self.request.query_params.get("branch")
        if not branch_id:
            raise ValidationError(
                {"branch": ["Admin must specify which branch this belongs to."]}
            )
        return branch_id
