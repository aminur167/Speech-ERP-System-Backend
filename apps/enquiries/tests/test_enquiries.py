"""
Enquiries — calls that came in off an ad.

What these guard, in the order they would do damage if wrong:

1. **An enquiry is not a patient.** The whole reason for a separate table is
   that the patient roster keeps meaning one thing; a test pins that a
   recorded call changes nothing about it.
2. **Branch isolation**, as everywhere — one branch's calls must be
   invisible to another branch's manager, on reads and on every action.
3. **Conversion links, and links once.** The link is what turns "40 calls
   from Facebook" into "12 enrolled"; a second conversion would double-count
   the ad that produced the caller.
4. **The two filters do different jobs.** `status` says where someone is in
   the funnel, `followUp` says whether today's work includes them, and
   conflating them is how a converted patient ends up on a call-back list.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.models import AuditLog
from apps.enquiries.models import Enquiry
from apps.patients.models import Patient

pytestmark = pytest.mark.django_db

LIST_URL = reverse("enquiries:enquiry-list")
SUMMARY_URL = reverse("enquiries:enquiry-summary")


def payload(**overrides):
    body = {
        "caller_name": "Shirin Akter",
        "caller_phone": "01712345678",
        "caller_relation": "mother",
        "patient_name": "Rafi",
        "patient_age": 4,
        "concern": "Not speaking clearly",
        "source": "facebook",
        "source_detail": "October video ad",
    }
    body.update(overrides)
    return body


@pytest.fixture
def enquiry_factory(db, branch, manager):
    from apps.enquiries import services

    def _create(**overrides):
        data = payload(**overrides)
        return services.create_enquiry(actor=manager, branch=branch, data=data)

    return _create


class TestRecordingACall:
    def test_a_manager_can_record_one(self, manager_client, branch):
        response = manager_client.post(LIST_URL, payload(), format="json")

        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["callerName"] == "Shirin Akter"
        assert body["source"] == "facebook"
        assert body["sourceDetail"] == "October video ad"
        assert body["status"] == "new"
        assert body["branchName"] == branch.name
        assert body["enquiryCode"].startswith("ENQ-DHK-")

    def test_only_a_name_phone_and_source_are_required(self, manager_client):
        response = manager_client.post(
            LIST_URL,
            {"caller_name": "Rahim", "caller_phone": "01712345678", "source": "poster"},
            format="json",
        )

        assert response.status_code == 201, response.json()

    @pytest.mark.parametrize("field", ["caller_name", "caller_phone", "source"])
    def test_the_three_required_fields_are_enforced(self, manager_client, field):
        body = payload()
        del body[field]

        response = manager_client.post(LIST_URL, body, format="json")

        assert response.status_code == 400
        assert field in response.json()

    def test_a_bad_phone_is_refused(self, manager_client):
        response = manager_client.post(
            LIST_URL, payload(caller_phone="12345"), format="json"
        )

        assert response.status_code == 400
        assert "caller_phone" in response.json()

    def test_the_phone_is_normalised(self, manager_client):
        body = manager_client.post(
            LIST_URL, payload(caller_phone="+8801712345678"), format="json"
        ).json()

        assert body["callerPhone"] == "01712345678"

    def test_a_call_cannot_be_in_the_future(self, manager_client):
        later = (timezone.now() + timedelta(days=1)).isoformat()

        response = manager_client.post(
            LIST_URL, payload(called_at=later), format="json"
        )

        assert response.status_code == 400
        assert "called_at" in response.json()

    def test_who_took_it_comes_from_the_token(self, manager_client, manager):
        body = manager_client.post(LIST_URL, payload(), format="json").json()

        assert body["takenBy"] == manager.name

    def test_recording_is_audited(self, manager_client):
        body = manager_client.post(LIST_URL, payload(), format="json").json()

        entry = AuditLog.objects.get(
            target_type="Enquiry", target_id=str(body["id"]),
            action=AuditLog.Action.CREATE,
        )
        assert entry.changes["source"] == "facebook"

    def test_admin_cannot_record_one(self, admin_client):
        """Admin has no branch to file a call under — the same rule as patients."""
        response = admin_client.post(LIST_URL, payload(), format="json")

        assert response.status_code == 403


class TestAnEnquiryIsNotAPatient:
    def test_recording_a_call_creates_no_patient(self, manager_client):
        manager_client.post(LIST_URL, payload(), format="json")

        assert not Patient.objects.exists()

    def test_the_patient_list_is_untouched(self, manager_client, patient_factory):
        patient_factory(name="Rahim Ahmed")
        manager_client.post(LIST_URL, payload(), format="json")

        body = manager_client.get(reverse("patients:patient-list")).json()

        assert [row["name"] for row in body["results"]] == ["Rahim Ahmed"]


class TestConversion:
    def registration(self, **overrides):
        body = {
            "name": "Rafi Islam",
            "phone": "01712345678",
            "date_of_birth": "2021-05-10",
            "gender": "male",
            "address": "Dhanmondi, Dhaka",
            "guardian_name": "Shirin Akter",
            "guardian_relation": "mother",
            "guardian_phone": "01712345678",
        }
        body.update(overrides)
        return body

    def convert_url(self, enquiry):
        return reverse("enquiries:enquiry-convert", args=[enquiry.pk])

    def test_converting_registers_the_patient_and_links_them(
        self, manager_client, enquiry_factory
    ):
        enquiry = enquiry_factory()

        response = manager_client.post(
            self.convert_url(enquiry), self.registration(), format="json"
        )

        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["patient"]["name"] == "Rafi Islam"
        assert body["enquiry"]["status"] == "converted"
        assert body["enquiry"]["convertedPatientCode"] == body["patient"]["patientCode"]

        enquiry.refresh_from_db()
        assert enquiry.converted_patient_id == int(body["patient"]["id"])

    def test_converting_clears_the_follow_up(self, manager_client, enquiry_factory):
        """Nobody is waiting on a call back once they have enrolled."""
        enquiry = enquiry_factory(follow_up_on=timezone.localdate())

        manager_client.post(
            self.convert_url(enquiry), self.registration(), format="json"
        )

        enquiry.refresh_from_db()
        assert enquiry.follow_up_on is None

    def test_converting_twice_is_refused(self, manager_client, enquiry_factory):
        """A second patient row would double-count the ad that brought them."""
        enquiry = enquiry_factory()
        manager_client.post(
            self.convert_url(enquiry), self.registration(), format="json"
        )

        response = manager_client.post(
            self.convert_url(enquiry),
            self.registration(phone="01799887766"),
            format="json",
        )

        assert response.status_code == 400
        assert response.json()["code"] == "already_converted"
        assert Patient.objects.count() == 1

    def test_the_registration_rules_still_apply(self, manager_client, enquiry_factory):
        """A caller is not a shortcut past the guardian rule for a minor."""
        enquiry = enquiry_factory()
        body = self.registration()
        del body["guardian_name"]

        response = manager_client.post(self.convert_url(enquiry), body, format="json")

        assert response.status_code == 400
        assert "guardian_name" in response.json()
        enquiry.refresh_from_db()
        assert not enquiry.is_converted

    def test_a_converted_enquiry_still_names_its_source(
        self, manager_client, enquiry_factory
    ):
        """The point of the link: which ad produced this enrolment."""
        enquiry = enquiry_factory(source="google", source_detail="Search ad")
        manager_client.post(
            self.convert_url(enquiry), self.registration(), format="json"
        )

        body = manager_client.get(
            reverse("enquiries:enquiry-detail", args=[enquiry.pk])
        ).json()

        assert body["source"] == "google"
        assert body["status"] == "converted"


class TestFilters:
    def test_search_matches_the_caller(self, manager_client, enquiry_factory):
        enquiry_factory(caller_name="Shirin Akter")
        enquiry_factory(caller_name="Kamal Hossain", caller_phone="01811111111")

        results = manager_client.get(LIST_URL, {"search": "Shirin"}).json()["results"]

        assert [row["callerName"] for row in results] == ["Shirin Akter"]

    def test_search_matches_a_phone_typed_in_another_format(
        self, manager_client, enquiry_factory
    ):
        enquiry_factory()

        results = manager_client.get(
            LIST_URL, {"search": "+8801712345678"}
        ).json()["results"]

        assert len(results) == 1

    def test_search_matches_the_child_and_the_concern(
        self, manager_client, enquiry_factory
    ):
        enquiry_factory(patient_name="Rafi", concern="Not speaking clearly")
        enquiry_factory(
            caller_name="Other", caller_phone="01811111111",
            patient_name="Mim", concern="Stammering",
        )

        by_child = manager_client.get(LIST_URL, {"search": "Mim"}).json()["results"]
        by_concern = manager_client.get(
            LIST_URL, {"search": "Stammer"}
        ).json()["results"]

        assert [row["patientName"] for row in by_child] == ["Mim"]
        assert [row["patientName"] for row in by_concern] == ["Mim"]

    def test_source_narrows_the_list(self, manager_client, enquiry_factory):
        enquiry_factory(source="facebook")
        enquiry_factory(source="poster", caller_phone="01811111111")

        results = manager_client.get(LIST_URL, {"source": "poster"}).json()["results"]

        assert [row["source"] for row in results] == ["poster"]

    def test_status_narrows_the_list(self, manager_client, enquiry_factory):
        enquiry_factory(status="new")
        enquiry_factory(status="contacted", caller_phone="01811111111")

        results = manager_client.get(LIST_URL, {"status": "contacted"}).json()["results"]

        assert [row["status"] for row in results] == ["contacted"]

    def test_the_date_range_filters_on_when_the_phone_rang(
        self, manager_client, enquiry_factory
    ):
        old = enquiry_factory()
        Enquiry.objects.filter(pk=old.pk).update(
            called_at=timezone.now() - timedelta(days=10)
        )
        enquiry_factory(caller_phone="01811111111")

        today = timezone.localdate().isoformat()
        body = manager_client.get(LIST_URL, {"dateFrom": today}).json()

        assert body["count"] == 1


class TestFollowUpIsNotStatus:
    def test_due_today(self, manager_client, enquiry_factory):
        enquiry_factory(follow_up_on=timezone.localdate())
        enquiry_factory(
            caller_phone="01811111111",
            follow_up_on=timezone.localdate() + timedelta(days=3),
        )

        body = manager_client.get(LIST_URL, {"followUp": "today"}).json()

        assert body["count"] == 1

    def test_overdue(self, manager_client, enquiry_factory):
        enquiry_factory(follow_up_on=timezone.localdate() - timedelta(days=2))
        enquiry_factory(caller_phone="01811111111", follow_up_on=timezone.localdate())

        body = manager_client.get(LIST_URL, {"followUp": "overdue"}).json()

        assert body["count"] == 1
        assert body["results"][0]["followUpOverdue"] is True

    def test_upcoming(self, manager_client, enquiry_factory):
        enquiry_factory(follow_up_on=timezone.localdate() + timedelta(days=3))
        enquiry_factory(caller_phone="01811111111", follow_up_on=timezone.localdate())

        body = manager_client.get(LIST_URL, {"followUp": "upcoming"}).json()

        assert body["count"] == 1

    def test_none_set(self, manager_client, enquiry_factory):
        enquiry_factory()
        enquiry_factory(caller_phone="01811111111", follow_up_on=timezone.localdate())

        body = manager_client.get(LIST_URL, {"followUp": "none"}).json()

        assert body["count"] == 1
        assert body["results"][0]["followUpOn"] is None

    def test_a_closed_enquiry_is_never_outstanding_work(
        self, manager_client, enquiry_factory
    ):
        """
        Someone who said no is not a call to make, however old their
        follow-up date is. This is why `status` and `followUp` are separate
        filters rather than one.
        """
        enquiry_factory(
            status="not_interested",
            follow_up_on=timezone.localdate() - timedelta(days=5),
        )

        body = manager_client.get(LIST_URL, {"followUp": "overdue"}).json()

        assert body["count"] == 0

    def test_overdue_is_derived_not_stored(self, manager_client, enquiry_factory):
        """No job runs; the flag is computed when it is read."""
        enquiry = enquiry_factory(follow_up_on=timezone.localdate() - timedelta(days=1))

        body = manager_client.get(
            reverse("enquiries:enquiry-detail", args=[enquiry.pk])
        ).json()

        assert body["followUpOverdue"] is True


class TestSummary:
    def test_the_header_counts_what_the_filters_left(
        self, manager_client, enquiry_factory
    ):
        enquiry_factory(source="facebook")
        enquiry_factory(source="facebook", caller_phone="01811111111")
        enquiry_factory(source="poster", caller_phone="01822222222")

        everything = manager_client.get(SUMMARY_URL).json()
        just_poster = manager_client.get(SUMMARY_URL, {"source": "poster"}).json()

        assert everything["total"] == 3
        assert everything["topSource"] == "facebook"
        assert everything["topSourceCount"] == 2
        assert just_poster["total"] == 1

    def test_it_counts_follow_ups_and_conversions(
        self, manager_client, enquiry_factory
    ):
        enquiry_factory(follow_up_on=timezone.localdate())
        enquiry_factory(
            caller_phone="01811111111",
            follow_up_on=timezone.localdate() - timedelta(days=1),
        )
        enquiry_factory(caller_phone="01822222222", status="converted")

        body = manager_client.get(SUMMARY_URL).json()

        assert body["dueToday"] == 1
        assert body["overdue"] == 1
        assert body["converted"] == 1


@pytest.mark.isolation
class TestBranchIsolation:
    def test_another_branch_cannot_see_it(
        self, other_manager_client, enquiry_factory
    ):
        enquiry_factory()

        assert other_manager_client.get(LIST_URL).json()["count"] == 0

    def test_another_branch_cannot_read_it_by_id(
        self, other_manager_client, enquiry_factory
    ):
        enquiry = enquiry_factory()

        response = other_manager_client.get(
            reverse("enquiries:enquiry-detail", args=[enquiry.pk])
        )

        assert response.status_code == 404

    def test_another_branch_cannot_edit_it(
        self, other_manager_client, enquiry_factory
    ):
        enquiry = enquiry_factory()

        response = other_manager_client.patch(
            reverse("enquiries:enquiry-detail", args=[enquiry.pk]),
            {"status": "contacted"},
            format="json",
        )

        assert response.status_code == 404
        enquiry.refresh_from_db()
        assert enquiry.status == "new"

    def test_another_branch_cannot_convert_it(
        self, other_manager_client, enquiry_factory
    ):
        enquiry = enquiry_factory()

        response = other_manager_client.post(
            reverse("enquiries:enquiry-convert", args=[enquiry.pk]),
            {
                "name": "Rafi", "phone": "01712345678",
                "date_of_birth": "1990-01-01", "gender": "male", "address": "x",
            },
            format="json",
        )

        assert response.status_code == 404
        assert not Patient.objects.exists()

    def test_admin_sees_every_branch_and_can_narrow(
        self, admin_client, enquiry_factory, other_branch, other_manager
    ):
        from apps.enquiries import services

        enquiry_factory()
        services.create_enquiry(
            actor=other_manager, branch=other_branch,
            data=payload(caller_name="Chattogram Caller", caller_phone="01899999999"),
        )

        everything = admin_client.get(LIST_URL).json()
        narrowed = admin_client.get(LIST_URL, {"branch": other_branch.id}).json()

        assert everything["count"] == 2
        assert [row["callerName"] for row in narrowed["results"]] == [
            "Chattogram Caller"
        ]


class TestEditing:
    def test_a_status_change_is_audited(self, manager_client, enquiry_factory):
        enquiry = enquiry_factory()

        manager_client.patch(
            reverse("enquiries:enquiry-detail", args=[enquiry.pk]),
            {"status": "contacted", "follow_up_on": str(timezone.localdate())},
            format="json",
        )

        entry = AuditLog.objects.filter(
            target_type="Enquiry", target_id=str(enquiry.pk),
            action=AuditLog.Action.UPDATE,
        ).first()
        assert entry is not None
        assert entry.changes["status"] == {"from": "new", "to": "contacted"}

    def test_deleting_hides_it_without_destroying_it(
        self, manager_client, enquiry_factory
    ):
        enquiry = enquiry_factory()

        response = manager_client.delete(
            reverse("enquiries:enquiry-detail", args=[enquiry.pk])
        )

        assert response.status_code == 204
        assert manager_client.get(LIST_URL).json()["count"] == 0
        assert Enquiry.all_objects.filter(pk=enquiry.pk, is_deleted=True).exists()
