"""
Daily reminder for every currently outstanding due payment.

Run via system cron (docs/DEPLOYMENT.md's pattern for background jobs — see
also the monthly-bill job runner in the same style):

    0 9 * * * /path/to/.venv/bin/python manage.py send_due_payment_reminders

Reuses apps.duepayments.services.collect_due_items(), the same source of
truth the Due Payments screen and the collection flow already use -- so a
reminder is sent for exactly the item a manager would actually see and be
able to collect, never something already paid or not yet due under the
oldest-first rule.
"""

from django.core.management.base import BaseCommand

from apps.duepayments.services import collect_due_items
from apps.notifications.services import send_email_notification, send_sms
from apps.patients.models import Patient


class Command(BaseCommand):
    help = "Sends an SMS/email reminder for every currently outstanding due payment."

    def handle(self, *args, **options):
        items = collect_due_items()
        if not items:
            self.stdout.write("No outstanding due payments.")
            return

        patients = Patient.objects.in_bulk([item["patientId"] for item in items])

        sms_sent = email_sent = 0
        for item in items:
            patient = patients.get(int(item["patientId"]))
            if patient is None:
                continue

            message = (
                f"Dear {item['patientName']}, you have an outstanding due of "
                f"BDT {item['amount']} for {item['label']}. Please settle it at "
                f"your earliest convenience. - Speech Therapy Lab"
            )

            if send_sms(patient.phone, message):
                sms_sent += 1
            if patient.email and send_email_notification(
                patient.email, "Payment reminder — Speech Therapy Lab", message
            ):
                email_sent += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Processed {len(items)} due item(s): {sms_sent} SMS, {email_sent} email sent."
            )
        )
