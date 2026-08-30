"""
Monthly bill generation.

Run on a schedule (system cron calling this command is the recommended setup
for a single-VPS deployment — no extra broker or worker to keep alive):

    0 1 1 * *  cd /srv/app && .venv/bin/python manage.py generate_monthly_bills

Safe to run manually and safe to run twice: the unique constraint on
(enrollment, month) makes it idempotent, and it backfills any months missed
while the server was down rather than skipping them.

A silently dead billing job means the clinic stops invoicing without noticing,
so failures are logged loudly and exit non-zero for the scheduler to alert on.
"""

import logging
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from apps.enrollments.services import generate_due_bills

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Generate monthly bills for every active enrollment, backfilling any missed months."

    def add_arguments(self, parser):
        parser.add_argument(
            "--up-to",
            type=str,
            default=None,
            help="Generate bills through this month (YYYY-MM-DD). Defaults to today.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created without writing anything.",
        )

    def handle(self, *args, **options):
        up_to = None
        if options["up_to"]:
            try:
                up_to = datetime.strptime(options["up_to"], "%Y-%m-%d").date()
            except ValueError as exc:
                raise CommandError("--up-to must be YYYY-MM-DD.") from exc

        if options["dry_run"]:
            # Roll back rather than duplicating the logic in a "preview" branch
            # that could drift from what actually runs.
            from django.db import transaction

            try:
                with transaction.atomic():
                    result = generate_due_bills(up_to=up_to)
                    raise _DryRun(result)
            except _DryRun as preview:
                self.stdout.write(
                    self.style.WARNING(
                        f"[dry run] would create {preview.result['created']} bill(s)"
                    )
                )
                return

        try:
            result = generate_due_bills(up_to=up_to)
        except Exception as exc:
            logger.exception("Monthly bill generation failed")
            raise CommandError(f"Bill generation failed: {exc}") from exc

        created = result["created"]
        logger.info("Monthly bill generation complete: %s created", created)
        self.stdout.write(
            self.style.SUCCESS(f"Created {created} bill(s).")
            if created
            else self.style.SUCCESS("Already up to date — no bills needed.")
        )

        if result["enrollments_without_bills"]:
            # Shouldn't happen: enrollment creation always opens bills. Worth
            # surfacing rather than hiding, since it means someone is enrolled
            # and not being invoiced.
            self.stdout.write(
                self.style.WARNING(
                    f"{result['enrollments_without_bills']} active enrollment(s) have no "
                    f"bills at all and were skipped — investigate."
                )
            )


class _DryRun(Exception):
    def __init__(self, result):
        self.result = result
        super().__init__("dry run")
