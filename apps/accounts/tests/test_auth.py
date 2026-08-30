"""
Auth tests.

Covers the login contract the frontend depends on, plus the security
properties that matter more than the happy path: no account enumeration, no
password ever readable back, and inactive/unassigned accounts refused.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.common.models import AuditLog

pytestmark = pytest.mark.django_db


class TestLogin:
    def test_manager_logs_in_and_receives_tokens_and_user(self, api_client, manager, branch):
        response = api_client.post(
            reverse("accounts:login"),
            {"email": manager.email, "password": "manager-pass-123"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["accessToken"]
        assert body["refreshToken"]
        assert body["user"]["email"] == manager.email
        assert body["user"]["role"] == "manager"
        assert body["user"]["branchId"] == str(branch.id)
        assert body["user"]["branchName"] == branch.name

    def test_admin_logs_in_with_null_branch(self, api_client, admin_user):
        response = api_client.post(
            reverse("accounts:login"),
            {"email": admin_user.email, "password": "admin-pass-123"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["user"]["role"] == "admin"
        assert body["user"]["branchId"] is None

    def test_email_is_case_insensitive(self, api_client, manager):
        response = api_client.post(
            reverse("accounts:login"),
            {"email": manager.email.upper(), "password": "manager-pass-123"},
        )
        assert response.status_code == 200

    def test_password_is_never_returned(self, api_client, manager):
        response = api_client.post(
            reverse("accounts:login"),
            {"email": manager.email, "password": "manager-pass-123"},
        )
        assert "password" not in response.content.decode().lower()

    def test_wrong_password_rejected(self, api_client, manager):
        response = api_client.post(
            reverse("accounts:login"),
            {"email": manager.email, "password": "wrong-password"},
        )
        assert response.status_code == 401
        assert "accessToken" not in response.json()

    def test_unknown_and_wrong_password_give_identical_errors(self, api_client, manager):
        """
        No account enumeration: an attacker must not be able to tell a real
        email from a fake one by comparing responses.
        """
        unknown = api_client.post(
            reverse("accounts:login"),
            {"email": "nobody@speechlab.test", "password": "whatever-123"},
        )
        wrong_password = api_client.post(
            reverse("accounts:login"),
            {"email": manager.email, "password": "definitely-wrong"},
        )

        assert unknown.status_code == wrong_password.status_code == 401
        assert unknown.json() == wrong_password.json()

    def test_deactivated_account_refused(self, api_client, manager):
        manager.is_active = False
        manager.save(update_fields=["is_active"])

        response = api_client.post(
            reverse("accounts:login"),
            {"email": manager.email, "password": "manager-pass-123"},
        )
        assert response.status_code == 401

    def test_soft_deleted_account_refused(self, api_client, manager):
        manager.delete()  # soft delete

        response = api_client.post(
            reverse("accounts:login"),
            {"email": manager.email, "password": "manager-pass-123"},
        )
        assert response.status_code == 401

    def test_manager_without_branch_refused_with_clear_message(self, api_client, db):
        """
        A branch-less manager would be scoped to nothing and see empty screens
        everywhere. Fail at login with an explanation instead.
        """
        orphan = User.objects.create_user(
            email="orphan@speechlab.test",
            password="orphan-pass-123",
            name="Orphan Manager",
            role=User.Role.MANAGER,
            branch=None,
        )

        response = api_client.post(
            reverse("accounts:login"),
            {"email": orphan.email, "password": "orphan-pass-123"},
        )

        assert response.status_code == 401
        assert "branch" in response.content.decode().lower()

    def test_error_body_uses_detail_key(self, api_client, manager):
        """
        The frontend renders `error.response.data.detail` under the password
        field. A different key would silently show nothing.
        """
        response = api_client.post(
            reverse("accounts:login"),
            {"email": manager.email, "password": "wrong"},
        )
        assert "detail" in response.json()

    def test_missing_fields_rejected(self, api_client):
        assert api_client.post(reverse("accounts:login"), {}).status_code == 400

    def test_login_is_audited(self, api_client, manager):
        api_client.post(
            reverse("accounts:login"),
            {"email": manager.email, "password": "manager-pass-123"},
        )

        entry = AuditLog.objects.filter(action=AuditLog.Action.LOGIN).first()
        assert entry is not None
        assert entry.actor_id == manager.id
        assert entry.actor_email == manager.email


class TestLoginThrottling:
    """
    Brute-force protection.

    Throttling is disabled in the test settings so unrelated tests aren't
    flaky, so it's re-enabled here explicitly — otherwise the protection could
    be removed from production settings and no test would notice.
    """

    @pytest.fixture(autouse=True)
    def enable_throttle(self, settings):
        settings.REST_FRAMEWORK = {
            **settings.REST_FRAMEWORK,
            "DEFAULT_THROTTLE_RATES": {"login": "5/min"},
        }
        # Rates are read at class construction; clear any cached throttle
        # history so counts start from zero for this test.
        from django.core.cache import cache

        cache.clear()
        yield
        cache.clear()

    def test_repeated_failures_are_eventually_throttled(self, api_client, manager):
        statuses = []
        for _ in range(8):
            response = api_client.post(
                reverse("accounts:login"),
                {"email": manager.email, "password": "wrong-password"},
            )
            statuses.append(response.status_code)

        assert 429 in statuses, f"login was never throttled: {statuses}"

    def test_throttle_applies_before_credentials_are_checked(self, api_client, manager):
        """
        Once throttled, even correct credentials are refused — otherwise an
        attacker could keep guessing and the limit would only slow down the
        failures, not the attack.
        """
        for _ in range(8):
            api_client.post(
                reverse("accounts:login"),
                {"email": manager.email, "password": "wrong-password"},
            )

        response = api_client.post(
            reverse("accounts:login"),
            {"email": manager.email, "password": "manager-pass-123"},
        )
        assert response.status_code == 429


class TestCurrentUser:
    def test_returns_authenticated_user(self, manager_client, manager):
        response = manager_client.get(reverse("accounts:me"))
        assert response.status_code == 200
        assert response.json()["email"] == manager.email

    def test_requires_authentication(self, api_client):
        assert api_client.get(reverse("accounts:me")).status_code == 401

    def test_rejects_garbage_token(self, api_client):
        api_client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")
        assert api_client.get(reverse("accounts:me")).status_code == 401


class TestTokenRefresh:
    def test_refresh_returns_new_tokens(self, api_client, manager):
        login = api_client.post(
            reverse("accounts:login"),
            {"email": manager.email, "password": "manager-pass-123"},
        ).json()

        response = api_client.post(
            reverse("accounts:refresh"), {"refreshToken": login["refreshToken"]}
        )

        assert response.status_code == 200
        assert response.json()["accessToken"]
        assert response.json()["refreshToken"]

    def test_refresh_rejects_invalid_token(self, api_client):
        response = api_client.post(reverse("accounts:refresh"), {"refreshToken": "nonsense"})
        assert response.status_code == 401

    def test_refresh_requires_a_token(self, api_client):
        assert api_client.post(reverse("accounts:refresh"), {}).status_code == 400


class TestProfile:
    def test_user_can_change_own_name(self, manager_client, manager):
        response = manager_client.patch(reverse("accounts:profile"), {"name": "Farhana R."})

        assert response.status_code == 200
        manager.refresh_from_db()
        assert manager.name == "Farhana R."

    def test_role_and_branch_cannot_be_changed_via_profile(
        self, manager_client, manager, other_branch
    ):
        """
        Role and branch decide what a user can see. Letting them be edited
        through the self-service profile endpoint would be privilege
        escalation — a manager could make themselves an admin.
        """
        manager_client.patch(
            reverse("accounts:profile"),
            {"name": "Renamed", "role": "admin", "branch": str(other_branch.id)},
        )

        manager.refresh_from_db()
        assert manager.role == User.Role.MANAGER
        assert manager.branch_id != other_branch.id

    def test_too_short_name_rejected(self, manager_client):
        assert manager_client.patch(reverse("accounts:profile"), {"name": "x"}).status_code == 400

    def test_profile_change_is_audited(self, manager_client, manager):
        manager_client.patch(reverse("accounts:profile"), {"name": "Audited Name"})

        entry = AuditLog.objects.filter(
            action=AuditLog.Action.UPDATE, target_type="User"
        ).first()
        assert entry is not None
        assert entry.changes["name"]["to"] == "Audited Name"

    def test_requires_authentication(self, api_client):
        assert api_client.patch(reverse("accounts:profile"), {"name": "Nope"}).status_code == 401


class TestChangePassword:
    def test_user_can_change_password(self, manager_client, manager):
        response = manager_client.post(
            reverse("accounts:change-password"),
            {"current_password": "manager-pass-123", "new_password": "brand-new-pass-456"},
        )

        assert response.status_code == 204
        manager.refresh_from_db()
        assert manager.check_password("brand-new-pass-456")

    def test_wrong_current_password_rejected(self, manager_client, manager):
        response = manager_client.post(
            reverse("accounts:change-password"),
            {"current_password": "not-the-password", "new_password": "brand-new-pass-456"},
        )

        assert response.status_code == 400
        manager.refresh_from_db()
        assert manager.check_password("manager-pass-123")  # unchanged

    def test_weak_new_password_rejected(self, manager_client):
        response = manager_client.post(
            reverse("accounts:change-password"),
            {"current_password": "manager-pass-123", "new_password": "12345678"},
        )
        assert response.status_code == 400


class TestPasswordStorage:
    def test_password_is_hashed_not_stored_raw(self, manager):
        assert manager.password != "manager-pass-123"
        assert manager.password.startswith(("pbkdf2_", "argon2", "bcrypt", "md5$"))
        assert manager.check_password("manager-pass-123")
