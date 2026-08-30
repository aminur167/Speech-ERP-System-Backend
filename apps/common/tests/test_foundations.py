"""
Tests for the shared foundations.

These guard the pieces every later module builds on. A bug here doesn't stay
local — it becomes a data leak, a duplicate receipt number, or a missing audit
trail in whatever module inherits it.
"""

import threading

import pytest
from django.db import connection, transaction

from apps.common import audit
from apps.common.models import AuditLog, CodeSequence
from apps.common.sequences import format_code, generate, next_value

pytestmark = pytest.mark.django_db


class TestSoftDelete:
    def test_default_manager_hides_deleted_rows(self, branch):
        from apps.branches.models import Branch

        branch.delete()

        assert not Branch.objects.filter(pk=branch.pk).exists()
        assert Branch.all_objects.filter(pk=branch.pk).exists()

    def test_delete_sets_flag_and_timestamp(self, branch):
        branch.delete()
        branch.refresh_from_db()

        assert branch.is_deleted is True
        assert branch.deleted_at is not None

    def test_restore_brings_it_back(self, branch):
        from apps.branches.models import Branch

        branch.delete()
        branch.restore()

        assert Branch.objects.filter(pk=branch.pk).exists()
        assert branch.deleted_at is None

    def test_hard_delete_really_removes(self, branch):
        from apps.branches.models import Branch

        pk = branch.pk
        branch.hard_delete()

        assert not Branch.all_objects.filter(pk=pk).exists()


class TestCodeSequences:
    def test_values_increment(self):
        with transaction.atomic():
            assert next_value("test", 2026) == 1
            assert next_value("test", 2026) == 2
            assert next_value("test", 2026) == 3

    def test_scopes_are_independent(self):
        """
        Per-branch receipt series depend on this: two branches numbering in
        parallel must not interfere.
        """
        with transaction.atomic():
            assert next_value("receipt:DHK", 2026) == 1
            assert next_value("receipt:CTG", 2026) == 1
            assert next_value("receipt:DHK", 2026) == 2

    def test_years_are_independent(self):
        with transaction.atomic():
            next_value("receipt:DHK", 2026)
            assert next_value("receipt:DHK", 2027) == 1

    def test_format_with_year(self):
        assert format_code("PT", 2026, 42) == "PT-2026-00042"

    def test_format_without_year(self):
        assert format_code("MAT", None, 7) == "MAT-00007"

    def test_generate_produces_sequential_codes(self):
        assert generate("PT", year=2026) == "PT-2026-00001"
        assert generate("PT", year=2026) == "PT-2026-00002"

    def test_only_one_row_per_scope_year(self):
        with transaction.atomic():
            for _ in range(5):
                next_value("test", 2026)

        assert CodeSequence.objects.filter(scope="test", year=2026).count() == 1
        assert CodeSequence.objects.get(scope="test", year=2026).last_value == 5


@pytest.mark.slow
@pytest.mark.money
class TestCodeSequenceConcurrency:
    """
    The property that actually matters: no duplicates under concurrent load.

    The mock's `sequence += 1` passes every single-threaded test and still
    produces duplicate receipt numbers in production across Gunicorn workers.
    Only a concurrent test catches that, and it only means something on
    PostgreSQL, where select_for_update genuinely locks.
    """

    def test_concurrent_callers_never_get_the_same_number(self, django_db_blocker):
        results = []
        errors = []
        worker_count = 10

        def worker():
            try:
                with django_db_blocker.unblock():
                    with transaction.atomic():
                        results.append(next_value("concurrent", 2026))
            except Exception as exc:  # surfaced below rather than swallowed
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, f"workers raised: {errors}"
        assert len(results) == worker_count
        assert len(set(results)) == worker_count, f"duplicates issued: {sorted(results)}"
        assert sorted(results) == list(range(1, worker_count + 1))


class TestAuditLog:
    def test_record_captures_actor_action_and_target(self, manager, branch):
        entry = audit.record(
            actor=manager,
            action=AuditLog.Action.UPDATE,
            target=branch,
            reason="Phone number corrected",
        )

        assert entry.actor_id == manager.id
        assert entry.actor_email == manager.email
        assert entry.action == AuditLog.Action.UPDATE
        assert entry.target_type == "Branch"
        assert entry.target_id == str(branch.pk)
        assert entry.reason == "Phone number corrected"

    def test_branch_inferred_from_target(self, manager, branch):
        """Callers shouldn't have to remember to pass branch for the common case."""
        entry = audit.record(actor=manager, action=AuditLog.Action.UPDATE, target=manager)
        assert entry.branch_id == branch.id

    def test_entry_survives_actor_removal(self, manager, branch):
        """
        The trail must outlive the user. An audit record that vanishes when
        someone is removed is exactly the record a dispute needs.
        """
        entry = audit.record(actor=manager, action=AuditLog.Action.UPDATE, target=branch)
        manager_email = manager.email

        manager.hard_delete()
        entry.refresh_from_db()

        assert entry.actor_id is None
        assert entry.actor_email == manager_email  # denormalised, still readable

    def test_diff_reports_only_changed_fields(self):
        changes = audit.diff(
            {"name": "Old", "phone": "123", "status": "active"},
            {"name": "New", "phone": "123", "status": "active"},
        )

        assert changes == {"name": {"from": "Old", "to": "New"}}

    def test_diff_empty_when_nothing_changed(self):
        assert audit.diff({"name": "Same"}, {"name": "Same"}) == {}

    def test_entries_are_newest_first(self, manager, branch):
        audit.record(actor=manager, action=AuditLog.Action.CREATE, target=branch)
        audit.record(actor=manager, action=AuditLog.Action.UPDATE, target=branch)

        actions = list(AuditLog.objects.values_list("action", flat=True))
        assert actions[0] == AuditLog.Action.UPDATE


class TestUserModel:
    def test_role_helpers(self, admin_user, manager):
        assert admin_user.is_admin and not admin_user.is_manager
        assert manager.is_manager and not manager.is_admin

    def test_email_normalised_to_lowercase(self, db, branch):
        from apps.accounts.models import User

        user = User.objects.create_user(
            email="MixedCase@SpeechLab.TEST",
            password="pass-123-456",
            name="Mixed Case",
            role=User.Role.MANAGER,
            branch=branch,
        )
        assert user.email == "mixedcase@speechlab.test"

    def test_manager_without_branch_fails_validation(self, db):
        from django.core.exceptions import ValidationError

        from apps.accounts.models import User

        user = User(email="x@speechlab.test", name="X", role=User.Role.MANAGER, branch=None)
        with pytest.raises(ValidationError):
            user.clean()

    def test_admin_with_branch_fails_validation(self, db, branch):
        from django.core.exceptions import ValidationError

        from apps.accounts.models import User

        user = User(email="y@speechlab.test", name="Y", role=User.Role.ADMIN, branch=branch)
        with pytest.raises(ValidationError):
            user.clean()
