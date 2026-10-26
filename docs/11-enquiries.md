# 11 — Enquiries (calls off an ad)

**Status:** built 2026-10-27. Requested by the client: they advertise daily, prospective patients or their guardians ring, and none of them are registered. The clinic wants those calls recorded, followed up, and measured against the ads that produced them.

## Why this is not a `Patient`

Walk-in buyers became `Patient` rows because every `Payment` needs one (`02-patients.md`, `06-materials.md`). An enquiry involves no money and no service, so nothing forces it into that table — and putting it there would mean the patient list, the directory, the dashboard counts and every patient picker had to learn to exclude a *second* kind of non-patient.

A separate table keeps the patient roster meaning exactly one thing: people the clinic has taken on.

The link runs the other way. `converted_patient` is set when a caller actually comes in and registers, which is what turns *"40 calls from Facebook"* into *"40 calls, 12 enrolled"*.

## Model

`Enquiry` — branch-scoped, soft-delete, timestamped. Code series `ENQ-DHK-2026-00001`, per-branch, from the same race-safe sequence every other code uses.

| field | notes |
|---|---|
| `caller_name`, `caller_phone` | required; the phone is BD-validated and normalised on save, so a number typed any way still matches a search |
| `caller_relation` | self / father / mother / guardian / other |
| `patient_name`, `patient_age` | who the therapy is for. Age is an integer, not a birth date — nobody reads out a date of birth to a receptionist, and a wrong one would look authoritative later |
| `concern` | what they described |
| `source` | **required** — facebook / google / poster / referral / signboard / other |
| `source_detail` | free text: which campaign, whose referral |
| `status` | new / contacted / visit_planned / visited / converted / not_interested / unreachable |
| `follow_up_on` | when to ring back; null means nobody has decided yet |
| `converted_patient` | `OneToOne`, `PROTECT` — losing the link would quietly turn a converted enquiry back into an unconverted one and understate the ad's return |
| `branch`, `taken_by`, `called_at` | set by the server; `called_at` is editable and deliberately not `created_at`, because a call is often typed in later and the report should count when the phone rang |

### `status` and `follow_up_on` are different questions

`status` says **where someone is in the funnel**. `follow_up_on` says **whether today's work includes them**. They are separate filters, and a closed enquiry (converted, or not interested) is never counted as outstanding work however old its follow-up date is.

Conflating them is how a patient who already enrolled ends up on a call-back list.

## Endpoints

`/api/enquiries/` — full CRUD, branch-scoped by the shared mixin.

- Recording and converting are **Manager-only**: Admin has no branch to file a call under, the same rule patients follow. Admin reads, edits and deletes for oversight.
- `GET /api/enquiries/summary/` — the page header's counts (total, converted, due today, overdue, top source), computed over **whatever the filters left**, so the numbers always describe the list beneath them.
- `POST /api/enquiries/{id}/convert/` — registers the caller as a patient and links them. The registration goes through `PatientWriteSerializer`, so it obeys exactly the rules the registration form does, including the guardian requirement for minors. A caller is not a shortcut past them. Converting twice is refused (`already_converted`).

### Filters

`search` (caller or patient name, phone in any format, concern, code, source detail) · `status` · `source` · `followUp` (`today` / `overdue` / `upcoming` / `none`) · `dateFrom`/`dateTo` on `called_at` · `branch` (Admin).

## Tests

39, in `apps/enquiries/tests/test_enquiries.py`. The groups that matter:

- **an enquiry is not a patient** — recording a call creates no `Patient` and leaves the patient list untouched
- **branch isolation** — on reads, edits and conversion
- **conversion links, and links once** — plus the registration rules still applying
- **follow-up is not status** — including that someone who said no is never outstanding work
