# 05 — Enrollments (Monthly, Installment, Booking)

Depends on `01`, `02`, `03`, `04`.

Read `docs/00-OVERVIEW.md` first for the cross-cutting rules.

Frontend source: `src/lib/api/monthlyEnrollments.ts`, `installmentPlans.ts`, `bookings.ts`, and the four wizards in `src/components/services/*ServiceEnrollment.tsx`.

---

## The four enrollment flows

| Category | Flow | Creates |
|---|---|---|
| **Daily** | Pick service → search patient → confirm → pay → receipt | A `Payment` only (no enrollment record — it's a one-off visit) |
| **Monthly** | Pick service → patient → create enrollment → view bills → pay → receipt | `MonthlyEnrollment` + `MonthlyBill` rows, then a `Payment` per bill paid |
| **Installment** | Pick service → patient → choose 2/3/4 installments → schedule → pay → receipt | `InstallmentPlan` + `Installment` rows, then a `Payment` per installment paid |
| **Online** | Pick service → patient → date & time → advance payment → confirmation → receipt | `Booking` + a `Payment` for the advance |

Daily needs no model of its own — it goes straight to `04-payments-core.md`.

---

## Models

### MonthlyEnrollment + MonthlyBill

**The mock embeds `bills` as an array inside the enrollment. In Postgres these must be separate related tables** — reporting and due-payment queries filter/aggregate across bills directly, which a JSON blob makes slow and awkward.

**MonthlyEnrollment:** `patient` FK, `service` FK, `branch` FK, `status` (`active`|`terminated`), `created_at`

**MonthlyBill:** `enrollment` FK, `month` (`"2026-08"`), `label` (`"August 2026"`), `amount` (**Decimal**), **`amount_paid`** (Decimal, default 0), `status` (`paid`|`due`|`upcoming`|`overdue`), **`paid_at`** (DateTime, nullable), **`due_date`** (Date — the 5th of that bill's month)

⚠️ **`amount_paid` exists because partial refunds are supported** (see `04`). The outstanding balance of a bill is **`amount − amount_paid`**, not `amount`. Every place that reasons about what's owed — the oldest-first rule, overdue detection, Outstanding Due totals in `07` — must use the remaining balance, not the full bill amount. A bill counts as `paid` when `amount_paid >= amount`.

Collection endpoints currently require the **full remaining balance** — partial *payment* is not enabled, only partial refund. See `04` for why.

Bill generation: on enrollment, create the current month + next two. First is `due`, the rest `upcoming`.

### ✅ CONFIRMED: monthly billing rules

**1. Bills auto-generate every month via a scheduled job.** A monthly task creates the next bill for every `active` enrollment, so billing continues indefinitely without manual re-enrollment. Unpaid bills accumulate as real outstanding dues — the clinic is owed for each month of service whether or not the patient paid.

Requirements for the job:
- **Idempotent** — running it twice in one month must not create duplicate bills. Enforce with a DB unique constraint on `(enrollment, month)`, don't rely on the schedule firing exactly once.
- **Catch-up capable** — if the server was down on the 1st, the next run must backfill the missed month(s), not skip them silently.
- Skips `terminated` enrollments.
- Log every run (how many bills created) and alert on failure — a silently dead billing job means the clinic stops invoicing without noticing.

Use a scheduled task runner (django-cron / Celery beat / a management command on system cron — pick based on the deployment, note it in `ROADMAP` Phase 10). Keep it a plain management command so it can also be triggered manually for catch-up.

**2. Payment is due by the 5th of the month; after that the patient doesn't receive service.**
- Each bill carries `due_date` = the **5th of its own month**.
- A `due` bill still unpaid after its `due_date` becomes **`overdue`** (new status — not in the frontend mock, so this is a frontend addition).
- `overdue` must be treated as outstanding everywhere `due` is (due-payments list, Outstanding Due totals in `07`) — it's unpaid money, just later.

**Enforcement: surface it, don't hard-block.** The system flags the patient clearly and lets the manager decide; it does **not** automatically block enrollments, bookings, or sales.

Rationale (this is the standard approach for billing systems that serve walk-in humans): a hard block would let a one-day delay, a bank holiday, or a mis-recorded cash payment turn away a paying patient with no override path. Real-world clinics need staff judgement at the counter. So the system's job is to make the overdue state impossible to miss, and to keep the money correctly tracked — not to enforce the policy mechanically.

Implementation:
- Expose a derived, read-only **`serviceStatus`** on the patient: `active` | `overdue`. It is `overdue` when the patient has any `overdue` bill or installment on a non-terminated enrollment/plan.
- **Derive it, never store it.** A stored flag would go stale the moment a bill crosses its due date without anything writing to that row. Compute from the bill data at query time.
- Include it on the patient directory listing, the patient profile, and due-payment rows so the badge can appear everywhere the patient does.
- Also expose `overdueAmount` and `overdueSince` (the oldest overdue `due_date`) so the UI can show *how much* and *how long*, not just a binary flag. A manager deciding whether to serve someone needs that context.
- Frontend follow-up: an "Overdue — সেবা বন্ধ" badge in those three places. Not built yet.
- Overdue status changes nothing about what the API allows — enrollment, booking, and material sale all still succeed. Do not add blocking logic to those endpoints.

**3. A patient can stop the service — but only after clearing what they owe.**

`terminate` sets `status = terminated` and stops new bill generation. Already-collected payments and history stay intact.

### ✅ CONFIRMED: termination is blocked while dues are outstanding

**Termination is refused if the enrollment/plan has any unpaid (`due` or `overdue`) bill or installment.** The patient must settle the outstanding amount first; only then can the service be closed.

Return a 400 naming exactly what's owed — e.g. *"৳5,000 outstanding (August 2026) must be paid before this service can be stopped."* Include the amount and the specific bills in the error payload so the UI can show them and offer a "Collect payment" action directly.

This closes a real revenue leak: without it, a patient could avoid an unpaid bill simply by asking to stop the service, and the debt would vanish from Outstanding Due.

**Consequence to be aware of:** there is now no path to close an enrollment for a patient who genuinely won't or can't pay (moved away, disputed charge, goodwill write-off). Every clinic eventually hits this. The clean solution is an **Admin-only write-off** — Admin cancels the outstanding bill with a recorded reason, which removes it from Outstanding Due as a deliberate, audited decision rather than a silent disappearance, after which termination proceeds normally. Not built; raise with the client, because otherwise these enrollments will sit stuck forever and staff will invent workarounds (like paying it out of the till) that corrupt the books.

### InstallmentPlan + Installment

Same rule — installments become their own table, not an embedded array.

**InstallmentPlan:** `patient` FK, `service` FK, `branch` FK, `total_amount` (**Decimal**), `status`, `created_at`

**Installment:** `plan` FK, `index` (1-based), `label` (`"1st Installment"`), `amount` (**Decimal**), **`amount_paid`** (Decimal, default 0), `status`, **`paid_at`** (DateTime, nullable)

Same `amount_paid` semantics as MonthlyBill above.

Split logic (from `buildInstallments`): `base = floor(total / count)`, and **the last installment absorbs the remainder** so the parts always sum exactly to the total. Frontend offers 2, 3, or 4 installments.

Worked example: ৳18,500 over 3 → `6166 + 6166 + 6168` = 18,500 exactly. Never let rounding lose or invent money.

### Booking

`booking_code` (unique, `BKG-{year}-{5-digit}`, race-safe), `patient` FK, `service` FK, `branch` FK, `date`, `time`, `advance_amount` (**Decimal**), `status` (`confirmed`|`cancelled`)

Advance is **50%** of the service fee (`ADVANCE_RATIO = 0.5` in `OnlineServiceEnrollment.tsx`). Booking window is 10:00–18:00 (enforced in the UI; **re-validate server-side**).

### ⚠️ `paid_at` is load-bearing — do not omit it

`paid_at` on both `MonthlyBill` and `Installment` is what makes historical "what was outstanding on date X" reconstruction possible (see `07-due-payments.md`). Set it **at the moment** a bill/installment is marked paid. Without it, the dashboard's date-picker feature silently returns wrong numbers for past dates. This was a real bug already found and fixed in the frontend.

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/monthly-enrollments/` | `{ patientId, serviceId, fee }` → enrollment + 3 bills |
| POST | `/monthly-enrollments/{id}/pay-bill/` | `{ month }` → Payment + mark bill paid + advance next |
| POST | `/monthly-enrollments/{id}/terminate/` | Stops future billing |
| POST | `/installment-plans/` | `{ patientId, serviceId, totalAmount, numberOfInstallments }` |
| POST | `/installment-plans/{id}/pay-installment/` | `{ index }` → Payment + mark paid + advance next |
| POST | `/installment-plans/{id}/terminate/` | |
| POST | `/bookings/` | `{ patientId, serviceId, date, time, advanceAmount }` |

`branch` is always taken from `request.user.branch`, never the request body.

### The payment transition (both pay endpoints) — must be atomic

One `transaction.atomic()` block doing all of:
1. `select_for_update()` the target bill/installment (prevents double-collection races)
2. Reject if already `paid`
3. Create the `Payment` (category `monthly`/`installment`, honoring the idempotency key)
4. Mark it `paid` and stamp **`paid_at = now()`**
5. Advance the **next** bill/installment from `upcoming` → `due`

Step 5 is the sequencing rule from the mock: paying bill N promotes bill N+1 to due. Paying the final one leaves nothing due.

### ✅ CONFIRMED: oldest unpaid first — no skipping ahead

A patient with an unpaid August bill cannot pay September while August is outstanding. Payment must always settle the **oldest** unpaid bill/installment for that enrollment or plan.

**Enforce in step 2 of the transaction:** if the targeted bill/installment is not the oldest unpaid one, reject it with an error naming what must be paid first — e.g. *"August 2026 (৳5,000) must be paid before September 2026."* Don't just say "invalid"; the manager is standing in front of a patient and needs to know what to collect.

This is standard receivables practice (oldest-first application). Without it, a patient can keep paying only the current month while an old debt quietly ages forever, and the Outstanding Due figure stops reflecting anything actionable.

Applies identically to installments: installment 2 can't be paid while installment 1 is outstanding.

Note this makes an `overdue` bill genuinely blocking in a financial sense — the patient can't move forward without clearing it — even though overdue status itself doesn't block enrollments or bookings (see above). Those are two different things: the first is accounting order, the second is service access.

**Currently the frontend does steps 3 and 4–5 as two separate API calls** (`createPayment` then `payBill`) — a crash between them leaves money collected with the bill still marked unpaid. The backend must expose this as **one atomic endpoint** so that split can't happen. This is a genuine improvement over the mock, not a deviation to avoid.

---

## Required Tests

**Monthly enrollment**
- Creating an enrollment generates exactly 3 bills: current month `due`, next two `upcoming`.
- Bill `amount` equals the service fee, exact as Decimal.
- Month labels/keys are correct across a year boundary (December enrollment → Dec, Jan, Feb of the next year).
- Enrollment stores the authenticated manager's branch, ignoring any posted branch.
- Each bill's `due_date` is the **5th of its own month** (not 5 days after creation) — including across a year boundary.

**Monthly bill generation job**
- Running the job creates the next month's bill for each `active` enrollment.
- **Running it twice in the same month creates no duplicate** (the idempotency test — assert the `(enrollment, month)` unique constraint holds).
- Skips `terminated` enrollments entirely.
- **Catch-up:** simulate the job not running for 2 months, then run it — the missed months are backfilled, not skipped.
- New bills get the correct `due_date` (5th of their month) and start as `upcoming`/`due` per the sequencing rule.
- Job failure is logged/alerted rather than failing silently.

**Overdue status**
- A `due` bill past its `due_date` is reported as `overdue`.
- A bill paid **before** the 5th never becomes overdue.
- A bill paid **after** the 5th: was overdue before payment, is not after.
- Patient `serviceStatus` is `overdue` when any bill or installment is overdue, `active` otherwise.
- **`serviceStatus` is derived, not stored** — crossing the due date with no write to the row still flips it (test by advancing time past the 5th without touching the record).
- `overdueAmount` sums all overdue bills/installments; `overdueSince` is the **oldest** overdue `due_date`.
- Terminated enrollments' bills don't contribute to `serviceStatus`.
- **Overdue does not block anything** — assert that enrollment, booking, and material sale all still succeed for an overdue patient. This is deliberate; a future change that adds blocking should fail this test.

**Installment plan**
- **Split sums exactly to the total** — test ৳18,500 / 3 → `6166 + 6166 + 6168`.
- Test 2, 3, and 4 installments; test a total that divides evenly (no remainder) and one that doesn't.
- First installment `due`, rest `upcoming`.

**Payment transition (highest-value tests here)**
- Paying a due bill: creates a Payment with `category: "monthly"`, marks bill `paid`, **sets `paid_at`**, promotes the next bill to `due`.
- Same for installments with `category: "installment"`.
- Paying the **last** bill/installment leaves nothing `due` and doesn't error.
- Attempting to pay an already-`paid` bill → rejected, no second Payment created.

**Oldest-first ordering (confirmed rule)**
- With August unpaid and September due, attempting to pay **September** → rejected; the error names August and its amount.
- Paying **August** in the same situation → succeeds.
- After August is paid, September becomes payable.
- Same for installments: paying installment 2 while 1 is unpaid → rejected; paying 1 → succeeds.
- With **two** months overdue, only the oldest is payable; clearing it makes the next one payable.
- No Payment is created by a rejected out-of-order attempt (assert the payment count is unchanged).
- An `overdue` bill is still payable when it's the oldest — overdue status must not accidentally block the very payment that clears it.
- **Concurrent requests to pay the same bill → exactly one Payment created** (the `select_for_update` test).
- **Atomicity:** force a failure after the Payment is created but before the bill update — assert full rollback, no orphaned Payment.
- Idempotency key replay → single Payment, identical response.

**Termination**
- **Terminating with an unpaid `due` bill → rejected**; the error names the amount and the bill.
- **Terminating with an `overdue` bill → rejected** likewise.
- Terminating with an unpaid installment → rejected.
- Terminating once all bills/installments are `paid` → succeeds.
- A rejected termination leaves the enrollment `active` (assert status unchanged).
- Terminated enrollment/plan stops appearing in due-payments and active-services.
- Already-paid bills' Payments survive termination (history intact).
- Terminating twice is safe/idempotent.
- No new bills are generated for a terminated enrollment on the next job run.

**Booking**
- `booking_code` format correct; concurrent creates produce unique codes.
- `advance_amount` = 50% of service fee; server recomputes rather than trusting a client-supplied amount.
- Time outside 10:00–18:00 → rejected server-side (don't rely on the UI constraint).
- Past date → rejected (confirm desired behavior with the user).
- Creates a Payment with `category: "online"`.

**Branch isolation & permissions**
- Manager A cannot create an enrollment for Branch B's patient.
- Manager A cannot pay/terminate Branch B's enrollment or plan → 404.
- Admin browsing a branch cannot perform these money-moving actions (the frontend deliberately omits them from `branchNav`; enforce server-side too).
