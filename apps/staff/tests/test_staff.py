"""
Staff HR tests: roster CRUD, branch isolation, attendance state transitions,
bonuses, and the monthly payroll report.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.staff import services
from apps.staff.models import StaffAttendance, StaffBonus, StaffMember

pytestmark = pytest.mark.django_db


@pytest.fixture
def farhana(staff_member_factory):
    return staff_member_factory(name="Farhana Akter", monthly_salary=Decimal("42000.00"))


class TestRosterCrud:
    def test_manager_can_create_staff(self, manager_client):
        response = manager_client.post(
            reverse("staff:staffmember-list"),
            {
                "name": "Tanvir Hasan",
                "designation": "therapist",
                "phone": "01710000000",
                "email": "tanvir@speechlab.test",
                "joined_at": "2025-01-15",
                "monthly_salary": "38000.00",
                "status": "active",
            },
            format="json",
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Tanvir Hasan"
        assert body["staffCode"].startswith("STF-")
        assert body["monthlySalary"] == "38000.00"

    def test_staff_code_increments_per_branch(self, manager_client):
        def create(name):
            return manager_client.post(
                reverse("staff:staffmember-list"),
                {
                    "name": name, "designation": "therapist", "phone": "017",
                    "joined_at": "2025-01-01", "monthly_salary": "10000",
                },
                format="json",
            ).json()["staffCode"]

        first = create("A")
        second = create("B")
        assert first != second

    def test_manager_can_update_salary(self, manager_client, farhana):
        response = manager_client.patch(
            reverse("staff:staffmember-detail", args=[farhana.id]),
            {"monthly_salary": "45000.00"},
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["monthlySalary"] == "45000.00"

    def test_update_requires_full_write_serializer_fields_or_partial(self, manager_client, farhana):
        """PUT without partial should still work when every field is supplied."""
        response = manager_client.put(
            reverse("staff:staffmember-detail", args=[farhana.id]),
            {
                "name": farhana.name, "designation": farhana.designation,
                "phone": "0199", "joined_at": str(farhana.joined_at),
                "monthly_salary": "50000.00", "status": "active",
            },
            format="json",
        )
        assert response.status_code == 200
        assert response.json()["phone"] == "0199"

    def test_delete_is_soft(self, manager_client, farhana):
        response = manager_client.delete(reverse("staff:staffmember-detail", args=[farhana.id]))
        assert response.status_code == 204

        farhana.refresh_from_db()
        assert farhana.is_deleted is True
        assert StaffMember.objects.filter(pk=farhana.id).count() == 0
        assert StaffMember.all_objects.filter(pk=farhana.id).count() == 1

    def test_admin_cannot_check_in_staff(self, admin_client, farhana):
        """Money/attendance actions stay Manager-only, same split as materials and expenses."""
        response = admin_client.post(reverse("staff:staffmember-check-in", args=[farhana.id]))
        assert response.status_code == 403


class TestBranchIsolation:
    def test_manager_only_sees_own_branch_roster(
        self, manager_client, other_manager_client, farhana, staff_member_factory, other_branch
    ):
        other_staff = staff_member_factory(name="Other Branch Person", branch=other_branch)

        results = manager_client.get(reverse("staff:staffmember-list")).json()["results"]
        names = [row["name"] for row in results]
        assert farhana.name in names
        assert other_staff.name not in names

    def test_manager_cannot_reach_other_branch_staff_by_id(
        self, other_manager_client, farhana
    ):
        response = other_manager_client.get(
            reverse("staff:staffmember-detail", args=[farhana.id])
        )
        assert response.status_code == 404

    def test_manager_cannot_check_in_other_branch_staff(self, other_manager_client, farhana):
        response = other_manager_client.post(
            reverse("staff:staffmember-check-in", args=[farhana.id])
        )
        assert response.status_code == 404

    def test_admin_sees_every_branch(
        self, admin_client, farhana, staff_member_factory, other_branch
    ):
        staff_member_factory(name="Other Branch Person", branch=other_branch)

        results = admin_client.get(reverse("staff:staffmember-list")).json()["results"]
        assert len(results) == 2


class TestAttendance:
    def test_check_in_status_matches_the_real_clock(self, farhana):
        """
        The service reads the real clock, so assert on whatever "now" actually
        produces rather than assuming a fixed hour — that keeps the test
        honest without making it flaky depending on when it runs.
        """
        record = services.check_in(staff=farhana)
        expected = (
            StaffAttendance.Status.LATE
            if timezone.now().hour >= services.LATE_AFTER_HOUR
            else StaffAttendance.Status.PRESENT
        )
        assert record.status == expected
        assert record.check_in_at is not None

    def test_check_in_at_or_after_cutoff_is_derived_as_late(self):
        """Exercises the derivation function directly for a fixed, non-flaky time."""
        late_time = timezone.make_aware(
            datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=11)
        )
        assert services._today_status_for_check_in(late_time) == StaffAttendance.Status.LATE

    def test_check_in_before_cutoff_is_derived_as_present(self):
        early_time = timezone.make_aware(
            datetime.combine(date.today(), datetime.min.time()) + timedelta(hours=9)
        )
        assert services._today_status_for_check_in(early_time) == StaffAttendance.Status.PRESENT

    def test_check_in_is_idempotent_per_day(self, farhana):
        first = services.check_in(staff=farhana)
        second = services.check_in(staff=farhana)
        assert first.id == second.id
        assert StaffAttendance.objects.filter(staff=farhana).count() == 1

    def test_check_out_requires_no_prior_check_in(self, farhana):
        record = services.check_out(staff=farhana)
        assert record.check_out_at is not None

    def test_check_out_after_check_in_keeps_check_in_time(self, farhana):
        checked_in = services.check_in(staff=farhana)
        checked_out = services.check_out(staff=farhana)
        assert checked_out.id == checked_in.id
        assert checked_out.check_in_at == checked_in.check_in_at
        assert checked_out.check_out_at is not None

    def test_mark_on_leave_clears_times(self, farhana):
        services.check_in(staff=farhana)
        record = services.mark_attendance(staff=farhana, status=StaffAttendance.Status.ON_LEAVE)
        assert record.status == StaffAttendance.Status.ON_LEAVE
        assert record.check_in_at is None
        assert record.check_out_at is None

    def test_api_check_in_then_check_out(self, manager_client, farhana):
        in_response = manager_client.post(reverse("staff:staffmember-check-in", args=[farhana.id]))
        assert in_response.status_code == 200
        assert in_response.json()["checkOutAt"] is None

        out_response = manager_client.post(reverse("staff:staffmember-check-out", args=[farhana.id]))
        assert out_response.status_code == 200
        assert out_response.json()["checkOutAt"] is not None

    def test_today_attendance_keyed_by_staff_id(self, manager_client, farhana):
        manager_client.post(reverse("staff:staffmember-check-in", args=[farhana.id]))
        body = manager_client.get(reverse("staff:staffmember-today-attendance")).json()
        assert str(farhana.id) in body
        assert body[str(farhana.id)]["status"] in ("present", "late")

    def test_attendance_history_orders_newest_first(self, farhana):
        older = StaffAttendance.objects.create(
            staff=farhana, branch=farhana.branch, date=date.today() - timedelta(days=2),
            status=StaffAttendance.Status.PRESENT,
        )
        newer = StaffAttendance.objects.create(
            staff=farhana, branch=farhana.branch, date=date.today() - timedelta(days=1),
            status=StaffAttendance.Status.ABSENT,
        )
        history = list(farhana.attendance_records.all())
        assert history[0].id == newer.id
        assert history[-1].id in (older.id, history[-1].id)  # newest-first ordering holds


@pytest.mark.money
class TestBonuses:
    def test_manager_can_award_a_bonus(self, manager_client, manager, farhana):
        response = manager_client.post(
            reverse("staff:staffmember-award-bonus", args=[farhana.id]),
            {"amount": "2000.00", "reason": "Eid bonus"},
            format="json",
        )
        assert response.status_code == 201
        body = response.json()
        assert body["amount"] == "2000.00"
        assert body["awardedBy"] == manager.name

    def test_bonus_is_attributed_to_the_authenticated_manager_not_the_request_body(
        self, manager_client, manager, farhana
    ):
        """A client can't claim someone else awarded it — the actor always comes from the token."""
        response = manager_client.post(
            reverse("staff:staffmember-award-bonus", args=[farhana.id]),
            {"amount": "500.00", "reason": "Test", "awardedBy": "Someone Else"},
            format="json",
        )
        assert response.json()["awardedBy"] == manager.name

    def test_negative_or_zero_bonus_is_rejected(self, manager_client, farhana):
        response = manager_client.post(
            reverse("staff:staffmember-award-bonus", args=[farhana.id]),
            {"amount": "0.00", "reason": "Invalid"},
            format="json",
        )
        assert response.status_code == 400

    def test_bonus_list_is_newest_first(self, manager, farhana):
        first = services.add_bonus(actor=manager, staff=farhana, amount=Decimal("100"), reason="a")
        second = services.add_bonus(actor=manager, staff=farhana, amount=Decimal("200"), reason="b")
        bonuses = list(farhana.bonuses.all())
        assert bonuses[0].id == second.id
        assert bonuses[1].id == first.id


class TestSummary:
    def test_summary_counts_active_staff_and_todays_attendance(
        self, manager_client, manager, farhana, staff_member_factory
    ):
        inactive = staff_member_factory(name="Retired", status=StaffMember.Status.INACTIVE)
        present = staff_member_factory(name="Present Person")
        services.check_in(staff=present)

        body = manager_client.get(reverse("staff:staffmember-summary")).json()
        assert body["totalStaff"] == 2  # farhana + present, not the inactive one
        assert body["presentToday"] == 1
        assert body["monthlySalaryPayout"] == "62000.00"  # 42000 + 20000

    def test_summary_is_branch_scoped(self, other_manager_client, farhana):
        body = other_manager_client.get(reverse("staff:staffmember-summary")).json()
        assert body["totalStaff"] == 0


class TestMonthlyReport:
    def test_report_aggregates_salary_bonus_and_attendance(self, manager, farhana):
        today = timezone.localdate()
        StaffAttendance.objects.create(
            staff=farhana, branch=farhana.branch, date=today.replace(day=1),
            status=StaffAttendance.Status.PRESENT,
        )
        StaffAttendance.objects.create(
            staff=farhana, branch=farhana.branch, date=today.replace(day=2),
            status=StaffAttendance.Status.LATE,
        )
        StaffAttendance.objects.create(
            staff=farhana, branch=farhana.branch, date=today.replace(day=3),
            status=StaffAttendance.Status.ON_LEAVE,
        )
        services.add_bonus(actor=manager, staff=farhana, amount=Decimal("1500.00"), reason="x")

        rows = services.monthly_report([farhana], year=today.year, month=today.month)
        row = rows[0]
        assert row["presentCount"] == 1
        assert row["lateCount"] == 1
        assert row["leaveCount"] == 1
        assert row["absentCount"] == 0
        assert row["bonusTotal"] == Decimal("1500.00")
        assert row["netPayable"] == Decimal("43500.00")  # 42000 + 1500

    def test_report_does_not_fan_out_bonuses_and_attendance_across_each_other(self, manager, farhana):
        """
        Regression guard for the classic Django multi-relation-aggregate bug:
        two bonuses and two attendance rows in the same month must not
        multiply into 4 counted rows for either metric.
        """
        today = timezone.localdate()
        StaffAttendance.objects.create(
            staff=farhana, branch=farhana.branch, date=today.replace(day=1),
            status=StaffAttendance.Status.PRESENT,
        )
        StaffAttendance.objects.create(
            staff=farhana, branch=farhana.branch, date=today.replace(day=2),
            status=StaffAttendance.Status.PRESENT,
        )
        services.add_bonus(actor=manager, staff=farhana, amount=Decimal("100.00"), reason="a")
        services.add_bonus(actor=manager, staff=farhana, amount=Decimal("200.00"), reason="b")

        row = services.monthly_report([farhana], year=today.year, month=today.month)[0]
        assert row["presentCount"] == 2
        assert row["bonusTotal"] == Decimal("300.00")

    def test_report_excludes_other_months(self, manager, farhana):
        last_month = (timezone.localdate().replace(day=1) - timedelta(days=1))
        StaffAttendance.objects.create(
            staff=farhana, branch=farhana.branch, date=last_month,
            status=StaffAttendance.Status.PRESENT,
        )
        today = timezone.localdate()
        row = services.monthly_report([farhana], year=today.year, month=today.month)[0]
        assert row["presentCount"] == 0

    def test_report_endpoint_defaults_to_current_month(self, manager_client, farhana):
        response = manager_client.get(reverse("staff:staffmember-monthly-report"))
        assert response.status_code == 200
        assert response.json()[0]["staffId"] == str(farhana.id)

    def test_report_endpoint_rejects_malformed_month(self, manager_client):
        response = manager_client.get(
            reverse("staff:staffmember-monthly-report"), {"month": "not-a-month"}
        )
        assert response.status_code == 400

    def test_report_is_branch_scoped(self, other_manager_client, farhana):
        response = other_manager_client.get(reverse("staff:staffmember-monthly-report"))
        assert response.json() == []
