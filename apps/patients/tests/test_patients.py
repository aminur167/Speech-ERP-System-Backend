"""
Patient tests.

Organised around the doc's required-test list. The highest-value groups here
are the age-conditional guardian rule, branch isolation, and the per-branch
code series — the three places where a subtle bug does real damage.
"""

import threading
from datetime import date, timedelta

import pytest
from django.db import connection
from django.urls import reverse

from apps.common.models import AuditLog
from apps.patients.models import Patient, calculate_age
from apps.patients.services import create_patient

pytestmark = pytest.mark.django_db


def years_ago(years: int, *, days: int = 0) -> str:
    """A birth date exactly `years` old today, optionally nudged by days."""
    today = date.today()
    try:
        born = today.replace(year=today.year - years)
    except ValueError:  # 29 Feb
        born = today.replace(year=today.year - years, day=28)
    return (born + timedelta(days=days)).isoformat()


def patient_payload(**overrides):
    """An adult patient — no guardian needed."""
    payload = {
        "name": "Arif Hossain",
        "date_of_birth": years_ago(31),
        "gender": "male",
        "phone": "01711000003",
        "address": "Uttara, Dhaka",
    }
    payload.update(overrides)
    return payload


def minor_payload(**overrides):
    """A child — guardian fields required."""
    payload = {
        "name": "Rafiul Islam",
        "date_of_birth": years_ago(8),
        "gender": "male",
        "phone": "01711000001",
        "address": "Dhanmondi, Dhaka",
        "guardian_name": "Kamal Islam",
        "guardian_relation": "father",
        "guardian_phone": "01711000002",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# CRUD and codes
# ---------------------------------------------------------------------------


class TestPatientCreation:
    def test_manager_registers_a_patient(self, manager_client, branch):
        response = manager_client.post(reverse("patients:patient-list"), patient_payload())

        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Arif Hossain"
        assert body["branchId"] == str(branch.id)

    def test_patient_code_uses_branch_prefixed_series(self, manager_client, branch):
        response = manager_client.post(reverse("patients:patient-list"), patient_payload())

        code = response.json()["patientCode"]
        assert code == f"PT-{branch.short_code}-{date.today().year}-00001"

    def test_codes_increment_within_a_branch(self, manager_client):
        first = manager_client.post(
            reverse("patients:patient-list"), patient_payload()
        ).json()["patientCode"]
        second = manager_client.post(
            reverse("patients:patient-list"), patient_payload(phone="01711000004")
        ).json()["patientCode"]

        assert first.endswith("00001")
        assert second.endswith("00002")

    @pytest.mark.isolation
    def test_each_branch_has_its_own_series(
        self, manager_client, other_manager_client, branch, other_branch
    ):
        """
        Both branches start at 00001 and neither advances the other's counter —
        this is what lets a branch issue codes offline.
        """
        dhaka = manager_client.post(
            reverse("patients:patient-list"), patient_payload()
        ).json()["patientCode"]
        ctg = other_manager_client.post(
            reverse("patients:patient-list"), patient_payload(phone="01811000001")
        ).json()["patientCode"]

        assert dhaka == f"PT-DHK-{date.today().year}-00001"
        assert ctg == f"PT-CTG-{date.today().year}-00001"

        dhaka_second = manager_client.post(
            reverse("patients:patient-list"), patient_payload(phone="01711000009")
        ).json()["patientCode"]
        assert dhaka_second.endswith("00002")

    def test_admin_cannot_register_a_patient(self, admin_client):
        """Registration is a branch desk action; the UI hides it from Admin."""
        response = admin_client.post(reverse("patients:patient-list"), patient_payload())
        assert response.status_code == 403

    def test_anonymous_denied(self, api_client):
        assert api_client.post(reverse("patients:patient-list"), patient_payload()).status_code == 401

    def test_created_by_comes_from_the_token(self, manager_client, manager):
        response = manager_client.post(reverse("patients:patient-list"), patient_payload())

        patient = Patient.objects.get(pk=response.json()["id"])
        assert patient.created_by_id == manager.id

    def test_created_by_cannot_be_spoofed(self, manager_client, manager, admin_user):
        response = manager_client.post(
            reverse("patients:patient-list"),
            patient_payload(created_by=str(admin_user.id)),
        )

        patient = Patient.objects.get(pk=response.json()["id"])
        assert patient.created_by_id == manager.id

    def test_optional_fields_may_be_omitted(self, manager_client):
        response = manager_client.post(reverse("patients:patient-list"), patient_payload())

        assert response.status_code == 201
        body = response.json()
        assert body["bloodGroup"] == ""
        assert body["emergencyContact"] == ""
        assert body["email"] == ""

    def test_optional_fields_are_stored_when_supplied(self, manager_client):
        response = manager_client.post(
            reverse("patients:patient-list"),
            patient_payload(
                blood_group="B+",
                email="arif@example.com",
                referred_by="Dr. Rahman",
                chief_complaint="Stammering since childhood",
                national_id="1234567890",
                notes="Prefers morning slots",
            ),
        )

        body = response.json()
        assert body["bloodGroup"] == "B+"
        assert body["referredBy"] == "Dr. Rahman"
        assert body["chiefComplaint"] == "Stammering since childhood"
        assert body["nationalId"] == "1234567890"

    def test_creation_is_audited(self, manager_client, manager):
        manager_client.post(reverse("patients:patient-list"), patient_payload())

        entry = AuditLog.objects.filter(
            action=AuditLog.Action.CREATE, target_type="Patient"
        ).first()
        assert entry is not None
        assert entry.actor_id == manager.id


@pytest.mark.slow
@pytest.mark.money
@pytest.mark.django_db(transaction=True)
class TestPatientCodeConcurrency:
    """
    transaction=True, overriding the module-level default: worker threads use
    their own DB connections, which can only see committed rows. Under the
    normal per-test atomic wrapper, `branch` and `manager` exist only inside
    the outer test's uncommitted transaction and are invisible to those
    threads -- every worker fails with a foreign-key violation before the
    concurrency behaviour this test exists to check ever runs.
    """

    def test_concurrent_registrations_get_unique_codes(self, branch, manager, django_db_blocker):
        """
        The mock's in-memory counter passes every sequential test and still
        issues duplicates across Gunicorn workers. Only a threaded test proves
        the row lock actually holds.
        """
        codes = []
        errors = []
        worker_count = 8

        def worker(index):
            try:
                with django_db_blocker.unblock():
                    patient = create_patient(
                        actor=manager,
                        branch=branch,
                        data={
                            "name": f"Concurrent {index}",
                            "date_of_birth": date(1990, 1, 1),
                            "gender": "male",
                            "phone": f"017110000{index:02d}",
                            "address": "Dhaka",
                        },
                    )
                    codes.append(patient.patient_code)
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, f"workers raised: {errors}"
        assert len(codes) == worker_count
        assert len(set(codes)) == worker_count, f"duplicate codes issued: {sorted(codes)}"


# ---------------------------------------------------------------------------
# Required-field validation
# ---------------------------------------------------------------------------


class TestRequiredFields:
    @pytest.mark.parametrize("field", ["name", "date_of_birth", "gender", "phone", "address"])
    def test_missing_required_field_is_rejected_and_named(self, manager_client, field):
        payload = patient_payload()
        del payload[field]

        response = manager_client.post(reverse("patients:patient-list"), payload)

        assert response.status_code == 400
        assert field in response.json()

    def test_future_date_of_birth_rejected(self, manager_client):
        future = (date.today() + timedelta(days=1)).isoformat()
        response = manager_client.post(
            reverse("patients:patient-list"), patient_payload(date_of_birth=future)
        )

        assert response.status_code == 400
        assert "date_of_birth" in response.json()

    @pytest.mark.parametrize(
        "field,value",
        [
            ("gender", "unspecified"),
            ("guardian_relation", "uncle"),
            ("blood_group", "Z+"),
            ("status", "archived"),
        ],
    )
    def test_invalid_choice_rejected(self, manager_client, field, value):
        response = manager_client.post(
            reverse("patients:patient-list"), minor_payload(**{field: value})
        )

        assert response.status_code == 400
        assert field in response.json()


# ---------------------------------------------------------------------------
# The age-conditional guardian rule
# ---------------------------------------------------------------------------


class TestGuardianRequirement:
    def test_minor_without_guardian_is_rejected(self, manager_client):
        payload = minor_payload()
        for field in ("guardian_name", "guardian_relation", "guardian_phone"):
            del payload[field]

        response = manager_client.post(reverse("patients:patient-list"), payload)

        assert response.status_code == 400
        body = response.json()
        assert "guardian_name" in body
        assert "guardian_relation" in body
        assert "guardian_phone" in body

    def test_minor_with_guardian_is_accepted(self, manager_client):
        response = manager_client.post(reverse("patients:patient-list"), minor_payload())
        assert response.status_code == 201

    def test_adult_without_guardian_is_accepted(self, manager_client):
        response = manager_client.post(reverse("patients:patient-list"), patient_payload())
        assert response.status_code == 201

    def test_adult_with_guardian_is_also_accepted(self, manager_client):
        """Optional means optional — supplying it must not be an error."""
        response = manager_client.post(
            reverse("patients:patient-list"),
            patient_payload(
                guardian_name="Someone",
                guardian_relation="guardian",
                guardian_phone="01711000099",
            ),
        )
        assert response.status_code == 201

    def test_exactly_eighteen_today_counts_as_adult(self, manager_client):
        """Pins the cutoff so a future change to it fails loudly."""
        response = manager_client.post(
            reverse("patients:patient-list"),
            patient_payload(date_of_birth=years_ago(18)),
        )
        assert response.status_code == 201

    def test_one_day_under_eighteen_still_needs_a_guardian(self, manager_client):
        payload = patient_payload(date_of_birth=years_ago(18, days=1))
        response = manager_client.post(reverse("patients:patient-list"), payload)

        assert response.status_code == 400
        assert "guardian_name" in response.json()

    def test_updating_an_adult_does_not_demand_guardian_fields(self, manager_client):
        created = manager_client.post(
            reverse("patients:patient-list"), patient_payload()
        ).json()

        response = manager_client.patch(
            reverse("patients:patient-detail", args=[created["id"]]),
            {"address": "Banani, Dhaka"},
        )
        assert response.status_code == 200

    def test_updating_a_minor_keeps_existing_guardian_data(self, manager_client):
        """
        Editing a phone number on a child's record shouldn't force re-entering
        guardian details that are already stored.
        """
        created = manager_client.post(
            reverse("patients:patient-list"), minor_payload()
        ).json()

        response = manager_client.patch(
            reverse("patients:patient-detail", args=[created["id"]]),
            {"address": "Mirpur, Dhaka"},
        )
        assert response.status_code == 200

    def test_guardian_columns_stay_nullable_at_db_level(self, branch, manager):
        """
        Requiredness is a serializer rule, not a schema rule — otherwise
        existing incomplete records break and a migration can't be applied.
        """
        patient = Patient.objects.create(
            patient_code="PT-DHK-2026-09999",
            name="Legacy Record",
            phone="01711000077",
            branch=branch,
        )
        assert patient.pk is not None
        assert patient.guardian_name == ""


class TestAgeDerivation:
    def test_age_is_computed_from_date_of_birth(self, manager_client):
        response = manager_client.post(
            reverse("patients:patient-list"), patient_payload(date_of_birth=years_ago(31))
        )
        assert response.json()["age"] == 31

    def test_age_accounts_for_birthday_not_yet_reached(self):
        """Naive year subtraction is off by one for half the year."""
        reference = date(2026, 3, 1)
        assert calculate_age(date(2000, 6, 15), on=reference) == 25  # birthday later this year
        assert calculate_age(date(2000, 1, 15), on=reference) == 26  # already had it

    def test_age_on_the_birthday_itself(self):
        assert calculate_age(date(2000, 5, 10), on=date(2026, 5, 10)) == 26

    def test_null_date_of_birth_gives_null_age(self, branch):
        patient = Patient.objects.create(
            patient_code="PT-DHK-2026-09998", name="No DOB", phone="01711000078", branch=branch
        )
        assert patient.age is None

    def test_client_supplied_age_is_ignored(self, manager_client):
        """A stored age rots; the request body must not be able to set one."""
        response = manager_client.post(
            reverse("patients:patient-list"),
            patient_payload(date_of_birth=years_ago(31), age=99),
        )
        assert response.json()["age"] == 31


# ---------------------------------------------------------------------------
# Phone handling
# ---------------------------------------------------------------------------


class TestPhoneNormalisation:
    @pytest.mark.parametrize(
        "supplied",
        ["01711000001", "+8801711000001", "8801711000001", "+880 1711-000001", "01711-000001"],
    )
    def test_equivalent_formats_store_identically(self, manager_client, supplied):
        response = manager_client.post(
            reverse("patients:patient-list"),
            patient_payload(phone=supplied, name=f"Format {supplied}"),
        )

        assert response.status_code == 201
        assert response.json()["phone"] == "01711000001"

    @pytest.mark.parametrize(
        "bad", ["12345", "0171100000", "0121100000", "abcdefghijk", "+1 555 0100"]
    )
    def test_malformed_numbers_rejected(self, manager_client, bad):
        response = manager_client.post(
            reverse("patients:patient-list"), patient_payload(phone=bad)
        )
        assert response.status_code == 400
        assert "phone" in response.json()

    def test_guardian_and_emergency_numbers_validated_too(self, manager_client):
        bad_guardian = manager_client.post(
            reverse("patients:patient-list"), minor_payload(guardian_phone="123")
        )
        bad_emergency = manager_client.post(
            reverse("patients:patient-list"), patient_payload(emergency_contact="123")
        )

        assert bad_guardian.status_code == 400
        assert bad_emergency.status_code == 400

    def test_either_format_finds_the_patient_in_search(self, manager_client):
        manager_client.post(
            reverse("patients:patient-list"), patient_payload(phone="+880 1711-000001")
        )

        for query in ("01711000001", "+8801711000001"):
            response = manager_client.get(reverse("patients:patient-list"), {"search": query})
            assert response.json()["count"] == 1, f"search failed for {query}"


# ---------------------------------------------------------------------------
# Branch isolation
# ---------------------------------------------------------------------------


@pytest.mark.isolation
class TestBranchIsolation:
    def test_manager_list_excludes_other_branches(
        self, manager_client, other_manager_client
    ):
        manager_client.post(reverse("patients:patient-list"), patient_payload(name="Dhaka Patient"))
        other_manager_client.post(
            reverse("patients:patient-list"),
            patient_payload(name="Chattogram Patient", phone="01811000001"),
        )

        response = manager_client.get(reverse("patients:patient-list"))

        names = [item["name"] for item in response.json()["results"]]
        assert names == ["Dhaka Patient"]

    def test_manager_cannot_retrieve_another_branch_patient(
        self, manager_client, other_manager_client
    ):
        """The IDOR case: list is scoped but detail is left open."""
        foreign = other_manager_client.post(
            reverse("patients:patient-list"), patient_payload(phone="01811000001")
        ).json()

        response = manager_client.get(
            reverse("patients:patient-detail", args=[foreign["id"]])
        )
        assert response.status_code == 404

    def test_manager_cannot_edit_another_branch_patient(
        self, manager_client, other_manager_client
    ):
        foreign = other_manager_client.post(
            reverse("patients:patient-list"), patient_payload(phone="01811000001")
        ).json()

        response = manager_client.patch(
            reverse("patients:patient-detail", args=[foreign["id"]]), {"name": "Hijacked"}
        )
        assert response.status_code == 404

    def test_posted_branch_is_ignored(self, manager_client, manager, other_branch):
        """
        A manager sending another branch's id must not file the patient there.
        The branch always comes from the token.
        """
        response = manager_client.post(
            reverse("patients:patient-list"),
            patient_payload(branch=str(other_branch.id)),
        )

        patient = Patient.objects.get(pk=response.json()["id"])
        assert patient.branch_id == manager.branch_id

    def test_admin_sees_every_branch(self, admin_client, manager_client, other_manager_client):
        manager_client.post(reverse("patients:patient-list"), patient_payload())
        other_manager_client.post(
            reverse("patients:patient-list"), patient_payload(phone="01811000001")
        )

        response = admin_client.get(reverse("patients:patient-list"))
        assert response.json()["count"] == 2

    def test_admin_can_narrow_to_one_branch(
        self, admin_client, manager_client, other_manager_client, branch
    ):
        manager_client.post(reverse("patients:patient-list"), patient_payload())
        other_manager_client.post(
            reverse("patients:patient-list"), patient_payload(phone="01811000001")
        )

        response = admin_client.get(
            reverse("patients:patient-list"), {"branch": str(branch.id)}
        )
        assert response.json()["count"] == 1


# ---------------------------------------------------------------------------
# Search, listing, deletion
# ---------------------------------------------------------------------------


class TestSearch:
    @pytest.fixture(autouse=True)
    def seed(self, manager_client):
        manager_client.post(reverse("patients:patient-list"), minor_payload())
        manager_client.post(
            reverse("patients:patient-list"),
            patient_payload(name="Tasnia Rahman", phone="01711000005"),
        )

    def test_partial_case_insensitive_name(self, manager_client):
        response = manager_client.get(reverse("patients:patient-list"), {"search": "tasnia"})
        assert response.json()["count"] == 1

    def test_by_phone(self, manager_client):
        response = manager_client.get(reverse("patients:patient-list"), {"search": "01711000005"})
        assert response.json()["count"] == 1

    def test_by_guardian_name(self, manager_client):
        response = manager_client.get(reverse("patients:patient-list"), {"search": "Kamal"})
        assert response.json()["count"] == 1

    def test_by_full_patient_code(self, manager_client):
        code = manager_client.get(reverse("patients:patient-list")).json()["results"][0][
            "patientCode"
        ]
        response = manager_client.get(reverse("patients:patient-list"), {"search": code})
        assert response.json()["count"] == 1

    def test_by_numeric_tail_of_code(self, manager_client):
        """Staff type `00001`, not the whole `PT-DHK-2026-00001`."""
        response = manager_client.get(reverse("patients:patient-list"), {"search": "00001"})
        assert response.json()["count"] >= 1

    def test_no_match_returns_empty_not_error(self, manager_client):
        response = manager_client.get(
            reverse("patients:patient-list"), {"search": "nobody-by-this-name"}
        )
        assert response.status_code == 200
        assert response.json()["count"] == 0


class TestListSerializer:
    def test_list_carries_fields_the_search_component_needs(self, manager_client):
        """PatientSearchResultList renders name, code, phone and gender."""
        manager_client.post(reverse("patients:patient-list"), patient_payload())

        item = manager_client.get(reverse("patients:patient-list")).json()["results"][0]
        for field in ("name", "patientCode", "phone", "gender"):
            assert field in item


class TestDeletion:
    def test_delete_is_soft_and_hides_from_list(self, manager_client):
        created = manager_client.post(
            reverse("patients:patient-list"), patient_payload()
        ).json()

        response = manager_client.delete(
            reverse("patients:patient-detail", args=[created["id"]])
        )

        assert response.status_code == 204
        assert manager_client.get(reverse("patients:patient-list")).json()["count"] == 0
        assert Patient.all_objects.filter(pk=created["id"]).exists()
