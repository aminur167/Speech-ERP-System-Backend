"""
Automatic termination of monthly services with an unpaid due.

Run on a schedule, just after midnight so it sees the month that has just
finished:

    5 0 * * *  cd /srv/app && .venv/bin/python manage.py terminate_unpaid_monthly_services

Deliberately daily rather than monthly. The rule is "unpaid when the month
ends", and a service that dodged one run because the server was down must
still be caught the next day — the same catch-up property `generate_due_bills`
has, and for the same reason.

Safe to run twice: an already-terminated enrollment isn't in the queryset, so
nothing is terminated a second time. It never writes the debt off either;
that stays payable if the patient comes back and resumes.
"""

import logging
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from apps.enrollments.services import terminate_unpaid_monthly_services

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Terminate monthly services whose due for a finished month is still unpaid."

    def add_arguments(self, parser):
        parser.add_argument(
            "--on",
            type=str,
            default=None,
            help="Run as though today were this date (YYYY-MM-DD). Defaults to today.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be terminated without writing anything.",
        )

    def handle(self, *args, **options):
        on = None
        if options["on"]:
            try:
                on = datetime.strptime(options["on"], "%Y-%m-%d").date()
            except ValueError as exc:
                raise CommandError("--on must be YYYY-MM-DD.") from exc

        if options["dry_run"]:
            # Roll back the real thing rather than maintaining a separate
            # preview path that could drift from what actually runs.
            from django.db import transaction

            try:
                with transaction.atomic():
                    result = terminate_unpaid_monthly_services(on=on)
                    raise _DryRun(result)
            except _DryRun as preview:
                self.stdout.write(
                    self.style.WARNING(
                        f"[dry run] would terminate {preview.result['terminated']} service(s)"
                    )
                )
                return

        try:
            result = terminate_unpaid_monthly_services(on=on)
        except Exception as exc:
            logger.exception("Automatic monthly termination failed")
            raise CommandError(f"Termination run failed: {exc}") from exc

        count = result["terminated"]
        logger.info("Automatic monthly termination complete: %s terminated", count)
        self.stdout.write(
            self.style.SUCCESS(f"Terminated {count} service(s) for unpaid dues.")
            if count
            else self.style.SUCCESS("Nothing overdue — no service terminated.")
        )


class _DryRun(Exception):
    def __init__(self, result):
        self.result = result
        super().__init__("dry run")
