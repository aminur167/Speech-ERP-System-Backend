"""
Seeds the monthly termination / resume flow with something to click through.

Deliberately built on the branch, manager and service already in the
database rather than inventing a parallel set: the point is to exercise the
real screens with the real catalogue, and a scenario built on a service
nobody offers proves nothing about the one they do.

Every scenario is produced by the real code path — `create_monthly_enrollment`
for the enrollment, `collect_bill_payment` for money, and the actual
`terminate_unpaid_monthly_services` job for the terminations. Nothing is
hand-written into a status field, so what you see on screen is what the
system genuinely does rather than what a fixture claimed.

    python manage.py seed_termination_scenarios
    python manage.py seed_termination_scenarios --branch BR-DHK-001
    python manage.py seed_termination_scenarios --undo
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.branches.models import Branch
from apps.enrollments import services
from apps.enrollments.models import (
    BillStatus,
    EnrollmentStatus,
    MonthlyEnrollment,
    due_date_for_month,
)
from apps.patients.models import Patient
from apps.patients.services import create_patient
from apps.services.models import Service

# Stamped on any patient this command has to invent, so `--undo` can tell
# them from the clinic's own records and never touch a real one.
MARKER = "[seed:termination-scenarios]"

# name, months of unpaid history, what should happen to it
SCENARIOS = [
    ("Paid up - should survive the job", 0, "paid"),
    ("Current month due - collectable now", 0, "due"),
    ("One month unpaid - auto-terminated", 1, "auto"),
    ("Three months unpaid - auto-terminated", 3, "auto"),
    ("Stopped by the manager", 0, "manual"),
]


class Command(BaseCommand):
    help = "Create monthly-service scenarios for testing termination and resume."

    def add_arguments(self, parser):
        parser.add_argument(
            "--branch",
            type=str,
            default=None,
            help="Branch code or id. Defaults to the first branch offering a monthly service.",
        )
        parser.add_argument(
            "--undo",
            action="store_true",
            help="Remove what a previous run created. Anything that has taken money is left alone.",
        )

    def handle(self, *args, **options):
        branch = self._resolve_branch(options["branch"])

        if options["undo"]:
            self._undo(branch)
            return

        service = (
            Service.objects.filter(
                branch=branch, category=Service.Category.MONTHLY, is_active=True
            )
            .order_by("id")
            .first()
        )
        if service is None:
            raise CommandError(
                f"{branch.name} has no active monthly service. Add one first -- "
                f"seeding against a service the branch doesn't offer would prove nothing."
            )

        manager = User.objects.filter(role=User.Role.MANAGER, branch=branch).first()
        if manager is None:
            raise CommandError(f"{branch.name} has no manager to attribute the actions to.")

        self.stdout.write(
            f"Branch:  {branch.name} ({branch.code})\n"
            f"Manager: {manager.email}\n"
            f"Service: {service.name} -- {service.fee}/month\n"
        )

        with transaction.atomic():
            patients = self._patients_for(branch, manager, len(SCENARIOS))
            created = []
            for (label, unpaid_months, outcome), patient in zip(SCENARIOS, patients):
                created.append(
                    self._build(
                        actor=manager, branch=branch, service=service, patient=patient,
                        label=label, unpaid_months=unpaid_months, outcome=outcome,
                    )
                )

            # The real job, not a shortcut — the terminations below are the
            # ones production would produce.
            result = services.terminate_unpaid_monthly_services(actor=manager)

        self.stdout.write(
            self.style.SUCCESS(f"\nThe job terminated {result['terminated']} service(s).\n")
        )
        self._report(created)

    # -- building blocks ---------------------------------------------------

    def _resolve_branch(self, given):
        if given:
            branch = Branch.objects.filter(code=given).first() or Branch.objects.filter(
                pk=given if str(given).isdigit() else None
            ).first()
            if branch is None:
                raise CommandError(f"No branch matches {given!r}.")
            return branch

        branch = (
            Branch.objects.filter(
                services__category=Service.Category.MONTHLY, services__is_active=True
            )
            .order_by("id")
            .first()
        )
        if branch is None:
            raise CommandError("No branch offers a monthly service yet.")
        return branch

    def _patients_for(self, branch, manager, needed):
        """
        The branch's own patients first, inventing only what's missing.

        Reusing real records is the point — they carry real names and phone
        numbers, which is what the screen's search is actually used with.
        Invented ones are stamped so `--undo` can tell them apart.
        """
        # Real people first, in the order that keeps the scenarios readable:
        # someone with no monthly service at all, then someone who already has
        # one (a patient may legitimately hold a second), and only then an
        # invented patient. Inventing is the last resort rather than the first
        # move -- a scenario carrying a real name and phone number is what the
        # screen's search is actually used with.
        free = list(
            Patient.objects.filter(branch=branch)
            .exclude(monthly_enrollments__isnull=False)
            .order_by("id")[:needed]
        )
        chosen = list(free)

        if len(chosen) < needed:
            already_enrolled = (
                Patient.objects.filter(branch=branch)
                .exclude(pk__in=[p.pk for p in chosen])
                .order_by("id")[: needed - len(chosen)]
            )
            chosen.extend(already_enrolled)

        self.stdout.write(
            f"Reusing {len(chosen)} of the branch's own patient(s)"
            + (f", {len(free)} of them with no monthly service yet." if free else ".")
        )

        invented = 0
        for index in range(len(chosen), needed):
            chosen.append(
                create_patient(
                    actor=manager,
                    branch=branch,
                    data={
                        "name": f"Test Patient {index + 1}",
                        "phone": f"019{str(11000000 + index * 137)[:8]}",
                        "notes": MARKER,
                    },
                )
            )
            invented += 1

        if invented:
            self.stdout.write(f"Invented {invented} patient(s) to make up the numbers.")
        return chosen

    def _build(self, *, actor, branch, service, patient, label, unpaid_months, outcome):
        enrollment = services.create_monthly_enrollment(
            actor=actor, branch=branch, patient=patient, service=service
        )

        if unpaid_months:
            self._backdate(enrollment, unpaid_months)
        elif outcome == "paid":
            services.collect_bill_payment(
                actor=actor, branch=branch,
                bill=enrollment.oldest_unpaid_bill(), method="cash",
            )
        elif outcome == "manual":
            services.terminate(actor=actor, container=enrollment)

        enrollment.refresh_from_db()
        return (label, outcome, enrollment)

    def _backdate(self, enrollment, unpaid_months):
        """
        Rewind the billing so `unpaid_months` finished months went unpaid.

        Backdated rather than the clock being wound forward, because that is
        what production looks like: real time passed, the patient didn't pay,
        and the nightly job then finds a month that is genuinely over. Each
        bill gets its own month — (enrollment, month) is unique.
        """
        start = services.add_months(
            timezone.localdate().replace(day=1), -unpaid_months
        )

        for offset, bill in enumerate(enrollment.bills.order_by("month")):
            month_date = services.add_months(start, offset)
            bill.month = services.month_key(month_date)
            bill.label = services.month_label(month_date)
            bill.due_date = due_date_for_month(bill.month)
            bill.status = (
                BillStatus.DUE if offset < unpaid_months else BillStatus.UPCOMING
            )
            bill.save()

    # -- output ------------------------------------------------------------

    def _report(self, created):
        self.stdout.write("What to look at:\n")
        for label, outcome, enrollment in created:
            enrollment.refresh_from_db()
            due = enrollment.outstanding_total()
            where = {
                "paid": "Due Payments -- should NOT be listed for a past month",
                "due": "Due Payments -> Monthly Dues (this month)",
                "auto": "Terminated Services -> reason 'Unpaid due'",
                "manual": "Terminated Services -> reason 'Stopped by manager'",
            }[outcome]

            self.stdout.write(
                f"  {enrollment.patient.name} ({enrollment.patient.patient_code})\n"
                f"    {label}\n"
                f"    status {enrollment.status}"
                + (f" / {enrollment.terminated_kind}" if enrollment.terminated_kind else "")
                + f", outstanding {due}\n"
                f"    -> {where}\n"
            )

        self.stdout.write(
            "Resume one of the terminated ones to see both options: paying the\n"
            "previous due gives one receipt per unpaid month, skipping it writes\n"
            "those months off. Either way billing restarts at the current month.\n"
        )

    # -- undo --------------------------------------------------------------

    def _undo(self, branch):
        """
        Remove the scenarios again.

        Scoped to the patients this command invented, and nothing else. The
        obvious shortcut — "delete the branch's monthly enrollments that have
        no payment against them" — would sweep up the clinic's own unpaid
        enrollments, which is precisely the data a test must never touch.
        Scenarios built on real patients are therefore reported rather than
        guessed at, with their ids, so the decision stays with a person.

        An enrollment that has taken a payment is left alone too: a receipt is
        financial history, and this project does not erase that to tidy up a
        test.
        """
        seeded = Patient.all_objects.filter(branch=branch, notes__contains=MARKER)
        removed = kept = 0

        for enrollment in MonthlyEnrollment.objects.filter(patient__in=seeded):
            if enrollment.bills.filter(payment__isnull=False).exists():
                kept += 1
                continue
            enrollment.delete()  # bills cascade
            removed += 1

        deleted_patients = 0
        for patient in seeded:
            if patient.monthly_enrollments.exists() or patient.installment_plans.exists():
                continue
            patient.hard_delete()
            deleted_patients += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Removed {removed} enrollment(s) and {deleted_patients} seeded patient(s)."
            )
        )
        if kept:
            self.stdout.write(
                self.style.WARNING(
                    f"Left {kept} enrollment(s) alone -- they have receipts against them, "
                    f"and a receipt is history rather than test litter."
                )
            )

        on_real_patients = MonthlyEnrollment.objects.filter(branch=branch).exclude(
            patient__in=seeded
        )
        if on_real_patients.exists():
            listing = ", ".join(
                f"#{e.id} ({e.patient.name})" for e in on_real_patients[:20]
            )
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"{on_real_patients.count()} monthly enrollment(s) sit on this "
                    f"branch's own patients and were NOT touched -- some may be "
                    f"scenarios from a seed run, some may be real. Check first:"
                )
            )
            self.stdout.write(f"  {listing}")
