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

from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.branches.models import Branch
from apps.enrollments import services
from apps.enrollments.models import (
    BillStatus,
    MonthlyBill,
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

# Each entry is one behaviour worth being able to see on screen, not a
# variation on the last one. `lapsed` is how many finished months went
# unpaid before the job ran; `paid_first` how many of those were settled
# on time; `kind` is what happens after that.
SCENARIOS = [
    # --- the service keeps running -------------------------------------
    {"label": "Paid up - the job must leave it alone", "kind": "paid"},
    {"label": "This month's due - collectable right now", "kind": "due"},

    # --- stopped for an unpaid due, with arrears to resume against ------
    {"label": "One month unpaid", "kind": "lapse", "lapsed": 1},
    {"label": "Two months unpaid", "kind": "lapse", "lapsed": 2},
    {"label": "Three months unpaid", "kind": "lapse", "lapsed": 3},
    {"label": "Six months unpaid - a long-abandoned service", "kind": "lapse", "lapsed": 6},
    {
        "label": "Paid two months, then stopped paying",
        "kind": "lapse", "lapsed": 3, "paid_first": 2,
    },
    {
        "label": "Part-paid the month it lapsed on - owes the remainder only",
        "kind": "lapse", "lapsed": 1, "part_pay": Decimal("0.40"),
    },

    # --- stopped by a person -------------------------------------------
    {"label": "Stopped by the manager - nothing left owing", "kind": "manual"},
    {
        "label": "Stopped by the manager after paying a month",
        "kind": "manual", "paid_first": 1,
    },

    # --- already been round the loop once -------------------------------
    {
        "label": "Lapsed, then resumed by paying the arrears",
        "kind": "resumed", "lapsed": 2, "carry_due": True,
    },
    {
        "label": "Lapsed, then resumed with the arrears skipped",
        "kind": "resumed", "lapsed": 2, "carry_due": False,
    },
    {
        "label": "Resumed once, then lapsed again",
        "kind": "resumed_then_lapsed", "lapsed": 4,
    },

    # --- shapes that catch a screen confusing one row for another -------
    {
        "label": "Second service for a patient who already has one running",
        "kind": "lapse", "lapsed": 1, "alongside_active": True,
    },
    {
        "label": "Lapsed on a fee of a different size",
        "kind": "lapse", "lapsed": 2, "fee_multiplier": 3,
    },
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
            for spec, patient in zip(SCENARIOS, patients):
                created.append(
                    self._build(
                        actor=manager, branch=branch, service=service,
                        patient=patient, spec=spec,
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
        The branch's own patients, cycled if there are fewer than scenarios.

        Cycling rather than inventing: a scenario carrying a real name and
        phone number is what the screen's search is actually used with, and
        one patient holding several monthly services is a shape the screens
        have to handle anyway -- it is one of the scenarios. A patient is
        only invented when the branch has none at all, which is the one case
        where there is nothing real to build on.

        Patients with no monthly service come first, so the earliest and most
        readable scenarios land on someone with an otherwise empty history.
        """
        free = list(
            Patient.objects.filter(branch=branch)
            .exclude(monthly_enrollments__isnull=False)
            .order_by("id")
        )
        rest = list(
            Patient.objects.filter(branch=branch)
            .exclude(pk__in=[p.pk for p in free])
            .order_by("id")
        )
        pool = free + rest

        if not pool:
            pool = [
                create_patient(
                    actor=manager,
                    branch=branch,
                    data={
                        "name": "Test Patient 1",
                        "phone": "01911000000",
                        "notes": MARKER,
                    },
                )
            ]
            self.stdout.write("Branch had no patients; invented one to build on.")
        else:
            self.stdout.write(
                f"Using {len(pool)} of the branch's own patient(s)"
                + (f", {len(free)} with no monthly service yet." if free else ".")
            )
            if needed > len(pool):
                self.stdout.write(
                    f"Cycling them to cover {needed} scenarios -- some patients will "
                    f"hold more than one service, which is itself one of the cases."
                )

        return [pool[i % len(pool)] for i in range(needed)]

    def _build(self, *, actor, branch, service, patient, spec):
        """
        Produce one scenario through the real code paths.

        Nothing is written straight into a status column: enrollments come
        from `create_monthly_enrollment`, money from `collect_bill_payment`,
        terminations from the actual nightly job, and resumes from
        `resume_monthly_service`. What ends up on screen is therefore what
        production does, not what a fixture asserted.
        """
        kind = spec["kind"]
        lapsed = spec.get("lapsed", 0)
        paid_first = spec.get("paid_first", 0)

        if spec.get("alongside_active"):
            # A second, healthy service for the same patient. The point is
            # that one patient can appear on both screens at once without
            # either row borrowing the other's status or arrears.
            services.create_monthly_enrollment(
                actor=actor, branch=branch, patient=patient, service=service
            )

        enrollment = services.create_monthly_enrollment(
            actor=actor, branch=branch, patient=patient, service=service
        )

        if spec.get("fee_multiplier"):
            # Charged at a different rate, so the arrears column has to hold
            # more than one number to be believed.
            enrollment.bills.update(amount=service.fee * spec["fee_multiplier"])

        if kind == "paid":
            self._settle(actor, branch, enrollment, months=1)
            return spec, enrollment
        if kind == "due":
            return spec, enrollment
        if kind == "manual":
            self._settle(actor, branch, enrollment, months=paid_first)
            services.terminate(actor=actor, container=enrollment)
            return spec, enrollment

        # Everything below lapses first.
        self._backdate(enrollment, lapsed)
        self._settle(actor, branch, enrollment, months=paid_first)

        if spec.get("part_pay"):
            self._part_pay(actor, branch, enrollment, share=spec["part_pay"])

        services.terminate_unpaid_monthly_services(actor=actor)
        enrollment.refresh_from_db()

        if kind == "resumed":
            services.resume_monthly_service(
                actor=actor, enrollment=enrollment,
                carry_due=spec["carry_due"],
                method="cash" if spec["carry_due"] else "",
            )
        elif kind == "resumed_then_lapsed":
            services.resume_monthly_service(
                actor=actor, enrollment=enrollment, carry_due=False
            )
            # Let the freshly opened cycle lapse as well, which is the case
            # that would break if a skipped month came back as owing.
            enrollment.refresh_from_db()
            self._backdate(enrollment, 1, only_after=True)
            services.terminate_unpaid_monthly_services(actor=actor)

        enrollment.refresh_from_db()
        return spec, enrollment

    def _settle(self, actor, branch, enrollment, *, months):
        """Pay the oldest `months` bills, oldest-first, as the desk would."""
        for _ in range(months):
            bill = enrollment.oldest_unpaid_bill()
            if bill is None:
                return
            services.collect_bill_payment(
                actor=actor, branch=branch, bill=bill, method="cash"
            )

    def _part_pay(self, actor, branch, enrollment, *, share):
        """
        Leave a bill part-settled.

        Written straight onto `amount_paid` rather than through a payment,
        because a monthly bill is all-or-nothing at the desk -- the partial
        state this reproduces comes from a refund against a settled bill, and
        the point here is only that the arrears figure is `amount - paid`
        rather than the headline amount.
        """
        bill = enrollment.oldest_unpaid_bill()
        if bill is None:
            return
        bill.amount_paid = (bill.amount * share).quantize(Decimal("0.01"))
        bill.save(update_fields=["amount_paid"])

    def _backdate(self, enrollment, months, *, only_after=False):
        """
        Rewind the billing so `months` finished months went unpaid.

        Backdated rather than the clock being wound forward, because that is
        what production looks like: real time passed, the patient didn't pay,
        and the nightly job then finds a month that is genuinely over. Each
        bill gets its own month -- (enrollment, month) is unique, so stacking
        them would fail on the constraint instead of producing the history
        being described.

        `only_after` moves just the bills a resume opened, leaving the
        settled or written-off history before it exactly where it is -- and
        starts them the month after that history ends rather than counting
        back from today, which would drop them on top of a month the
        enrollment already owns and fail the constraint.
        """
        bills = enrollment.bills.order_by("month")

        if only_after:
            movable = bills.filter(
                status__in=[BillStatus.DUE, BillStatus.UPCOMING],
                amount_paid=Decimal("0.00"),
            )
            history = bills.exclude(pk__in=[b.pk for b in movable]).order_by("-month").first()
            if history is None:
                start = services.add_months(timezone.localdate().replace(day=1), -months)
            else:
                year, month = (int(part) for part in history.month.split("-"))
                start = services.add_months(date(year, month, 1), 1)
            bills = movable
        else:
            start = services.add_months(timezone.localdate().replace(day=1), -months)
            # An enrollment opens with a three-month lookahead, so a longer
            # lapse has to grow the series -- otherwise "six months unpaid"
            # would quietly be three, and the arrears figure on screen would
            # contradict the label next to it.
            for offset in range(bills.count(), months):
                month_date = services.add_months(start, offset)
                key = services.month_key(month_date)
                MonthlyBill.objects.get_or_create(
                    enrollment=enrollment,
                    month=key,
                    defaults={
                        "label": services.month_label(month_date),
                        "amount": enrollment.bills.first().amount,
                        "due_date": due_date_for_month(key),
                        "status": BillStatus.DUE,
                    },
                )
            bills = enrollment.bills.order_by("month")

        for offset, bill in enumerate(bills):
            month_date = services.add_months(start, offset)
            bill.month = services.month_key(month_date)
            bill.label = services.month_label(month_date)
            bill.due_date = due_date_for_month(bill.month)
            bill.status = BillStatus.DUE if offset < months else BillStatus.UPCOMING
            bill.save()

    # -- output ------------------------------------------------------------

    def _report(self, created):
        """
        Where each scenario lands, derived from the row itself rather than
        from what the spec intended -- a report that echoes the plan back
        would still look right if the code had put the row somewhere else.
        """
        self.stdout.write("What to look at:")
        self.stdout.write("")

        for spec, enrollment in created:
            enrollment.refresh_from_db()
            payable = enrollment.oldest_unpaid_bill()

            if enrollment.status == EnrollmentStatus.TERMINATED:
                reason = (
                    "'Unpaid due'"
                    if enrollment.terminated_kind
                    == MonthlyEnrollment.TerminationKind.UNPAID_DUE
                    else "'Stopped by manager'"
                )
                where = f"Terminated Services -> reason {reason}"
                owed = f"previous due {enrollment.outstanding_total()}"
            else:
                where = (
                    f"Due Payments -> Monthly Dues ({payable.month})"
                    if payable
                    else "nothing payable"
                )
                owed = f"payable now {payable.outstanding if payable else 0}"

            self.stdout.write(
                f"  #{enrollment.id} {enrollment.patient.name} "
                f"({enrollment.patient.patient_code})"
            )
            self.stdout.write(f"     {spec['label']}")
            self.stdout.write(f"     {enrollment.status}, {owed}")
            self.stdout.write(f"     -> {where}")
            self.stdout.write("")

        self.stdout.write(
            "Resume a terminated one to see both options: paying the previous due"
        )
        self.stdout.write(
            "gives one receipt per unpaid month, skipping it writes those months"
        )
        self.stdout.write("off. Either way billing restarts at the current month.")

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
