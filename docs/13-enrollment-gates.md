# 13 — What stops a patient being enrolled

**Status:** narrowed from patient-wide to per-service on 2026-10-27, at the clinic's request. Three rules now, all in `apps/enrollments/services.py` and all enforced in the service layer rather than the view, so a request straight to the API is refused on the same terms as the button.

## The rules

| # | rule | code | applies to |
|---|---|---|---|
| 1 | A **running** package for this service blocks enrolling in it | `already_enrolled` | new monthly enrolment, new installment plan |
| 2 | Money owed **on this service** blocks enrolling in it, and blocks reactivating it | `outstanding_dues` | the same, plus `resume_monthly_service` / `resume_installment_plan` |
| 3 | Money owed on **any other service** blocks nothing | — | it is shown, not enforced |

## What changed, and why

The gate used to be **patient-wide**: `_assert_no_outstanding_dues` refused a new service, or a reactivation, while the patient owed anything at all.

That left a manager unable to sell speech therapy to someone who owed for occupational therapy. The debt was real, but refusing the sale did not collect it — it only cost the clinic the next sale. So rule 3 is now informational: the confirm step shows the whole balance and the manager sells anyway.

Rule 2 is the part worth keeping. Re-selling someone the very package they have not paid for buries the first due under a second, and reactivating a stopped service revives exactly the months that service left unpaid.

## Rule 1 was an accident before this

Nothing enforced "one package per service" — there is no unique constraint on `(patient, service)`, only an index. Nothing needed to, because the patient-wide due gate refused a second enrollment as a side effect: the first one's unpaid months were a due, so the second was turned away.

Narrowing the gate removed the accident, so `_assert_not_already_enrolled` writes the rule down. Two live packages for one service would bill the patient twice for one course of therapy.

It reads **only the caller's own table** — a monthly enrolment can only collide with another monthly enrolment for that service — so it costs one query, not two. The write budgets in `test_query_budget.py` went up by exactly that one read.

### A stopped package does not block

Re-joining a service someone left is a real thing the clinic does, and it **charges the admit fee again**. Reactivating keeps the old row and charges nothing. Both are offered, because both are legitimate — what a manager must not do is take a joining fee from someone who only needed their old service switched back on, so the enrolment screen says a stopped enrollment exists before the fee is taken.

There is **no way to delete an enrollment** (the viewsets are `get`/`post` only and these models are not soft-delete), so "stopped" is as far back as a service ever goes.

## Where the figures come from

`patient_outstanding_dues(patient)` is unchanged except that each item now carries **`serviceId`**. The gate matches on that, never on `serviceName`: two services can share a name, and the wrong one would be blocked.

`service_outstanding_dues(patient, service)` filters that list in Python rather than issuing a second query — the caller has just read every owed item in order to display it.

## The screens

- `useEnrollmentEligibility(patientId, serviceId)` mirrors all three rules client-side so the refusal is explained *before* the button.
- `EnrollmentEligibilityNotice` renders them as three visually distinct blocks, because the manager does something different about each: pick another service / collect the due / nothing at all. The "owed elsewhere" block is deliberately neutral-coloured — it is not a warning.
- `OutstandingDueNotice` (the older component) now takes its explanatory line as a **required `message` prop**. It used to hardcode *"clear the due payments before enrolling a new service"*, which is no longer true, and its two remaining callers are blocked by two different rules — reactivation, and taking an advance while in arrears.
- The profile's **Reactivate** action is gated on that service's own arrears, not the patient's total, and names the figure: *"Clear the ৳5,000 owed on this service first."*

## Unchanged

**Deleting a patient** is still refused while they owe anything, patient-wide (`apps/patients/views.py`). That is a different question — not "may they buy this?" but "is this record finished with?" — and a patient who owes money is not.

**Taking an advance** is still refused while earlier months are unpaid. Paying ahead while an older month is outstanding is not a thing the oldest-first rule allows.

## Tests

`apps/enrollments/tests/test_outstanding_due_gate.py` — rewritten around the new rule:

- `TestADueBlocksItsOwnServiceOnly` — another monthly service, and an installment package, can still be sold while one is unpaid; the owed service itself is refused; clearing it allows the same service again
- `TestARunningPackageBlocksItsOwnService` — a second live package is refused for both kinds; a stopped one is not

Also inverted, in place, with the reason written into each: `test_a_due_on_another_service_no_longer_blocks_it` (`test_admission_fee.py`) and `test_a_due_on_another_service_does_not_block_it` (`test_auto_termination.py`).
