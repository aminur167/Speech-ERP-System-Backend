"""
Enquiries — people who rang after seeing an ad, before they are patients.

The clinic advertises daily; the calls that follow are from prospective
patients or their guardians, none of whom are registered. This records them
so the desk can follow them up and the client can see which ads actually
produce enrolments.

**Deliberately not a `Patient` row.** Walk-in buyers became patients because
every `Payment` needs one (docs/02); an enquiry involves no money and no
service, so nothing forces it into that table — and putting it there would
mean every patient list, directory and dashboard count had to learn to
exclude a second kind of non-patient. A separate table keeps the patient
roster meaning exactly one thing.

The link runs the other way instead: `converted_patient` is set when someone
who rang actually comes in and registers, which is what turns "40 calls from
Facebook" into "40 calls, 12 enrolled".
"""

from django.db import models

from apps.common.models import SoftDeleteModel, TimeStampedModel
from apps.common.validators import normalize_phone, validate_bd_phone


class Enquiry(TimeStampedModel, SoftDeleteModel):
    class Relation(models.TextChoices):
        SELF = "self", "The patient themselves"
        FATHER = "father", "Father"
        MOTHER = "mother", "Mother"
        GUARDIAN = "guardian", "Guardian"
        OTHER = "other", "Other"

    class Source(models.TextChoices):
        """
        Where they heard about the clinic — the whole point of the feature.

        A fixed set so the question "which ad is working?" can be answered by
        counting. `source_detail` beside it carries the specifics (which
        campaign, whose referral) that a dropdown cannot.
        """

        FACEBOOK = "facebook", "Facebook"
        GOOGLE = "google", "Google"
        POSTER = "poster", "Poster / leaflet"
        REFERRAL = "referral", "Referred by someone"
        SIGNBOARD = "signboard", "Signboard / passing by"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        """
        Where this person is in the funnel — distinct from `follow_up_on`,
        which is *when to act*. One says who they are, the other says what
        today's work is, and the screens filter on them separately.
        """

        NEW = "new", "New"
        CONTACTED = "contacted", "Contacted"
        VISIT_PLANNED = "visit_planned", "Coming in"
        VISITED = "visited", "Visited"
        CONVERTED = "converted", "Enrolled"
        NOT_INTERESTED = "not_interested", "Not interested"
        UNREACHABLE = "unreachable", "Unreachable"

    #: Statuses where nobody is waiting on a call back.
    CLOSED_STATUSES = frozenset({Status.CONVERTED, Status.NOT_INTERESTED})

    # Per-branch series (ENQ-DHK-2026-00001), the same shape and the same
    # race-safe sequence every other code in the system uses.
    enquiry_code = models.CharField(max_length=32, unique=True, db_index=True)

    caller_name = models.CharField(max_length=150, db_index=True)
    # The one field that matters most: it is how the desk rings back, and how
    # a repeat caller is recognised. Normalised on save so a number typed in
    # any format still matches a search.
    caller_phone = models.CharField(
        max_length=20, db_index=True, validators=[validate_bd_phone]
    )
    caller_relation = models.CharField(
        max_length=16, choices=Relation.choices, default=Relation.SELF
    )

    # Who the therapy would be for. Blank when the caller is the patient --
    # not duplicated into this column, so "whose name do we have" stays one
    # question with one answer.
    patient_name = models.CharField(max_length=150, blank=True, db_index=True)
    # Whatever age they gave on the phone. Deliberately an integer rather
    # than a birth date: nobody reads out a date of birth to a receptionist,
    # and a wrong date would look authoritative later.
    patient_age = models.PositiveSmallIntegerField(null=True, blank=True)

    concern = models.TextField(blank=True)

    source = models.CharField(max_length=16, choices=Source.choices, db_index=True)
    source_detail = models.CharField(max_length=150, blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.NEW, db_index=True
    )
    # When to ring back. Null means nobody has decided yet, which the screens
    # show as its own filter rather than folding into "no follow-up needed".
    follow_up_on = models.DateField(null=True, blank=True, db_index=True)

    notes = models.TextField(blank=True)

    # Set when this caller actually registers. PROTECT, not SET_NULL: losing
    # the link would quietly turn a converted enquiry back into an
    # unconverted one and understate every ad's return.
    converted_patient = models.OneToOneField(
        "patients.Patient",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="enquiry",
    )

    branch = models.ForeignKey(
        "branches.Branch", on_delete=models.PROTECT, related_name="enquiries"
    )
    taken_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="enquiries_taken",
    )
    # Editable, and not the same as `created_at`: a call taken at the desk
    # often gets typed in later, and the ad report should count when the
    # phone rang rather than when someone found a free minute.
    called_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-called_at"]
        verbose_name_plural = "enquiries"
        indexes = [
            models.Index(fields=["branch", "-called_at"]),
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["branch", "follow_up_on"]),
            models.Index(fields=["source"]),
            models.Index(fields=["caller_phone"]),
        ]

    def __str__(self):
        return f"{self.caller_name} ({self.enquiry_code})"

    def save(self, *args, **kwargs):
        if self.caller_phone:
            self.caller_phone = normalize_phone(self.caller_phone)
        super().save(*args, **kwargs)

    @property
    def is_converted(self) -> bool:
        return self.converted_patient_id is not None

    def is_follow_up_overdue(self, *, on=None) -> bool:
        """
        A call back that was due and has not happened.

        Derived, never stored: a stored flag goes stale the moment a date
        passes with nothing writing to the row. Closed enquiries are never
        overdue — nobody is waiting on someone who enrolled or said no.
        """
        from django.utils import timezone

        if self.follow_up_on is None or self.status in self.CLOSED_STATUSES:
            return False
        return (on or timezone.localdate()) > self.follow_up_on
