"""
Reminds every patient with a confirmed online booking tomorrow.

Run via system cron, once daily:

    0 18 * * * /path/to/.venv/bin/python manage.py send_appointment_reminders
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.enrollments.models import Booking
from apps.notifications.services import send_email_notification, send_sms


class Command(BaseCommand):
    help = "Sends an SMS/email reminder for every confirmed booking scheduled for tomorrow."

    def handle(self, *args, **options):
        tomorrow = timezone.localdate() + timedelta(days=1)
        bookings = Booking.objects.filter(
            date=tomorrow, status=Booking.Status.CONFIRMED
        ).select_related("patient", "service")

        if not bookings:
            self.stdout.write("No appointments scheduled for tomorrow.")
            return

        sms_sent = email_sent = 0
        for booking in bookings:
            patient = booking.patient
            message = (
                f"Dear {patient.name}, this is a reminder that your {booking.service.name} "
                f"session is scheduled for tomorrow ({booking.date}) at {booking.time}. "
                f"- Speech Therapy Lab"
            )

            if send_sms(patient.phone, message):
                sms_sent += 1
            if patient.email and send_email_notification(
                patient.email, "Appointment reminder — Speech Therapy Lab", message
            ):
                email_sent += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Processed {bookings.count()} appointment(s): {sms_sent} SMS, {email_sent} email sent."
            )
        )
