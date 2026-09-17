"""
Closes out today's attendance across every branch, shortly after office
hours (4pm Asia/Dhaka) end: marks any no-show absent, and auto-checks-out
anyone still checked in who never tapped "Check Out".

    5 10 * * *  cd /srv/app && .venv/bin/python manage.py close_out_daily_attendance

(10:05 UTC == 16:05 Asia/Dhaka.)

Without this, both of those only ever happen lazily, the next time someone
opens the Staff page and its `today-attendance`/`summary` request happens to
trigger `mark_no_show_absentees`/`auto_check_out_stragglers` -- correct
eventually, but not "real-time" if nobody opens that page after 4pm. Running
this on a schedule closes that gap for every branch at once, independent of
anyone's usage of the app that day.

Deliberately daily, every day of the week: the underlying service functions
already no-op before 4pm, and `mark_no_show_absentees` skips the weekly
holiday (Friday) on its own, so the schedule doesn't need to know the
clinic's calendar -- one place decides that, not two.

Safe to run more than once a day or after a missed run: marking absent only
ever creates a row for a staff member who doesn't already have one for
today, and auto-check-out only ever touches a row that's checked in with no
check-out yet -- so nothing already recorded (a real check-in/check-out, a
manual on-leave/absent mark, or a previous run of this same command) is
touched twice.
"""

import logging

from django.core.management.base import BaseCommand, CommandError

from apps.staff import services
from apps.staff.models import StaffMember

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Auto-mark absent any active staff member with no attendance record for "
        "today, and auto-check-out anyone still checked in, once office hours end."
    )

    def handle(self, *args, **options):
        active_staff = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE)
        try:
            marked = services.mark_no_show_absentees(active_staff)
            checked_out = services.auto_check_out_stragglers(active_staff)
        except Exception as exc:
            logger.exception("Daily attendance close-out failed")
            raise CommandError(f"Attendance close-out failed: {exc}") from exc

        logger.info(
            "Daily attendance close-out complete: %s marked absent, %s auto-checked-out",
            marked,
            checked_out,
        )
        if marked or checked_out:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Marked {marked} staff member(s) absent, "
                    f"auto-checked-out {checked_out} staff member(s)."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("Nothing to close out."))
