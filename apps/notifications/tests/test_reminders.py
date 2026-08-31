"""
Reminders — SMS/WhatsApp/email senders and the two daily reminder commands.

Twilio is never configured in test settings (TWILIO_ACCOUNT_SID is blank by
default), so send_sms/send_whatsapp exercise their real no-op path here
rather than needing a mocked HTTP call -- that's the same safety property
production relies on before real credentials are set.
"""

from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.enrollments.models import Booking
from apps.notifications.services import send_email_notification, send_sms, send_whatsapp
from apps.services.models import Service

pytestmark = pytest.mark.django_db


class TestSenders:
    def test_send_sms_no_ops_without_twilio_configured(self):
        assert send_sms("+8801700000000", "test") is False

    def test_send_whatsapp_no_ops_without_twilio_configured(self):
        assert send_whatsapp("+8801700000000", "test") is False

    def test_send_sms_no_ops_for_a_blank_number(self):
        with override_settings(TWILIO_ACCOUNT_SID="x", TWILIO_AUTH_TOKEN="y", TWILIO_SMS_FROM="z"):
            assert send_sms("", "test") is False

    def test_send_email_notification_delivers_via_the_configured_backend(self):
        sent = send_email_notification("patient@example.com", "Subject", "Body text")

        assert sent is True
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["patient@example.com"]
        assert mail.outbox[0].subject == "Subject"

    def test_send_email_notification_no_ops_for_a_blank_address(self):
        assert send_email_notification("", "Subject", "Body") is False
        assert len(mail.outbox) == 0


class TestDuePaymentReminders:
    def test_reminds_for_every_outstanding_due_item(
        self, branch, manager, patient_factory, service_factory
    ):
        from apps.enrollments.services import create_monthly_enrollment

        patient = patient_factory(email="parent@example.com")
        service = service_factory(category=Service.Category.MONTHLY, fee=Decimal("3000.00"))
        create_monthly_enrollment(actor=manager, branch=branch, patient=patient, service=service)

        out = StringIO()
        with patch("apps.notifications.management.commands.send_due_payment_reminders.send_sms") as mock_sms:
            mock_sms.return_value = True
            call_command("send_due_payment_reminders", stdout=out)

        assert mock_sms.call_count == 1
        assert mock_sms.call_args.args[0] == patient.phone
        assert len(mail.outbox) == 1
        assert "outstanding due" in mail.outbox[0].body

    def test_no_due_items_sends_nothing(self):
        out = StringIO()
        call_command("send_due_payment_reminders", stdout=out)

        assert "No outstanding" in out.getvalue()
        assert len(mail.outbox) == 0


class TestAppointmentReminders:
    def test_reminds_for_a_booking_scheduled_tomorrow(self, branch, patient_factory, service_factory):
        patient = patient_factory(email="parent@example.com")
        service = service_factory(category=Service.Category.ONLINE, fee=Decimal("2000.00"))
        Booking.objects.create(
            booking_code="BKG-TEST-00001",
            patient=patient,
            service=service,
            branch=branch,
            date=timezone.localdate() + timedelta(days=1),
            time="14:30",
            advance_amount=Decimal("1000.00"),
            status=Booking.Status.CONFIRMED,
        )

        out = StringIO()
        with patch(
            "apps.notifications.management.commands.send_appointment_reminders.send_sms"
        ) as mock_sms:
            mock_sms.return_value = True
            call_command("send_appointment_reminders", stdout=out)

        assert mock_sms.call_count == 1
        assert len(mail.outbox) == 1
        assert "tomorrow" in mail.outbox[0].body

    def test_does_not_remind_for_a_booking_further_out(self, branch, patient_factory, service_factory):
        patient = patient_factory()
        service = service_factory(category=Service.Category.ONLINE, fee=Decimal("2000.00"))
        Booking.objects.create(
            booking_code="BKG-TEST-00002",
            patient=patient,
            service=service,
            branch=branch,
            date=timezone.localdate() + timedelta(days=5),
            time="14:30",
            advance_amount=Decimal("1000.00"),
            status=Booking.Status.CONFIRMED,
        )

        out = StringIO()
        call_command("send_appointment_reminders", stdout=out)

        assert "No appointments" in out.getvalue()

    def test_does_not_remind_for_a_cancelled_booking(self, branch, patient_factory, service_factory):
        patient = patient_factory()
        service = service_factory(category=Service.Category.ONLINE, fee=Decimal("2000.00"))
        Booking.objects.create(
            booking_code="BKG-TEST-00003",
            patient=patient,
            service=service,
            branch=branch,
            date=timezone.localdate() + timedelta(days=1),
            time="14:30",
            advance_amount=Decimal("1000.00"),
            status=Booking.Status.CANCELLED,
        )

        out = StringIO()
        call_command("send_appointment_reminders", stdout=out)

        assert "No appointments" in out.getvalue()
