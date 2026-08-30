# 07 — Due Payments

Depends on `01`, `02`, `04`, `05`.

Read `docs/00-OVERVIEW.md` first for the cross-cutting rules.

Frontend source: `src/lib/api/duePayments.ts`, `src/components/duePayments/*`.

---

## What this module is

**No model of its own.** It's a denormalized *view* that aggregates the currently-due `MonthlyBill` and `Installment` rows from `05` into one unified list, joined with patient and service names — plus the action to collect payment against them.

Per enrollment/plan, **only the single currently-due item** appears (not every unpaid one), matching the mock: `bills.find(status === "due")`. Terminated enrollments/plans are excluded entirely.

⚠️ **`amount` here is the remaining balance — `bill.amount − bill.amount_paid`, not `bill.amount`.** Partial refunds (see `04`) mean a bill can be partly settled: a ৳5,000 bill with ৳2,000 refunded shows **৳2,000** outstanding, not ৳5,000. Every total on this screen and in the summary below must use the remaining balance. Using the raw `amount` would overstate what every affected patient owes.

Item shape (`DuePaymentItem`):
```
key, type ("monthly"|"installment"), patientId, patientName, patientCode,
serviceId, serviceName, branchId, label, amount, refId, refKey,
installmentIndex?, installmentsTotal?, installmentsRemaining?
```

The three `installment*` fields are installment-only, powering the "2 of 4 remaining" style display.

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/due-payments/` | `?search=&type=&page=&pageSize=` |
| GET | `/due-payments/summary/` | `{ totalDue, monthlyDue, installmentDue }` — `?branch=`, **`?date=`** |
| POST | `/due-payments/collect/` | Creates the Payment and settles the bill/installment |
| POST | `/due-payments/{type}/{refId}/terminate/` | Ends the enrollment/plan |

`search` matches patient name, patient code, or service name. `type` filters monthly vs installment.

### `POST /due-payments/collect/`

Input: `{ type, refId, refKey, patientId, method, amount, idempotencyKey }`.

This is **exactly the same atomic transition** documented in `05-enrollments.md` — lock the row, reject if already paid, create the Payment, mark paid + stamp `paid_at`, promote the next item to `due`. Implement it **once** as a shared service function and have both `/due-payments/collect/` and the `05` pay endpoints call it. Two divergent copies of money-settlement logic is exactly the kind of duplication that rots over years.

Recompute `amount` server-side from the bill/installment; don't trust the client's figure.

### `POST .../terminate/`

Ends the underlying enrollment or plan (`status = terminated`). Already-collected Payments survive — history stays intact. Log to the audit trail.

---

## ⚠️ Historical "outstanding due as of date X"

`summary` takes an optional `?date=` from the dashboard date picker, and must reconstruct **what was outstanding on that date**, not today's snapshot. This was a real bug already found and fixed in the frontend — the logic below is the corrected version, so follow it precisely.

For each non-terminated enrollment/plan (scoped to branch):

**Monthly:** find the earliest bill that was still unpaid as of the target date — i.e. `paid_at IS NULL` **OR** `paid_at > end-of-target-day`. Count it only if the bill's own month had already started by then (`month-01 <= target date`).

**Installment:** skip plans created *after* the target date entirely (`plan.created_at > end-of-target-day`). Otherwise find the earliest installment with `paid_at IS NULL` OR `paid_at > end-of-target-day`.

### Two traps that already caused bugs

1. **Compare against the END of the target day (23:59:59.999), not its start.** Comparing against midnight makes a payment collected earlier the same day still look outstanding "as of today" — the exact off-by-a-day bug that shipped and had to be fixed.
2. **This is why `paid_at` exists on `MonthlyBill` and `Installment`.** A plain `status` field can't answer "was this paid *yet* on date X". If `paid_at` isn't set at settlement time, every historical figure silently comes back wrong — no error, just incorrect money on screen.

With no `date` param, return the current outstanding snapshot.

---

## Required Tests

**Listing**
- Shows only the *currently-due* item per enrollment/plan — an enrollment with 1 paid + 1 due + 1 upcoming contributes exactly one row (the due one).
- Terminated enrollments/plans excluded.
- `type` filter isolates monthly vs installment.
- `search` matches patient name, patient code, and service name.
- `installmentIndex` / `installmentsTotal` / `installmentsRemaining` correct (e.g. paying 1 of 4 → next row shows index 2, total 4, remaining 2).
- Pagination `count` reflects the filtered total.

**Branch isolation**
- Manager A's list excludes Branch B's dues.
- Manager A collecting against a Branch B item → 404.
- Admin sees all; `?branch=X` narrows correctly.
- **Admin cannot collect payment** (money-moving actions are branch-manager-only — the frontend passes `readOnly` for Admin; enforce server-side).

**Collection**
- Creates a Payment with the right category (`monthly`/`installment`), marks the item paid, **stamps `paid_at`**, promotes the next to `due`.
- `amount` recomputed server-side — post a tampered amount and assert the stored Payment matches the bill, not the request.
- Already-paid item → rejected, no second Payment.
- **Concurrent collection of the same due item → exactly one Payment created.**
- Idempotency replay → one Payment, identical response.
- Atomicity: forced mid-transaction failure → full rollback, no orphaned Payment.
- Collecting the last item leaves nothing due for that enrollment/plan.

**Summary — current**
- `totalDue == monthlyDue + installmentDue`.
- Branch-scoped totals exclude other branches.
- Amounts exact as Decimal.
- No outstanding dues → zeros, not an error.

**Partially-settled bills (guards the overstatement bug)**
- A ৳5,000 bill with ৳2,000 partially refunded contributes **৳2,000** to `totalDue`, not ৳5,000.
- The same bill's row in the list shows ৳2,000 as its `amount`.
- Collecting that ৳2,000 clears the bill entirely (`amount_paid` back to full).
- A bill with `amount_paid == 0` contributes its full amount, unchanged.
- Historical `?date=` totals also use the remaining balance as of that date.

**Summary — historical (the highest-value tests in this module)**
- Collect a payment today, then query `?date=<yesterday>` → **the old, higher figure** still shows.
- Query `?date=<today>` immediately after collecting → the **new, lower** figure (the end-of-day boundary test — a start-of-day comparison fails here).
- A plan created *after* the target date contributes nothing to that date's total.
- A monthly bill whose month hadn't started by the target date isn't counted.
- A bill paid *before* the target date isn't counted.
- A bill never paid at all is counted for any date on/after its month start.
- End-to-end: seed a known payment timeline, then assert the exact expected figure for several different dates.

**Termination**
- Terminated enrollment disappears from dues.
- Its already-collected Payments remain in reporting.
- Audit entry recorded.
