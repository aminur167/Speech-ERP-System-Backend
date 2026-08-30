# 02 — Patients

Depends on `01-auth-and-branches.md` (User, Branch, branch-scoping mixin).

Read `docs/00-OVERVIEW.md` first for the cross-cutting rules.

Frontend source: `src/lib/api/patients.ts`, `src/lib/api/patientDirectory.ts`, `src/lib/api/patientServices.ts`, `src/components/patients/*`.

> **Note on ordering:** the Patient Directory endpoint below joins against enrollments, plans, and payments. Build basic Patient CRUD now; build the Directory *after* `04-payments-core.md` and `05-enrollments.md` exist, or stub the derived fields and fill them in then. `ROADMAP.md` reflects this.

---

## Model: Patient

Field set confirmed by the user for the patient registration form. Several fields are **new** relative to the current frontend mock (`src/types/domain.ts`) and several previously-optional fields are now **required** — both are marked below. The frontend's `Patient` type and `PatientRegistrationForm` will need updating to match when this is wired up.

| Field | Type | Required | Notes |
|---|---|---|---|
| `patient_code` | CharField, unique | auto | e.g. `PT-2026-00125` |
| `name` | CharField | ✅ | "Full Name" — e.g. "Rahim Ahmed" |
| `date_of_birth` | DateField | ✅ **(now required)** | Age is **derived** from this, never stored |
| `gender` | choices: `male`\|`female`\|`other` | ✅ **(now required)** | |
| `blood_group` | choices | optional | 🆕 `A+`,`A-`,`B+`,`B-`,`AB+`,`AB-`,`O+`,`O-` |
| `phone` | CharField | ✅ | "Contact Number", BD format |
| `address` | TextField | ✅ **(now required)** | "House, road, area, city" |
| `guardian_name` | CharField | ⚠️ conditional | Required **only if the patient is a minor** — see below |
| `guardian_relation` | choices: `father`\|`mother`\|`guardian`\|`other` | ⚠️ conditional | Same rule |
| `guardian_phone` | CharField | ⚠️ conditional | 🆕 Same rule |
| `emergency_contact` | CharField | optional | 🆕 "Alternate number" |
| `email` | EmailField | optional | Not on the confirmed form list but already in the model and shown on the profile — keep it |
| `referred_by` | CharField | optional | 🆕 Referring doctor/person — standard clinic field, and useful for the client to see where patients come from |
| `chief_complaint` | TextField | optional | 🆕 Why the patient came (e.g. delayed speech, stammering). Core context for a therapy clinic; currently the system stores nothing clinical at all |
| `national_id` | CharField | optional | 🆕 BD NID / birth registration number — identity verification, common in BD clinic records |
| `status` | choices: `active`\|`inactive` | auto (`active`) | 🆕 Patient lifecycle, distinct from `is_deleted`. Lets the clinic archive someone who stopped coming without deleting their history |
| `notes` | TextField | optional | 🆕 Free-text remarks for reception/admin |
| `branch` | FK → Branch | auto | From the authenticated manager, never the request body |
| `created_by` | FK → User | auto | 🆕 Who registered this patient — audit basics |
| `created_at` | DateTimeField | auto | Drives "New Patients this month" metrics |
| `updated_at` | DateTimeField | auto | 🆕 Audit basics |
| `is_deleted` | bool | auto | Soft delete — never hard-delete patient records |

### ✅ CONFIRMED: guardian is conditionally required, not blanket-optional

The user asked for guardian fields to be optional so adult patients don't get blocked, and for the rest to follow industry practice. Blanket-optional is the wrong shape for a clinic that mostly treats children — a child's record with no guardian contact is a genuine safety and operational problem (nobody to call).

**Rule: guardian fields are required when the patient is a minor, optional when adult.**
- Compute age from `date_of_birth` at validation time. Under 18 → `guardian_name`, `guardian_relation`, `guardian_phone` all required. 18+ → all optional.
- Keep the DB columns nullable; enforce in the create/update serializer. Never write a migration that makes them non-null.
- **Frontend follow-up:** the guardian section should show as required/optional dynamically once a date of birth is entered. Not built yet.
- Confirm the threshold with the client (18 assumed — a therapy clinic may prefer a different cutoff).

### Newly added fields (🆕) — confirm before building

The user said to add whatever else the system genuinely needs. The additions above are the ones defensible for a real clinic record; each is **optional**, so none blocks registration:

- `referred_by`, `chief_complaint` — clinical/operational context the system currently lacks entirely
- `national_id` — identity verification
- `status` — archive a lapsed patient without deleting history (distinct from soft-delete, which is for mistakes)
- `notes` — every real clinic system needs a free-text field
- `created_by`, `updated_at` — basic audit, cheap now and impossible to backfill later

Run this list past the client before implementing — a field nobody fills is dead weight on the form. Trimming is fine; adding later is harder.

**Deliberately not added:** anything genuinely clinical (diagnosis, treatment plan, session notes). That's a therapy-records module, not a patient field, and it carries real medical-data handling obligations. Don't half-build it inside the patient row.

### Adult patients and emergency contact

With guardian optional for adults, an adult record can end up with only one phone number. Consider making `emergency_contact` required when no guardian is provided, so every patient has a second reachable contact. Confirm with the client — flagged rather than assumed.

**Age is displayed on the form but never submitted or stored** — it's computed live from `date_of_birth` in the UI and computed server-side for API responses. A stored age silently rots.

**Phone validation:** three separate phone fields now (`phone`, `guardian_phone`, `emergency_contact`) all in BD format (`+880 1XXX-XXXXXX` / `01XXXXXXXXX`). Define one shared validator and reuse it rather than duplicating the regex three times. Normalize to a single stored format so search works consistently regardless of how it was typed.

### ✅ CONFIRMED: required applies to new registrations only

The newly-required fields are enforced **in the create serializer**, not at the database level. **Keep every column nullable.**

Existing records may be incomplete (the seeded data already includes patients with no guardian), and making the columns strictly non-null would break the migration outright. Editing an old record must not force the manager to invent missing history either — validate on create, and on update only validate the fields actually being changed.

This is standard practice for tightening validation on live data: the rule applies going forward, and old records are cleaned up over time (or never, harmlessly).

### ✅ CONFIRMED: `patient_code` is a per-branch series

Format: **`PT-{BRANCH_CODE}-{year}-{5-digit}`** — e.g. `PT-DHK-2026-00001`, `PT-CTG-2026-00001`.

Same reasoning as receipt numbers in `04`: registration must work **offline**, and a branch that owns its own sequence can issue codes without coordinating with any other branch. One reception device per branch is confirmed, so there's no collision risk within a branch either.

**Still race-safe within a branch** — use a per-branch Postgres sequence or a `select_for_update()`-locked counter row. The mock's in-memory `sequence += 1` collides under multiple Gunicorn workers.

**Frontend follow-up:** patient code display and search must handle the longer format. Search should match on the numeric part alone too (staff will type `00001`, not the whole string).

**Age is computed, not stored.** The frontend derives it from `date_of_birth` (`calculateAge` in `patientDirectory.ts`, exported and reused by the patient profile). Compute server-side for the directory response; a stored age silently rots.

---

## Endpoints

### Basic CRUD

| Method | Path | Notes |
|---|---|---|
| GET | `/patients/` | `?search=&page=&pageSize=` — search matches name, phone, patient_code, guardian_name |
| POST | `/patients/` | **Manager only** (frontend hides Register for Admin — enforce server-side) |
| GET | `/patients/{id}/` | Branch-scoped, 404 if out of scope |

Search behavior is defined by the mock's `listPatients` filter: case-insensitive across name / phone / patientCode / guardianName. Used by the enrollment wizards' patient search (`PatientSearchResultList` — displays name, code, phone, gender, so make sure those fields are all in the list serializer).

### Patient Directory — the heavy one

`GET /patients/directory/` returns a **denormalized** listing that joins enrollments, plans, and payments. This is the most performance-sensitive endpoint in the system.

Response item shape (from `PatientDirectoryItem`):
```
id, patientCode, name, age, gender, guardianName, guardianRelation,
phone, branchId, branchName, therapyType, paymentType, status,
serviceCategories[], paymentMethods[], createdAt
```

**Derived fields — the actual business rules (from `buildDirectory`):**

| Field | Rule |
|---|---|
| `therapyType` | Name of the service from their active monthly enrollment; else from their active installment plan; else `"—"` |
| `status` | `active-care` if an active monthly enrollment exists; else `in-progress` if an active installment plan exists; else `action-needed` |
| `paymentType` | Label of their **most recent** payment's method (e.g. "bKash"); `"—"` if never paid |
| `serviceCategories[]` | Every distinct service category they've ever been billed for (excluding `material_sale`) — powers the Service Type column + filter |
| `paymentMethods[]` | Every distinct payment method they've ever used — powers the Payment Type filter |
| `branchName` | Resolved from the FK — **never** a hardcoded lookup map (two real bugs of exactly this kind were already fixed in the frontend) |
| `age` | Derived from `date_of_birth` |

Query params: `search`, `status`, `paymentType` (a payment *method*, e.g. `bkash`), `gender`, `serviceCategory`, `timeRange` (`today`/`week`/`month`), `date` (exact ISO date — **overrides** `timeRange` when both are set), `branch` (Admin only), `page`, `pageSize`.

**Performance is the whole game here.** Done naively this is a textbook N+1: one query for patients, then per-patient queries for enrollments, plans, and payments. With a decade of data that's fatal. Use `select_related` for FKs (branch), `prefetch_related` for the reverse relations, and aggregate the category/method sets in bulk rather than per row. Add indexes on `branch_id`, `created_at`, and the searched text fields. Benchmark this endpoint against a seeded dataset of realistic future size (e.g. 50k patients / 500k payments), not against 12 demo rows.

### Summary & active services

| Method | Path | Notes |
|---|---|---|
| GET | `/patients/directory/summary/` | `{ total, activeCare, inProgress, actionNeeded, intake }` — `?branch=` and `?date=` |
| GET | `/patients/{id}/active-services/` | Every active monthly enrollment + installment plan, newest first |

`intake` = patients created in the month of the given `date` (defaults to current month). The `date` param exists because of the dashboard date picker — passing a past date must return that date's month's intake, not today's.

`active-services` returns a **sorted list**, not one-of-each. A patient can hold several active services simultaneously. Item shape is a discriminated union (see `PatientActiveServiceItem`):
- `{ type: "monthly", id, serviceName, createdAt, enrollment: {...with bills} }`
- `{ type: "installment", id, serviceName, createdAt, plan: {...with installments, totalAmount} }`

Sorted by `createdAt` descending. Terminated enrollments/plans are excluded.

---

## Required Tests

**CRUD & codes**
- Create patient → `patient_code` matches `PT-{BRANCH}-{year}-{00001}` and increments within that branch.
- **Two branches each start their own series** — Dhaka's first patient and Chattogram's first patient both get `00001` with different branch prefixes, and neither affects the other's counter.
- Concurrent creates within one branch produce **unique** codes (the race-condition test — worth doing properly).
- Search matches the full code and the numeric portion alone.
- Create with all required fields → succeeds; optional fields (`blood_group`, `emergency_contact`, `email`) null.
- Admin attempting `POST /patients/` → 403 (registration is Manager-only).
- Soft-deleted patients don't appear in list results.

**Required-field validation** (each omitted field individually → validation error naming that field)
- Missing `name`, `date_of_birth`, `gender`, `phone`, or `address` → rejected.
- Omitting any optional field (`blood_group`, `emergency_contact`, `email`, `referred_by`, `chief_complaint`, `national_id`, `notes`) → accepted.
- Invalid `gender`, `guardian_relation`, `blood_group`, or `status` choice → validation error.
- Future `date_of_birth` → rejected.
- `created_by` set from the authenticated user, not the request body.

**Conditional guardian requirement (age-based — highest-value tests in this group)**
- Minor patient (e.g. DOB 8 years ago) **without** guardian fields → rejected, error names the missing guardian fields.
- Minor patient **with** all three guardian fields → accepted.
- Adult patient (e.g. DOB 31 years ago) **without** guardian fields → accepted.
- Adult patient **with** guardian fields → also accepted (optional, not forbidden).
- Boundary: exactly 18 years old today → treated as adult (assert the chosen cutoff explicitly so it's pinned down).
- A patient just under 18 → guardian still required.
- Updating an existing adult record doesn't retroactively demand guardian fields.
- **DB columns stay nullable** — assert a record can be saved without guardian data at the model level, so no future migration makes them non-null.
- Each of the three phone fields validates BD format and rejects malformed input.
- Phone numbers normalize consistently — the same number entered as `01711000001` and `+880 1711-000001` stores identically and both find the patient via search.
- `age` in responses is derived from `date_of_birth`; a client-supplied `age` in the request body is ignored.

**Branch isolation**
- Manager A's `/patients/` excludes Branch B's patients.
- Manager A `GET /patients/{branch_B_patient_id}/` → 404.
- Creating a patient always assigns the manager's own branch, even if a different `branch` is posted in the body.
- Admin sees all branches' patients; `?branch=X` correctly narrows.

**Search**
- Matches by name (partial, case-insensitive), by phone, by patient_code, by guardian_name.
- Non-matching query → empty results with `count: 0`, not an error.

**Directory derived fields** (the highest-value tests in this module)
- Patient with an active monthly enrollment → `status: "active-care"`, `therapyType` = that service's name.
- Patient with an active installment plan only → `status: "in-progress"`.
- Patient with neither → `status: "action-needed"`, `therapyType: "—"`.
- Patient with a *terminated* enrollment → treated as `action-needed`, not active.
- Patient with both monthly and installment active → monthly wins for `therapyType`/`status` (matches mock precedence).
- `paymentType` reflects the **latest** payment's method, not the first.
- Patient who never paid → `paymentType: "—"`.
- `serviceCategories` contains every distinct category billed, deduplicated, and **excludes** `material_sale`.
- `branchName` matches the actual related branch — explicitly test a non-Dhaka branch patient (the exact bug that occurred twice in the frontend).
- `age` correct including the "birthday hasn't happened yet this year" case; null `date_of_birth` → null age.

**Directory filters**
- Each filter (status, gender, serviceCategory, paymentType) narrows correctly and combines with search.
- `date` overrides `timeRange` when both supplied.
- Pagination: `count` reflects the filtered total, not the unfiltered table size.

**Summary**
- Counts match the directory listing for the same branch/filters.
- `intake` for a past `date` returns that month's intake, not the current month's.
- Branch-scoped: Manager's summary counts exclude other branches.

**Active services**
- Returns multiple active services for one patient, newest first.
- Excludes terminated enrollments/plans.
- Patient with none → empty list (not an error).
- Monthly items carry their bills; installment items carry their installments and `totalAmount`.

**Performance**
- Assert a bounded query count for the directory endpoint (e.g. `assertNumQueries`) so an N+1 regression fails the test suite rather than silently shipping.
