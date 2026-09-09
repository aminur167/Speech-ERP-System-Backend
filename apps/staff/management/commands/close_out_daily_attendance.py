"""
Closes out today's attendance across every branch, shortly after office
hours (4pm Asia/Dhaka) end:

    5 10 * * *  cd /srv/app && .venv/bin/python manage.py close_out_daily_attendance

(10:05 UTC == 16:05 Asia/Dhaka.)

Without this, a no-show is only ever marked absent lazily, the next time
someone opens the Staff page and its `today-attendance`/`summary` request
happens to trigger `mark_no_show_absentees` -- correct eventually, but not
"real-time" if nobody opens that page after 4pm. Running this on a schedule
closes that gap for every branch at once, independent of anyone's usage of
the app that day.

Deliberately daily, every day of the week: the underlying
`mark_no_show_absentees` already no-ops on the weekly holiday (Friday) and
before 4pm, so the schedule doesn't need to know the clinic's calendar --
one place decides that, not two.

Safe to run more than once a day or after a missed run: it only ever
creates a row for a staff member who doesn't already have one for today,
so nothing already recorded (a real check-in, a manual on-leave/absent
mark, or a previous run of this same command) is touched.
"""

import logging

from django.core.management.base import BaseCommand, CommandError

from apps.staff import services
from apps.staff.models import StaffMember

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Auto-mark absent any active staff member with no attendance record for today, once office hours end."

    def handle(self, *args, **options):
        try:
            marked = services.mark_no_show_absentees(
                StaffMember.objects.filter(status=StaffMember.Status.ACTIVE)
            )
        except Exception as exc:
            logger.exception("Daily attendance close-out failed")
            raise CommandError(f"Attendance close-out failed: {exc}") from exc

        logger.info("Daily attendance close-out complete: %s marked absent", marked)
        self.stdout.write(
            self.style.SUCCESS(f"Marked {marked} staff member(s) absent.")
            if marked
            else self.style.SUCCESS("Nothing to close out — no absent staff.")
        )
