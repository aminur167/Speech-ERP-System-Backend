"""
Reading a past day's attendance, and a past month's payroll.

Both used to be filtered by **today's active roster**, which is the wrong
list for a backward-looking question. Two separate consequences, and each
one loses real records:

1. **Somebody who has left the clinic disappears from the days they
   worked.** `StaffMember` is soft-delete, so a resignation hides the row
   from the default manager — and with it, that person's attendance on every
   past day and their salary and bonuses in every past month. The
   `StaffAttendance` rows were never touched; nothing could see them.
2. **A past day was being auto-closed.** `mark_no_show_absentees` ran on
   every read of this endpoint. Asking for last Tuesday would mark everyone
   with no row that day absent — inventing an observation nobody made, and
   permanently, since `check_in` refuses once the office has closed.

The rule these pin: *history is what was recorded, not what today's roster
says about it.*
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.staff.models import StaffAttendance, StaffBonus, StaffMember

pytestmark = pytest.mark.django_db

ATTENDANCE_URL = reverse("staff:staffmember-today-attendance")
REPORT_URL = reverse("staff:staffmember-monthly-report")


@pytest.fixture
def departed(staff_member_factory):
    """Someone who worked here and has since left."""
    return staff_member_factory(name="Shahana Begum", monthly_salary=Decimal("25000.00"))


@pytest.fixture
def current(staff_member_factory):
    return staff_member_factory(name="Tanvir Rahman", monthly_salary=Decimal("40000.00"))


def mark(staff, on, status=StaffAttendance.Status.PRESENT, check_in=None):
    return StaffAttendance.objects.create(
        staff=staff,
        branch=staff.branch,
        date=on,
        status=status,
        check_in_at=check_in,
    )


class TestAPastDayKeepsStaffWhoHaveLeft:
    def test_a_departed_member_is_still_on_the_day_they_worked(
        self, manager_client, departed, current
    ):
        yesterday = timezone.localdate() - timedelta(days=1)
        mark(departed, yesterday)
        mark(current, yesterday)
        departed.delete()

        body = manager_client.get(ATTENDANCE_URL, {"date": yesterday.isoformat()}).json()

        assert set(body) == {str(departed.id), str(current.id)}
        assert body[str(departed.id)]["status"] == StaffAttendance.Status.PRESENT

    def test_they_are_gone_from_the_live_roster_all_the_same(
        self, manager_client, departed
    ):
        """The fix is about history, not about un-deleting anyone."""
        departed.delete()

        body = manager_client.get(reverse("staff:staffmember-list")).json()

        listed = body["results"] if isinstance(body, dict) else body
        assert all(row["id"] != str(departed.id) for row in listed)

    @pytest.mark.isolation
    def test_another_branch_is_still_out_of_scope(
        self, other_manager_client, departed
    ):
        yesterday = timezone.localdate() - timedelta(days=1)
        mark(departed, yesterday)
        departed.delete()

        body = other_manager_client.get(
            ATTENDANCE_URL, {"date": yesterday.isoformat()}
        ).json()

        assert body == {}


class TestAPastDayIsNotAutoClosed:
    def test_nobody_is_marked_absent_for_a_day_already_over(
        self, manager_client, current
    ):
        """
        The destructive half. `mark_no_show_absentees` wrote ABSENT rows for
        anyone without one, and `check_in` refuses after 4pm — so a single
        read of an old date permanently recorded absences nobody observed.
        """
        long_ago = timezone.localdate() - timedelta(days=30)

        manager_client.get(ATTENDANCE_URL, {"date": long_ago.isoformat()})

        assert not StaffAttendance.objects.filter(date=long_ago).exists()

    def test_a_past_day_returns_only_what_was_recorded(
        self, manager_client, current, staff_member_factory
    ):
        """
        No row means nobody, not an absence. The screen shows the day's sheet
        as it stands; it does not invent a line per staff member.
        """
        yesterday = timezone.localdate() - timedelta(days=1)
        staff_member_factory(name="Never Marked")
        mark(current, yesterday)

        body = manager_client.get(ATTENDANCE_URL, {"date": yesterday.isoformat()}).json()

        assert list(body) == [str(current.id)]

    def test_today_still_runs_the_auto_close(self, manager_client, current):
        """Unchanged for the live sheet — the whole point of the job."""
        response = manager_client.get(ATTENDANCE_URL)

        assert response.status_code == 200

    def test_a_malformed_date_is_refused_not_silently_today(
        self, manager_client, current
    ):
        response = manager_client.get(ATTENDANCE_URL, {"date": "31-10-2026"})

        assert response.status_code == 400
        assert "date" in response.json()


class TestPayrollKeepsStaffWhoHaveLeft:
    def test_a_member_who_left_mid_month_is_still_in_that_month(
        self, manager_client, departed, current
    ):
        """
        Half a month worked is half a month owed. Their salary line vanished
        from the report the moment they were removed from the roster.
        """
        departed.delete()

        today = timezone.localdate()
        rows = manager_client.get(
            REPORT_URL, {"month": f"{today.year}-{today.month:02d}"}
        ).json()

        names = {row["name"] for row in rows}
        assert names == {"Shahana Begum", "Tanvir Rahman"}

    def test_their_bonuses_and_attendance_come_with_them(
        self, manager_client, departed
    ):
        today = timezone.localdate()
        StaffBonus.objects.create(
            staff=departed, branch=departed.branch,
            amount=Decimal("2000.00"), reason="Eid bonus",
        )
        mark(departed, today)
        departed.delete()

        rows = manager_client.get(
            REPORT_URL, {"month": f"{today.year}-{today.month:02d}"}
        ).json()

        row = next(r for r in rows if r["name"] == "Shahana Begum")
        assert Decimal(row["bonusTotal"]) == Decimal("2000.00")
        assert Decimal(row["netPayable"]) == Decimal("27000.00")
        assert row["presentCount"] == 1

    @pytest.mark.isolation
    def test_the_report_is_still_branch_scoped(
        self, other_manager_client, departed
    ):
        departed.delete()

        today = timezone.localdate()
        rows = other_manager_client.get(
            REPORT_URL, {"month": f"{today.year}-{today.month:02d}"}
        ).json()

        assert rows == []
