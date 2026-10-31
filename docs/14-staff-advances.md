# 14 — Salary advances, and reading staff history

**Status:** built 2026-10-28. Two things that turned out to belong together: advances need a past month's payroll to be correct, and it was not.

## Part 1 — Advances

Salary paid early, recovered from a later month. `StaffAdvance` runs the same three steps as `SalaryPayment`: Manager requests, **Admin** approves, Manager disburses, and the `Expense` is written at the moment the money actually leaves.

### Why it is its own model

`recovered_amount`. A salary request is finished when it reaches PAID; an advance stays open afterwards, shrinking across however many months it takes to clear. Folding a running balance into a table whose rows are done at PAID would have put a field on every salary row that means nothing for it.

### The rules

| | |
|---|---|
| **A reason is required** | Unlike a salary amount, which the server computes from the roster, somebody *chose* this figure. An unexplained early payment is exactly what an audit asks about. |
| **Several can be open at once** | A second emergency does not wait on the first being repaid. |
| **Capped at one month's salary** | Everything outstanding, plus everything already promised, plus this, must fit inside `monthly_salary`. Without it an advance could be granted that takes several months of someone's entire pay to clear — a decision for a person, not a form. |
| **Only a PAID advance is a debt** | One still waiting on Admin is not money the person has, so deducting it would collect a debt that does not exist. |

### Recovery

Taken **oldest first** — the model's own `ordering`, so the deduction walks the default queryset and nothing has to re-sort.

The subtraction is frozen onto the salary request (`gross_amount` − `advance_deduction` = `amount`), the same gross/deduction/net shape the bills use for discounts, so a payslip can show the arithmetic instead of a number nobody can reconcile.

**Recovery is written at disbursement, not at request.** A request that is rejected or still pending has handed over nothing; recording a repayment against it would write off a debt the clinic never collected. `_apply_advance_recovery` runs inside `disburse_salary_payment`.

That split creates one hazard, which `_recoverable_now` closes: two open salary requests would both read the same untouched `recovered_amount` and deduct the same advance twice, so the available figure subtracts what other open requests have already promised.

### A month swallowed whole

`SalaryPayment.amount` may be **zero**, and the validator was relaxed to allow it. A month whose advances take all of it is a real payroll event that moved no money — the same shape a fully discounted bill has in `enrollments` (settled, no payment, no receipt). No `Expense` is written: that money already left as the advance, and a zero-value expense line answers to nothing.

Refusing instead would **deadlock** — the salary could never be requested, so the advance could never be recovered.

Only reachable when the salary is cut after the advance was granted; the request-time ceiling keeps an advance inside one month's pay.

### Endpoints

- `POST /api/staff/{id}/request-advance/` — Manager
- `GET /api/staff/{id}/advances/`
- `/api/staff/advances/` — the queue, pending first; `review` is Admin-only, `disburse` Manager-only

## Part 2 — History was being filtered by today's roster

Three reads asked a backward-looking question and answered it with a list about *right now*. Each one silently lost records.

### Staff who have left

`StaffMember` is soft-delete, so a resignation hides the row from the default manager — and with it, that person's attendance on every past day and their salary and bonuses in every past month. The `StaffAttendance` rows were never touched; nothing could see them.

Somebody who worked a day still worked it. `_roster_including_departed()` reads `all_objects` through the same branch rule, and both `today-attendance` (for a past date) and `monthly-report` use it.

### The same bug on patients

`attendance_roster` resolved `roster_patient_ids` through the soft-delete manager, so a patient removed from the directory vanished from the days they actually attended. `roster_patient_ids` had already decided who the day covers, from the enrollment windows — filtering it again by today's visible list was second-guessing that with the wrong question.

### A past day was being auto-closed

The destructive half, and the reason this is worth a doc. `today-attendance` ran `mark_no_show_absentees` on **every** read. Asking for last Tuesday marked everyone without a row that day **absent** — inventing an observation nobody made, and permanently, since `check_in` refuses once the office has closed.

`?date=` now splits the endpoint in two:

| | today | any other date |
|---|---|---|
| auto-close jobs | run | **do not run** |
| roster | active staff | includes those who have left |
| a missing key means | not checked in yet — the screen offers the button | no record exists — the screen shows nobody |

A malformed `?date=` is refused rather than falling back to today, the same way `?month=` is: a typo that quietly returned the live sheet would be read as that day's record.

### The shared helper

`BranchScopedQuerySetMixin.scope_to_branch(queryset)` was split out of `get_queryset` so a view can apply the branch rule to a *different* manager of the same model without restating the Manager/Admin logic and letting the two drift apart.

## Tests

- `apps/staff/tests/test_advances.py` — 22: the ceiling, the approval chain, oldest-first recovery, the two-open-requests hazard, the swallowed month
- `apps/staff/tests/test_attendance_history.py` — 10: departed staff on past days and in past payroll, and that a past day is never auto-closed
- `apps/patients/tests/test_attendance.py::TestAPastSheetKeepsRemovedPatients` — 3

Full suite: **1498 passed**.
